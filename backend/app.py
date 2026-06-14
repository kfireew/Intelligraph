"""
graphify-qa: thin pod server. Serves static UI, handles OIDC SSO, relays LLM calls,
provides optional online MCP, and serves downloadable tools (MCP server, graph builder).

Usage: python app.py [--port 5050]
"""

import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse

import re
import requests
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, send_from_directory, session,
                   stream_with_context, url_for)
import bb_auth

# ── App setup ────────────────────────────────────────────────────

TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
DOWNLOADS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app = Flask(__name__, template_folder=TEMPLATES,
            static_folder=STATIC, static_url_path="/static")
REPO_DIR = os.environ.get("INTELLIGRAPH_REPO_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "repos"))
TEMP_DIR = os.environ.get("INTELLIGRAPH_TEMP", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "temp"))
os.makedirs(REPO_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
# ── SQLite persistence (optional) ──
INTELLIGRAPH_DB = os.environ.get("INTELLIGRAPH_DB", os.path.join(TEMP_DIR, "intelligraph.db"))

def _get_db():
    """Return SQLite connection. In-memory when INTELLIGRAPH_DB not set."""
    if INTELLIGRAPH_DB:
        os.makedirs(os.path.dirname(INTELLIGRAPH_DB) or ".", exist_ok=True)
        conn = sqlite3.connect(INTELLIGRAPH_DB, check_same_thread=False)
    else:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS projects (id INTEGER, user_key TEXT, data TEXT, PRIMARY KEY(user_key, id))")
    conn.execute("CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, user_key TEXT, project_id INTEGER, role TEXT, content TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS uploads (user_key TEXT, project_id INTEGER, type TEXT, data TEXT, UNIQUE(user_key, project_id, type))")
    return conn

_db = None  # lazy init

def _db_conn():
    global _db
    if _db is None:
        _db = _get_db()
    return _db

def _save_project(pid, proj):
    """Persist project to SQLite."""
    try:
        conn = _db_conn()
        safe = {k: v for k, v in proj.items() if k not in ("_G", "graph_html_path")}
        safe["_has_graphify"] = bool(proj.get("graphify_data"))
        safe["_has_crg"] = bool(proj.get("crg_db_path"))
        safe["_has_html"] = bool(proj.get("graph_html_path"))
        conn.execute("INSERT OR REPLACE INTO projects(id, user_key, data) VALUES(?, ?, ?)",
                     (pid, _user_key(), json.dumps(safe)))
        conn.commit()
    except Exception as e:
        app.logger.warning("DB save failed: %s", e)

def _load_projects():
    """Load projects from SQLite on startup."""
    try:
        conn = _db_conn()
        rows = conn.execute("SELECT id, data FROM projects WHERE user_key = ?", (_user_key(),)).fetchall()
        for row in rows:
            data = json.loads(row["data"])
            if row["id"] not in _projects():
                _projects()[row["id"]] = data
    except Exception as e:
        app.logger.warning("DB load failed: %s", e)

app.secret_key = os.environ.get("SECRET_KEY", "intelligraph-dev-key-do-not-use-in-production")

OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_CONFIG = {}

# ── Project storage ───────────────────────────────────────────────
# user_key -> {pid: {name, git_url, status, nodes, edges, crg_db_path, graphify_data, oidc_token}}
# user_key is session-based: OIDC sub or session ID for anonymous users
_PROJECTS = {}  # {user_key: {pid: project_dict}}
_NEXT_PID = {}   # {user_key: next_pid}`


def _user_key():
    """Stable identifier for the current user across the session."""
    u = get_user()
    if u and u.get("source") == "oidc":
        uk = session.get("oidc_sub", u["name"])
    else:
        uk = session.get("_anon_key") or _init_anon()
    # Load persisted projects on first access for this user
    if uk not in _PROJECTS:
        _load_projects()
    return uk


def _init_anon():
    key = secrets.token_hex(8)
    session["_anon_key"] = key
    session.permanent = True
    return key


def _projects():
    return _PROJECTS.setdefault(_user_key(), {})


def _next_pid():
    uk = _user_key()
    pid = _NEXT_PID.get(uk, 1)
    _NEXT_PID[uk] = pid + 1
    return pid


def fetch_oidc_config():
    global OIDC_CONFIG
    if not OIDC_ISSUER:
        return
    try:
        url = f"{OIDC_ISSUER.rstrip('/')}/.well-known/openid-configuration"
        OIDC_CONFIG = requests.get(url, timeout=10).json()
    except Exception:
        pass


# ── Auth ─────────────────────────────────────────────────────────

def get_user():
    if "user" in session:
        return session["user"]
    for h in ("X-Auth-Username", "X-Forwarded-User", "X-User", "REMOTE_USER"):
        val = request.headers.get(h)
        if val:
            return {"name": val, "source": "sso-proxy"}
    return None


@app.route("/auth/login")
def auth_login():
    if not OIDC_ISSUER:
        return jsonify({"error": "OIDC not configured"}), 400
    if not OIDC_CONFIG:
        fetch_oidc_config()
    if not OIDC_CONFIG:
        return jsonify({"error": "Cannot fetch OIDC config"}), 500
    state = secrets.token_urlsafe(16)
    session["oidc_state"] = state
    params = {
        "client_id": OIDC_CLIENT_ID,
        "response_type": "code",
        "scope": "openid profile email",
        "redirect_uri": url_for("auth_callback", _external=True),
        "state": state,
    }
    return redirect(f"{OIDC_CONFIG['authorization_endpoint']}?{urlencode(params)}")


@app.route("/auth/callback")
def auth_callback():
    if request.args.get("state") != session.pop("oidc_state", None):
        return "Invalid state", 400
    token_resp = requests.post(OIDC_CONFIG["token_endpoint"], data={
        "grant_type": "authorization_code", "code": request.args.get("code"),
        "redirect_uri": url_for("auth_callback", _external=True),
        "client_id": OIDC_CLIENT_ID, "client_secret": OIDC_CLIENT_SECRET,
    }, timeout=10).json()
    access_token = token_resp.get("access_token")
    if not access_token:
        return f"Token error: {token_resp.get('error_description', 'unknown')}", 400
    userinfo = requests.get(OIDC_CONFIG["userinfo_endpoint"],
                            headers={"Authorization": f"Bearer {access_token}"},
                            timeout=10).json()
    session["user"] = {
        "name": userinfo.get("preferred_username") or userinfo.get("email", "unknown"),
        "email": userinfo.get("email", ""),
        "source": "oidc",
    }
    session["oidc_access_token"] = access_token
    session["oidc_sub"] = userinfo.get("sub", "")
    return redirect("/")


@app.route("/auth/logout")
def auth_logout():
    session.clear()
    if OIDC_CONFIG.get("end_session_endpoint"):
        return redirect(OIDC_CONFIG["end_session_endpoint"])
    return redirect("/")


@app.route("/auth/me")
def auth_me():
    u = get_user()
    return jsonify({"authenticated": bool(u), "user": u,
                     "oidc_configured": bool(OIDC_ISSUER),
                     "login_url": "/auth/login" if OIDC_ISSUER else None,
                     "logout_url": "/auth/logout"})


# ── LLM relay ────────────────────────────────────────────────────

ALLOWED_LLM_HOSTS = set(os.environ.get(
    "LLM_ALLOWED_HOSTS", "ai-services.ai.idf.cts,litellm-api.up.railway.app,openrouter.ai"
).split(","))

@app.route("/llm/relay", methods=["POST"])
def llm_relay():
    """Relay LLM requests — forwards user's LLM call through the pod."""
    data = request.get_json(force=True)
    llm_url = data.get("url", "").strip()
    llm_token = data.get("token", "").strip()
    payload = data.get("payload", {})

    if not llm_url:
        return jsonify({"error": "llm_url required"}), 400

    host = urlparse(llm_url).hostname
    if host not in ALLOWED_LLM_HOSTS:
        return jsonify({"error": "provider not allowed"}), 403

    headers = {"Content-Type": "application/json"}
    if llm_token:
        headers["Authorization"] = f"Bearer {llm_token}"
    if host == "openrouter.ai":
        headers["HTTP-Referer"] = "https://localhost"
        headers["X-Title"] = "Intelligraph"

    try:
        resp = requests.post(llm_url, json=payload, headers=headers, timeout=30)
        resp.encoding = "utf-8"
        return jsonify({"status": resp.status_code, "body": resp.text[:8000]})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/llm/relay/stream", methods=["POST"])
def llm_relay_stream():
    """Stream LLM response via SSE (start/token/done/error events)."""
    data = request.get_json(force=True)
    llm_url = data.get("url", "").strip()
    llm_token = data.get("token", "").strip()
    payload = data.get("payload", {})
    project_id = data.get("project_id")

    if not llm_url:
        return jsonify({"error": "llm_url required"}), 400

    host = urlparse(llm_url).hostname
    if host not in ALLOWED_LLM_HOSTS:
        return jsonify({"error": "provider not allowed"}), 403

    def generate():
        headers = {"Content-Type": "application/json"}
        if llm_token:
            headers["Authorization"] = f"Bearer {llm_token}"
        if host == "openrouter.ai":
            headers["HTTP-Referer"] = "https://localhost"
            headers["X-Title"] = "Intelligraph"

        p = dict(payload)

        full_text = ""
        try:
            yield f"data: {json.dumps({'event': 'start', 'data': {}})}\n\n"
            resp = requests.post(llm_url, json=p, headers=headers, timeout=120)
            if resp.status_code != 200:
                yield f"data: {json.dumps({'event': 'error', 'data': {'message': f'LLM returned {resp.status_code}: {resp.text[:200]}'}})}\n\n"
                return

            # Try JSON first — handles providers that misreport content-type as SSE
            content_type = resp.headers.get("content-type", "")
            resp.encoding = "utf-8"
            text = None
            try:
                body = resp.json()
                text = body.get("choices", [{}])[0].get("message", {}).get("content") or None
            except Exception:
                pass

            if text:
                full_text = text
                yield f"data: {json.dumps({'event': 'token', 'data': {'text': text}})}\n\n"
            elif "text/event-stream" in content_type:
                # True SSE — parse data frames for delta content
                current_frame = []
                for line in resp.iter_lines(decode_unicode=True):
                    if line is None:
                        continue
                    line = line.rstrip("\r")
                    if line == "":
                        for frame_line in current_frame:
                            if frame_line.startswith("data: ") and "[DONE]" not in frame_line:
                                try:
                                    d = json.loads(frame_line[6:])
                                    delta = d.get("choices", [{}])[0].get("delta", {}).get("content") or ""
                                    if delta:
                                        full_text += delta
                                        yield f"data: {json.dumps({'event': 'token', 'data': {'text': delta}})}\n\n"
                                except (json.JSONDecodeError, KeyError):
                                    pass
                        current_frame = []
                    else:
                        current_frame.append(line)
            else:
                full_text = resp.text[:500]
                yield f"data: {json.dumps({'event': 'token', 'data': {'text': full_text}})}\n\n"

            done_data = {"text": full_text}
            if project_id is not None:
                warnings = _verify_paths(project_id, full_text)
                if warnings:
                    done_data["path_warnings"] = warnings
            yield f"data: {json.dumps({'event': 'done', 'data': done_data})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'event': 'error', 'data': {'message': str(e)}})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/llm/models", methods=["POST"])
def llm_models():
    """Fetch available models from provider."""
    data = request.get_json(force=True)
    llm_url = data.get("url", "").strip()
    llm_token = data.get("token", "").strip()
    if not llm_url:
        return jsonify({"models": []})
    base = re.sub(r"/chat/completions/?$", "", llm_url.rstrip("/"))
    models_url = f"{base}/models"
    host = urlparse(base).hostname
    if host not in ALLOWED_LLM_HOSTS:
        return jsonify({"models": []})
    headers = {}
    if llm_token:
        headers["Authorization"] = f"Bearer {llm_token}"
    try:
        resp = requests.get(models_url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return jsonify({"models": []})
        raw = resp.json()
        items = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = raw.get("data") or raw.get("models") or raw.get("items") or []
        if not isinstance(items, list):
            items = [raw] if isinstance(raw, dict) else []
        models = []
        for item in items:
            if not isinstance(item, dict):
                continue
            mid = item.get("id") or item.get("name") or item.get("slug") or item.get("model") or str(item)
            mname = item.get("name") or item.get("id") or item.get("label") or item.get("description") or mid
            context_len = item.get("context_length") or item.get("max_tokens") or item.get("max_context_length") or 0
            pricing = item.get("pricing", {})
            prompt_price = pricing.get("prompt") if isinstance(pricing, dict) else None
            models.append({"id": mid, "name": mname, "context_length": context_len})
        return jsonify({"models": models[:500]})
    except Exception:
        return jsonify({"models": []})


# ── Intent classification ────────────────────────────────────────

# ── Intent classification REMOVED — client-side only via intentDetector.js ──

FILE_PATH_PATTERN = re.compile(
    r"(?<![\w/.-])(?:[A-Za-z0-9_@.-]+/)*[A-Za-z0-9_@.-]+"
    r"\.(?:py|js|jsx|ts|tsx|json|md|yml|yaml|toml|txt|html|css|scss|java|go|"
    r"rs|cpp|c|h|hpp|cs|rb|php|sh|sql)(?::\d+)?(?![\w/-])"
)


def _verify_paths(project_id, llm_output):
    """Extract file paths from LLM output and verify against project graph data."""
    mentioned = set(FILE_PATH_PATTERN.findall(llm_output or ""))
    if not mentioned:
        return []
    uk = _user_key()
    proj = _PROJECTS.get(uk, {}).get(project_id, {})
    gf = proj.get("graphify_data") or {}
    valid = set()
    for n in gf.get("nodes", []):
        sf = n.get("source_file") or n.get("file_path") or ""
        if sf:
            valid.add(sf)
            valid.add(sf.split("/")[-1])
            valid.add(sf.split("\\")[-1])
    warnings = []
    for p in mentioned:
        if p not in valid and not any(p.endswith(v) or v.endswith(p) for v in valid if v):
            warnings.append(p)
    return warnings


# ── Downloads ────────────────────────────────────────────────────

@app.route("/download/mcp-server")
def download_mcp():
    """Download the standalone MCP server script."""
    mcp_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "mcp_server_standalone.py")
    return send_file(mcp_path, as_attachment=True,
                     download_name="mcp_server_standalone.py",
                     mimetype="text/x-python")


@app.route("/download/graph-builder")
def download_graph_builder():
    """Download the graph builder EXE if built, otherwise the Python script."""
    exe = os.path.join(DOWNLOADS, "graph-builder.exe")
    if os.path.exists(exe):
        return send_file(exe, as_attachment=True,
                         download_name="graph-builder.exe")
    # Fallback: send the Python script
    py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "graph_builder.py")
    return send_file(py, as_attachment=True,
                     download_name="graph_builder.py",
                     mimetype="text/x-python")





# ── Project management ───────────────────────────────────────────

def _name_from_url(url):
    # Bitbucket Server: .../projects/KEY/repos/NAME[/browse]
    m = re.search(r"/repos/([^/]+)", url)
    if m:
        return m.group(1)
    # Bitbucket Cloud / generic: last path segment
    m = re.search(r"/([^/]+?)(?:\.git)?$", url.rstrip("/"))
    return m.group(1) if m else url


@app.route("/projects", methods=["GET"])
def list_projects():
    return jsonify([{"id": pid, "name": p.get("name"), "status": p.get("status"),
                    "nodes": p.get("nodes", 0), "edges": p.get("edges", 0),
                    "has_graphify": bool(p.get("graphify_data")),
                    "has_crg": bool(p.get("crg_db_path") and os.path.exists(p.get("crg_db_path", "")))}
                   for pid, p in _projects().items()])

@app.route("/projects/clone", methods=["POST"])
def clone_project():
    data = request.get_json(force=True)
    git_url = data.get("git_url", "").strip()
    clone_type = data.get("type", "bitbucket")  # bitbucket/git accepted, or "upload"

    if not git_url and clone_type != "upload":
        return jsonify({"error": "git_url required (GitHub or Bitbucket)"}), 400

    # Bitbucket Data Center auth fields (optional)
    access_token = data.get("access_token")
    if access_token is not None:
        access_token = access_token.strip() or None
    bitbucket_username = data.get("bitbucket_username")
    if bitbucket_username is not None:
        bitbucket_username = bitbucket_username.strip() or None
    use_linked_credentials = data.get("use_linked_credentials", True)
    auth_provider = data.get("auth_provider", "bitbucket_datacenter")

    name = data.get("name") or _name_from_url(git_url)
    pid = _next_pid()
    proj = {"name": name, "git_url": git_url, "status": "cloning", "nodes": 0, "edges": 0}

    if clone_type in ("bitbucket", "git") and git_url:
        proj["status"] = "cloning"
        proj["crg_nodes"] = 0
        _projects()[pid] = proj

        try:
            repo_dir = os.path.join(REPO_DIR, f"{_user_key()}-{pid}")
            if os.path.exists(repo_dir):
                shutil.rmtree(repo_dir, ignore_errors=True)
            os.makedirs(repo_dir, exist_ok=True)

            # ── Credential resolution (Bitbucket Data Center) ──
            token = None
            username = None
            credential_source = "none"
            is_bitbucket = "bitbucket" in git_url.lower()

            if is_bitbucket and auth_provider == "bitbucket_datacenter":
                resolved = bb_auth.resolve_bitbucket_credential(
                    access_token=access_token,
                    bitbucket_username=bitbucket_username,
                    use_linked_credentials=use_linked_credentials,
                    user_key=_user_key(),
                    project_id=pid,
                )
                if resolved:
                    credential_source, token, username = resolved
                else:
                    # OIDC-authenticated user without credentials → fail immediately
                    if get_user():
                        status_code, body = bb_auth.missing_credential_error(is_oidc_user=True)
                        return jsonify(body), status_code
                    # Anonymous user: try no-auth public clone
                    token = None
                    username = None
                    credential_source = "none"

            # ── Log credential source (never the raw token) ──
            app.logger.info(
                "Clone auth [%s]: git_host=%s, repo=%s, source=%s, token_mask=%s",
                _user_key(),
                git_url.split("://", 1)[-1].split("/", 1)[0] if "://" in git_url else git_url.split("/")[0],
                _name_from_url(git_url),
                credential_source,
                bb_auth.token_display(token) if token else "[NONE]",
            )

            # ── Preflight: git ls-remote ──
            proj["status"] = "auth_test_pending"
            _projects()[pid] = proj

            preflight_ok, preflight_err = bb_auth.preflight_git_access(
                git_url, token=token, username=username,
            )

            if not preflight_ok:
                proj["status"] = "auth_failed"
                _projects()[pid] = proj
                _save_project(pid, proj)
                if preflight_err == "bitbucket_auth_failed":
                    error_code, error_body = bb_auth.auth_failed_error()
                    return jsonify(error_body), error_code
                elif preflight_err == "repo_not_found_or_no_access":
                    error_code, error_body = bb_auth.repo_not_found_error()
                    return jsonify(error_body), error_code
                else:
                    error_code, error_body = bb_auth.clone_failed_error()
                    return jsonify(error_body), error_code

            proj["status"] = "auth_test_passed"
            _projects()[pid] = proj

            # ── Clone (non-interactive, via GIT_ASKPASS) ──
            proj["status"] = "building"
            result = bb_auth.run_git(
                ["git", "clone", "--depth", "1", git_url, repo_dir],
                repo_dir="/",
                token=token,
                username=username,
                timeout=120,
            )

            if result.returncode != 0:
                proj["status"] = "clone_failed"
                _projects()[pid] = proj
                _save_project(pid, proj)
                return jsonify({
                    "error": "clone_failed",
                    "message": "Git clone failed after authentication preflight.",
                }), 500

            # ── Clean embedded credentials from remote URL ──
            bb_auth.clean_remote_url(repo_dir)

            _save_project(pid, proj)  # persist immediately on clone success

            # ── graphify update ──
            r = subprocess.run(["graphify", "update", "."], cwd=repo_dir,
                             capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                app.logger.warning("graphify update failed (rc=%d): %s",
                                   r.returncode, r.stderr[:200])

            # ── code-review-graph build ──
            r = subprocess.run(["code-review-graph", "build"], cwd=repo_dir,
                              capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                app.logger.warning("code-review-graph build failed (rc=%d): %s",
                                   r.returncode, r.stderr[:200])

            # ── Parse results ──
            gf_path = os.path.join(repo_dir, "graphify-out", "graph.json")
            crg_path = os.path.join(repo_dir, ".code-review-graph", "graph.db")

            if os.path.exists(gf_path):
                with open(gf_path) as f:
                    proj["graphify_data"] = json.load(f)
                proj["nodes"] = len(proj["graphify_data"].get("nodes", []))
                proj["edges"] = len(proj["graphify_data"].get("links", []))

            if os.path.exists(crg_path):
                proj["crg_db_path"] = crg_path
                import sqlite3
                conn = sqlite3.connect(f"file:{crg_path}?mode=ro", uri=True)
                cn = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                ce = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
                conn.close()
                proj["nodes"] = max(proj["nodes"], cn)
                proj["crg_nodes"] = cn
                proj["edges"] = max(proj["edges"], ce)
            else:
                app.logger.warning("CRG build did not produce output — skipping CRG analysis")

            # ── Generate graph.html from graphify_data ──
            if proj.get("graphify_data"):
                try:
                    import graphify
                    import graphify.export as gf_export
                    G = graphify.build_from_json(proj["graphify_data"])
                    if G and G.number_of_nodes() > 0:
                        comms = {}
                        for nid, ndata in G.nodes(data=True):
                            cid = ndata.get("community", 0)
                            if cid not in comms:
                                comms[cid] = []
                            comms[cid].append(nid)
                        html_path = f"{TEMP_DIR}/intelligraph-gf-html-{_user_key()}-{pid}-{int(time.time())}.html"
                        gf_export.to_html(G, comms, html_path)
                        proj["graph_html_path"] = html_path
                except Exception as e:
                    app.logger.warning("graph.html generation failed: %s", e, exc_info=True)

            # ── Optional: detect Nx workspace ──
            try:
                from nx_adapter import extract_nx_context
                nx_ctx = extract_nx_context(repo_dir)
                if nx_ctx.get("available"):
                    proj["nx_metadata"] = {k: v for k, v in nx_ctx.items() if k != "raw"}
                    proj["nx_raw"] = nx_ctx.get("raw", {})
                    proj["workspace_type"] = "nx"
                    proj["nx_available"] = True
                    app.logger.info("Nx workspace detected: %d projects, %d dependencies",
                                    len(nx_ctx.get("projects", [])),
                                    len(nx_ctx.get("dependencies", [])))
                else:
                    proj["workspace_type"] = "standard"
                    proj["nx_available"] = False
            except Exception as e:
                proj["workspace_type"] = "standard"
                proj["nx_available"] = False
                app.logger.warning("Nx detection failed (non-fatal): %s", str(e)[:200])
            if "nx_metadata" not in proj:
                proj["nx_metadata"] = {}

            proj["status"] = "ready"
            proj["repo_dir"] = repo_dir
            _save_project(pid, proj)

        except Exception as e:
            proj["status"] = "error"
            proj["error"] = str(e)[:500]
            app.logger.warning("Clone error [%s]: %s", _user_key(), str(e)[:500])
            return jsonify(proj), 500

        _projects()[pid] = proj

    return jsonify({"id": pid, **proj})


@app.route("/projects/<int:pid>", methods=["PATCH"])
def rename_project(pid):
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"id": pid, "name": ""}), 200
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    proj["name"] = name
    _save_project(pid, proj)
    return jsonify({"id": pid, "name": name})


@app.route("/projects/<int:pid>", methods=["DELETE"])
def delete_project(pid):
    proj = _projects().pop(pid, None)
    if proj and proj.get("repo_dir"):
        import shutil
        shutil.rmtree(proj["repo_dir"], ignore_errors=True)
    if proj:
        try:
            conn = _db_conn()
            conn.execute("DELETE FROM projects WHERE id=? AND user_key=?", (pid, _user_key()))
            conn.commit()
        except Exception as e:
            app.logger.warning("DB delete failed: %s", e)
    return jsonify({"status": "deleted"})


@app.route("/projects/<int:pid>/status")
def project_status(pid):
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"status": "not_found", "error": ""}), 200
    return jsonify({"id": pid, "status": proj["status"], "name": proj["name"],
                    "nodes": proj.get("nodes", 0), "edges": proj.get("edges", 0),
                    "error": proj.get("error", "")})


@app.route("/projects/<int:pid>/graph-data")
def project_graph_data(pid):
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"graphify": None, "nodes": 0, "edges": 0}), 200
    result = {"id": pid, "name": proj["name"], "status": proj.get("status"),
              "nodes": proj.get("nodes", 0), "edges": proj.get("edges", 0)}
    if "graphify_data" in proj and proj["graphify_data"]:
        result["graphify"] = proj["graphify_data"]
    crg_path = proj.get("crg_db_path")
    if crg_path and os.path.exists(crg_path):
        result["has_crg_db"] = True
        result["crg_db_size"] = os.path.getsize(crg_path)
    return jsonify(result)


@app.route("/projects/<int:pid>/crg-db")
def project_crg_db(pid):
    proj = _projects().get(pid)
    if not proj:
        return Response(b"", mimetype="application/octet-stream"), 200
    crg_path = proj.get("crg_db_path")
    if not crg_path or not os.path.exists(crg_path):
        return jsonify({"error": "no CRG DB available"}), 404
    return send_file(crg_path, as_attachment=False,
                     download_name=f"project-{pid}.db",
                     mimetype="application/octet-stream")


@app.route("/projects/<int:pid>/graph-html")
def project_graph_html(pid):
    """Serve graphify's graph.html with Intelligraph dark theme injected.
    Works for cloned repos (reads graphify-out/graph.html) AND uploads (generates HTML from graphify_data JSON)."""
    proj = _projects().get(pid)
    if not proj:
        return """<!DOCTYPE html><html><head><meta charset="utf-8"><style>body{background:rgba(0,0,0,0.8);color:#c9d1d9;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}p{text-align:center}</style></head><body><p>Project deleted or not found.<br>Select another project from the sidebar.</p></body></html>""", 200

    html = None
    repo_dir = proj.get("repo_dir")

    # 1. Try cloned repo's pre-built graph.html
    if repo_dir:
        p = os.path.join(repo_dir, "graphify-out", "graph.html")
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    html = f.read()
            except Exception:
                pass

    # 2. Fallback: use pre-generated HTML from upload or generate from graphify_data
    if not html:
        cached = proj.get("graph_html_path")
        if cached and os.path.exists(cached):
            with open(cached, "r", encoding="utf-8") as f:
                html = f.read()
    if not html and proj.get("graphify_data"):
        try:
            import graphify
            import graphify.export as gf_export

            gf_data = proj["graphify_data"]
            G = graphify.build_from_json(gf_data)
            if G and G.number_of_nodes() > 0:
                comms = {}
                for nid, ndata in G.nodes(data=True):
                    cid = ndata.get('community', 0)
                    if cid not in comms:
                        comms[cid] = []
                    comms[cid].append(nid)
                tmp_path = f"{TEMP_DIR}/intelligraph-gf-html-{_user_key()}-{pid}.html"
                gf_export.to_html(G, comms, tmp_path)
                if os.path.exists(tmp_path):
                    proj["graph_html_path"] = tmp_path
                    with open(tmp_path, "r", encoding="utf-8") as f:
                        html = f.read()
                else:
                    app.logger.warning("Generated graph HTML not found at %s", tmp_path)
        except Exception as e:
            return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>body{{background:rgba(0,0,0,0.8);color:#c9d1d9;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}p{{text-align:center;max-width:400px;line-height:1.5}}code{{background:#21262d;padding:2px 6px;border-radius:4px;font-size:12px}}</style></head><body><p>Failed to generate graph HTML.<br><code>{str(e)[:200]}</code><br><br>Upload a pre-built <code>graph.html</code> instead.</p></body></html>""", 500

    if not html:
        return """<!DOCTYPE html><html><head><meta charset="utf-8"><style>body{background:rgba(0,0,0,0.8);color:#c9d1d9;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}p{text-align:center;max-width:400px;line-height:1.6}a{color:#5b7fff}code{background:#21262d;padding:2px 6px;border-radius:4px;font-size:12px}</style></head><body><p>No graph data available.<br>Go to <b>Upload</b> tab and upload:<br><code>graph.json</code> + <code>graph.db</code> + <code>graph.html</code></p></body></html>""", 404

    # Inject Intelligraph dark theme CSS overrides
    THEME_OVERRIDES = """
<style id="intelligraph-theme">
/* ── Intelligraph theme overrides ── */
body {
    background: rgba(0,0,0,0.85) !important;
    color: #c9d1d9 !important;
    font-family: 'Space Grotesk', 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif !important;
    font-size: 13px !important;
}
#graph { position: relative !important; }
#graph canvas, #graph svg, #graph > div {
    background: rgba(0,0,0,0.8) !important;
}
/* ── Sidebar (graph internal nav) — restored ── */
#sidebar {
    background: rgba(0,0,0,0.85) !important;
    border-right: 1px solid #21262d !important;
    color: #c9d1d9 !important;
    font-family: 'Space Grotesk', 'DM Sans', sans-serif !important;
}
#sidebar h3 { color: #8b949e !important; font-family: 'Space Grotesk', 'DM Sans', sans-serif !important; }
#sidebar .neighbor-link { border-left-color: #30363d !important; }
#sidebar .neighbor-link:hover { background: #161b22 !important; }
#search {
    background: rgba(0,0,0,0.8) !important;
    border: 1px solid #30363d !important;
    color: #c9d1d9 !important;
    font-family: 'Space Grotesk', 'DM Sans', sans-serif !important;
}
#search:focus { border-color: #5b7fff !important; }
#search-results { background: #161b22 !important; border: 1px solid #30363d !important; }
#info-panel { color: #c9d1d9 !important; }
#info-content { color: #8b949e !important; }
#info-content .field b { color: #c9d1d9 !important; }
#info-content .field a { color: #5b7fff !important; }
#legend-wrap { border-top: 1px solid #21262d !important; }
#legend-wrap h3 { color: #8b949e !important; }
.legend-item:hover { background: #161b22 !important; }
.legend-cb, #select-all-cb { border-color: #30363d !important; background: rgba(0,0,0,0.8) !important; }
#stats { color: #8b949e !important; border-top: 1px solid #21262d !important; font-family: 'Space Grotesk', 'DM Sans', sans-serif !important; }
.legend-count { color: #8b949e !important; }
</style>

<script>
(function() {
  if (window.__intelligraphSidebar) return;
  window.__intelligraphSidebar = true;
  document.addEventListener('DOMContentLoaded', function() {
    // Theme: force Intelligraph backgrounds
    requestAnimationFrame(function() {
      document.body.style.setProperty("background", "radial-gradient(ellipse at 50% 0%, rgba(139,92,246,0.06), transparent 70%), rgba(0,0,0,0.85)", "important");
      var g = document.getElementById("graph");
      if (g) g.style.setProperty("background", "rgba(0,0,0,0.8)", "important");
      var c = document.querySelector("#graph canvas") || document.querySelector("#graph > div");
      if (c) c.style.setProperty("background", "rgba(0,0,0,0.8)", "important");
    });
  });
})();
</script>
"""
    # Inject Intelligraph dark theme CSS overrides
    # Find last </style> and inject overrides after it
    last_style = html.rfind('</style>')
    if last_style > 0:
        html = html[:last_style + 8] + THEME_OVERRIDES + html[last_style + 8:]
    else:
        html = html.replace('</head>', THEME_OVERRIDES + '\n</head>')

    # Google Fonts removed for offline support

    return html, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache, no-store, must-revalidate"}






def _get_graphify_path(proj):
    """Return filesystem path to a project's graph.json, writing in-memory
    data to a temp file for upload-based projects."""
    repo_dir = proj.get("repo_dir")
    if repo_dir:
        gf = os.path.join(repo_dir, "graphify-out", "graph.json")
        if os.path.exists(gf):
            return gf
    # Upload-based: write cached graphify_data to temp file
    gf_data = proj.get("graphify_data")
    if gf_data:
        tmp = f"{TEMP_DIR}/intelligraph-gf-{_user_key()}-{proj.get('id', 'unknown')}.json"
        with open(tmp, "w") as f:
            json.dump(gf_data, f)
        return tmp
    return None


@app.route("/graph/retrieve-context", methods=["POST"])
def graph_retrieve_context():
    """Backend-owned retrieval endpoint (Nx-aware).
    
    Frontend sends only { prompt, project_id }.
    Backend performs intent detection, file selection, context assembly.
    """
    data = request.get_json(force=True) or {}
    prompt = (data.get("prompt") or "").strip()
    project_id = data.get("project_id")
    if not prompt:
        return jsonify({"context": "", "files": [], "strategy": "no_prompt", "plan": {}}), 200

    proj = _projects().get(project_id) if project_id else None
    if not proj:
        return jsonify({"context": "", "files": [], "strategy": "no_project", "plan": {}}), 200

    from retrieval import retrieve_context
    result = retrieve_context(proj, prompt)
    return jsonify(result)


@app.route("/api/v1/projects/<int:pid>/completions", methods=["POST"])
def project_completions(pid):
    """Stateless completion endpoint for n8n/external automation.

    Each call creates a fresh LLM request with fresh project context.
    No LLM conversation state is persisted between calls.

    Request:
        {
            "prompt": "Explain OCR correction",
            "session_mode": "stateless",  # default; only mode supported
            "conversation_id": null,      # rejected in stateless mode
            "include_context": true,      # default true; retrieve project context
            "llm_url": "...",             # optional, falls back to INTELLIGRAPH_LLM_URL env
            "llm_token": "...",           # optional, falls back to INTELLIGRAPH_LLM_TOKEN env
            "model": "gpt-4o-mini",       # optional, falls back to INTELLIGRAPH_LLM_MODEL env
            "metadata": {"source": "n8n"} # optional, pass-through
        }

    Response:
        {
            "answer": "...",
            "model": "gpt-4o-mini",
            "session_mode": "stateless",
            "trace_id": "req_...",
            "conversation_reused": false,
            "context_used": true,
            "context_stats": {},
            "path_warnings": [],
            "usage": {}
        }
    """
    data = request.get_json(force=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt required"}), 400

    # Session mode: only stateless is supported
    session_mode = data.get("session_mode", "stateless")
    if session_mode != "stateless":
        return jsonify({"error": "unsupported_session_mode", "session_mode": session_mode,
                        "supported_modes": ["stateless"]}), 400

    # conversation_id rejected in stateless mode to avoid ambiguity
    if data.get("conversation_id") is not None:
        return jsonify({"error": "conversation_id not supported in stateless mode",
                        "session_mode": session_mode}), 400

    proj = _projects().get(pid)
    if not proj:
        return jsonify({"error": "project not found"}), 404

    # LLM provider: request body > env vars > default
    llm_url = data.get("llm_url") or os.environ.get("INTELLIGRAPH_LLM_URL") or ""
    llm_token = data.get("llm_token") or os.environ.get("INTELLIGRAPH_LLM_TOKEN") or ""
    model = data.get("model") or os.environ.get("INTELLIGRAPH_LLM_MODEL") or "gpt-4o-mini"

    if not llm_url:
        return jsonify({"error": "llm_url required (set INTELLIGRAPH_LLM_URL env var or pass in body)",
                        "session_mode": session_mode}), 400

    host = urlparse(llm_url).hostname
    if host not in ALLOWED_LLM_HOSTS:
        return jsonify({"error": "provider not allowed", "session_mode": session_mode}), 403

    # Retrieve fresh context per request (default: yes for automation)
    include_context = data.get("include_context")
    if include_context is None:
        include_context = True
    retrieved = ""
    context_stats = {}
    if include_context and proj.get("graphify_data"):
        try:
            from retrieval import retrieve_context
            result = retrieve_context(proj, prompt)
            retrieved = result.get("context", "")
            context_stats = result.get("context_stats", {})
        except Exception as e:
            app.logger.warning("Completions context retrieval failed: %s", e)

    # Build fresh LLM messages: system + context + prompt
    system_msg = (
        "You are a code analysis assistant. "
        "Answer questions about the project based solely on the provided context. "
        "Cite file paths when referencing code."
    )
    messages = [{"role": "system", "content": system_msg}]
    if retrieved:
        messages.append({"role": "user", "content": f"Project context:\n{retrieved}\n\nQuestion: {prompt}"})
    else:
        messages.append({"role": "user", "content": prompt})

    # No previous messages appended — this is a clean completion
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 4096,
        "temperature": 0.3,
    }

    headers = {"Content-Type": "application/json"}
    if llm_token:
        headers["Authorization"] = f"Bearer {llm_token}"
    if host == "openrouter.ai":
        headers["HTTP-Referer"] = "https://localhost"
        headers["X-Title"] = "Intelligraph"

    trace_id = f"req_{uuid.uuid4().hex[:12]}"

    try:
        resp = requests.post(llm_url, json=payload, headers=headers, timeout=60)
        resp.encoding = "utf-8"
        if resp.status_code != 200:
            return jsonify({
                "error": f"LLM returned {resp.status_code}",
                "detail": resp.text[:1000],
                "trace_id": trace_id,
                "session_mode": session_mode,
                "conversation_reused": False,
            }), 502
        body = resp.json()
        choices = body.get("choices", [])
        if not choices:
            return jsonify({
                "error": "empty LLM response",
                "trace_id": trace_id,
                "session_mode": session_mode,
                "conversation_reused": False,
            }), 502
        answer = choices[0].get("message", {}).get("content", "")
        path_warnings = _verify_paths(pid, answer) or []
        return jsonify({
            "answer": answer,
            "model": model,
            "session_mode": session_mode,
            "trace_id": trace_id,
            "conversation_reused": False,
            "context_used": bool(retrieved),
            "context_stats": context_stats if retrieved else {},
            "path_warnings": path_warnings,
            "usage": body.get("usage", {}),
        })
    except requests.exceptions.Timeout:
        return jsonify({
            "error": "LLM request timed out",
            "trace_id": trace_id,
            "session_mode": session_mode,
            "conversation_reused": False,
        }), 504
    except Exception as e:
        return jsonify({
            "error": str(e),
            "trace_id": trace_id,
            "session_mode": session_mode,
            "conversation_reused": False,
        }), 502

@app.route("/")
def index():
    # Serve React production build if available
    react_dist = os.path.join(os.path.dirname(__file__), "..", "dist")
    react_index = os.path.join(react_dist, "index.html")
    if os.path.isfile(react_index):
        return send_file(react_index)
    return render_template("index.html", oidc_configured=bool(OIDC_ISSUER))


@app.route("/assets/<path:filename>")
def serve_react_assets(filename):
    react_dist = os.path.join(os.path.dirname(__file__), "..", "dist", "assets")
    return send_from_directory(react_dist, filename)


@app.route("/status")
def status():
    pid = request.args.get("project_id", type=int)
    proj = _projects().get(pid) if pid else None
    return jsonify({
        "oidc_configured": bool(OIDC_ISSUER),
        "downloads": {"mcp_server": "/download/mcp-server",
                      "graph_builder": "/download/graph-builder"},
        "project": proj,
        "projects": list(_projects().keys()),
    })

@app.route("/diagnostics")
def diagnostics():
    """Check critical dependencies for clone pipeline."""
    import shutil
    result = {"status": {}, "errors": []}

    # Git
    git_path = shutil.which("git")
    result["status"]["git"] = git_path or "NOT FOUND"

    # graphify CLI
    g_path = shutil.which("graphify")
    result["status"]["graphify_cli"] = g_path or "NOT FOUND"

    # code-review-graph CLI
    c_path = shutil.which("code-review-graph")
    result["status"]["code_review_graph_cli"] = c_path or "NOT FOUND"

    # Python imports
    for mod in ["graphify", "code_review_graph", "bb_auth", "nx_adapter", "retrieval", "merger", "planner"]:
        try:
            __import__(mod)
            result["status"][f"import_{mod}"] = "ok"
        except Exception as e:
            result["status"][f"import_{mod}"] = f"FAILED: {str(e)[:100]}"
            result["errors"].append(f"Import {mod} failed: {e}")

    # pip list (truncated to relevant packages)
    try:
        r = subprocess.run(["pip", "list", "--format=columns"], capture_output=True, text=True, timeout=10)
        relevant = [l for l in r.stdout.split("\n") if any(x in l.lower() for x in ["graphify", "code-review", "tree-sitter", "flask", "requests"])]
        result["pip_packages"] = relevant
    except Exception as e:
        result["pip_packages"] = [f"pip list failed: {e}"]

    # Data directories
    result["status"]["repo_dir"] = REPO_DIR
    result["status"]["repo_dir_exists"] = os.path.exists(REPO_DIR)
    result["status"]["temp_dir"] = TEMP_DIR
    result["status"]["temp_dir_exists"] = os.path.exists(TEMP_DIR)

    result["healthy"] = len(result["errors"]) == 0
    return jsonify(result)


# ── Entry point ──────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5050)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--oidc-issuer")
    p.add_argument("--oidc-client-id")
    p.add_argument("--oidc-client-secret")
    args = p.parse_args()

    if args.oidc_issuer:
        OIDC_ISSUER = args.oidc_issuer
        OIDC_CLIENT_ID = args.oidc_client_id or ""
        OIDC_CLIENT_SECRET = args.oidc_client_secret or ""
        fetch_oidc_config()

    # (_projects_ref wired at module level above)

    print(f"OIDC:      {'configured' if OIDC_ISSUER else 'disabled'}")
    print(f"Downloads: /download/mcp-server, /download/graph-builder")
    print(f"LLM relay: /llm/relay")
    print(f"Server:    http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)