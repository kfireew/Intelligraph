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
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse

import re
import requests
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, send_from_directory, session,
                   stream_with_context, url_for)

# ── App setup ────────────────────────────────────────────────────

TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
DOWNLOADS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app = Flask(__name__, template_folder=TEMPLATES,
            static_folder=STATIC, static_url_path="/static")
TEMP_DIR = os.environ.get("INTELLIGRAPH_TEMP", os.path.join(tempfile.gettempdir(), "intelligraph"))
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
        safe = {k: v for k, v in proj.items() if k not in ("_G", "graphify_data", "crg_db_path", "repo_dir", "graph_html_path")}
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
    "LLM_ALLOWED_HOSTS", "ai-services.ai.idf.cts,openrouter.ai"
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
    headers["HTTP-Referer"] = "https://localhost"
    headers["X-Title"] = "Intelligraph"

    try:
        resp = requests.post(llm_url, json=payload, headers=headers, timeout=30)
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
                buffer = ""
                for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
                    if not chunk:
                        continue
                    buffer += chunk
                    while "\n\n" in buffer:
                        frame, buffer = buffer.split("\n\n", 1)
                        for line in frame.split("\n"):
                            if line.startswith("data: ") and "[DONE]" not in line:
                                try:
                                    d = json.loads(line[6:])
                                    delta = d.get("choices", [{}])[0].get("delta", {}).get("content") or ""
                                    if delta:
                                        full_text += delta
                                        yield f"data: {json.dumps({'event': 'token', 'data': {'text': delta}})}\n\n"
                                except (json.JSONDecodeError, KeyError):
                                    pass
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


@app.route("/llm/classify", methods=["POST"])
def llm_classify():
    """Classify user prompt intent via keyword matching."""
    data = request.get_json(force=True)
    prompt = (data.get("prompt", "") or "").strip().lower()
    if not prompt:
        return jsonify({"intent": "architecture", "target": "", "confidence": 0.5})
    # Client-side intentDetector.js is authoritative; server fallback
    kw = prompt.lower()
    if any(w in kw for w in ["test", "tested", "coverage"]):
        intent = "tests"
    elif any(w in kw for w in ["structure", "architecture", "overview", "summary", "explain project"]):
        intent = "architecture"
    elif any(w in kw for w in ["how", "work", "works", "flow", "pipeline"]):
        intent = "how_works"
    elif any(w in kw for w in ["who calls", "caller", "callers", "who uses"]):
        intent = "callers"
    elif any(w in kw for w in ["calls", "call", "depends", "callee", "callees", "who does"]):
        intent = "callees"
    elif any(w in kw for w in ["impact", "blast", "radius", "affected", "change"]):
        intent = "impact"
    else:
        intent = "architecture"
    return jsonify({"intent": intent, "target": prompt, "confidence": 0.5})


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


# ── Online MCP (optional) ────────────────────────────────────────

# MCP tokens are short random strings — one per uploaded graph.
# Claude Code sends ?token=xxx with every MCP request, so multiple
# users can have different graphs on the same pod endpoint.
mcp_tokens = {}  # token → {"crg_db": path, "graphify": dict}
_project_tokens = {}  # token → (user_key, pid)  — works without Flask session


@app.route("/mcp/upload", methods=["POST"])
def mcp_upload():
    """Upload graph files. Returns an MCP token + URL for Claude Code .mcp.json."""
    if "graph_file" not in request.files:
        return jsonify({"error": "no file uploaded"}), 400

    f = request.files["graph_file"]
    ftype = request.form.get("type", "")  # "crg" or "graphify"

    # Get or create token for this browser session
    token = session.get("mcp_token")
    if not token:
        token = secrets.token_urlsafe(12)
        session["mcp_token"] = token

    graphs = mcp_tokens.setdefault(token, {})

    if ftype == "crg":
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.save(tmp.name)
        graphs["crg_db"] = tmp.name
        graphs["graphify"] = graphs.get("graphify")  # keep if already uploaded
    elif ftype == "graphify":
        try:
            graphs["graphify"] = json.loads(f.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return jsonify({"error": f"Invalid JSON: {str(e)}"}), 400
        graphs["crg_db"] = graphs.get("crg_db")  # keep if already uploaded
    elif ftype == "html":
        tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False)
        f.save(tmp.name)
        graphs["html"] = tmp.name
    else:
        return jsonify({"error": "type must be 'crg', 'graphify', or 'html'"}), 400

    endpoint = f"{request.url_root.rstrip('/')}/mcp/message?token={token}"
    return jsonify({
        "status": "ok",
        "type": ftype,
        "token": token,
        "endpoint": endpoint,
    })


@app.route("/mcp/clear", methods=["POST"])
def mcp_clear():
    """Clear uploaded graph from online MCP session."""
    token = session.pop("mcp_token", None)
    if token and token in mcp_tokens:
        g = mcp_tokens.pop(token)
        if g.get("crg_db") and os.path.exists(g["crg_db"]):
            os.unlink(g["crg_db"])
    return jsonify({"status": "cleared"})


@app.route("/download/mcp-config")
def download_mcp_config():
    """Download a ready-to-use .mcp.json for online MCP with the user's token."""
    token = session.get("mcp_token")
    if not token:
        return jsonify({"error": "Upload graph files first to get a token"}), 400

    endpoint = f"{request.url_root.rstrip('/')}/mcp/message?token={token}"
    config = {
        "mcpServers": {
            "graphify-qa": {
                "comment": "Online MCP — graph uploaded via the graphify-qa web UI. Runs on the pod. Graph gone on pod restart.",
                "url": endpoint,
                "transport": "http",
            }
        }
    }
    return Response(
        json.dumps(config, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=.mcp.json"}
    )


# Include the MCP blueprint from mcp_server.py
import mcp_server
import code_chunker
app.register_blueprint(mcp_server.mcp_bp)

# Module-level wiring — must execute in ALL startup modes (app.run, gunicorn, WSGI)
mcp_server.mcp_tokens = mcp_tokens
mcp_server._projects_ref = _PROJECTS


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

    oidc_token = session.get("oidc_access_token", "")
    name = data.get("name") or _name_from_url(git_url)
    pid = _next_pid()
    proj = {"name": name, "git_url": git_url, "status": "cloning", "nodes": 0, "edges": 0}

    if clone_type in ("bitbucket", "git") and git_url:
        proj["status"] = "cloning"
        proj["crg_nodes"] = 0
        _projects()[pid] = proj

        try:
            repo_dir = tempfile.mkdtemp(prefix=f"intelligraph-clone-{_user_key()}-{pid}-", dir=TEMP_DIR)
            if os.path.exists(repo_dir):
                shutil.rmtree(repo_dir, ignore_errors=True)
            os.makedirs(repo_dir, exist_ok=True)

            # Any git URL — GitHub, Bitbucket, GitLab
            clone_url = git_url
            if "bitbucket" in git_url and oidc_token:
                host = git_url.split("://", 1)[-1].split("/", 1)[0]
                path = git_url.split("://", 1)[-1].split("/", 1)[1]
                clone_url = f"https://x-token-auth:{oidc_token}@{host}/{path}"

            proj["status"] = "building"
            r = subprocess.run(["git", "clone", "--depth", "1", clone_url, repo_dir],
                             capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                _projects().pop(pid, None)  # don't persist failed clones
                return jsonify({"error": r.stderr[:500]}), 500
            _save_project(pid, proj)  # persist immediately on clone success

            r = subprocess.run(["graphify", "update", "."], cwd=repo_dir,
                              capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                app.logger.warning("graphify update failed (rc=%d): %s",
                                   r.returncode, r.stderr[:200])
            r = subprocess.run(["code-review-graph", "build"], cwd=repo_dir,
                              capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                app.logger.warning("code-review-graph build failed (rc=%d): %s",
                                   r.returncode, r.stderr[:200])

            # Parse results
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
                app.logger.warning("CRG build failed (rc=%d): %s", r.returncode, r.stderr[:300])

            # Generate graph.html from graphify_data
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

            proj["status"] = "ready"
            proj["repo_dir"] = repo_dir
            _save_project(pid, proj)

        except Exception as e:
            proj["status"] = "error"
            proj["error"] = str(e)[:500]
            return jsonify(proj), 500

    elif clone_type == "upload":
        proj["status"] = "pending_upload"
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
#sidebar {
    background: rgba(10,10,10,0.95) !important;
    border-left: 1px solid rgba(255,255,255,0.06) !important;
    width: 280px !important;
    min-width: 280px !important;
    font-family: 'Space Grotesk', 'DM Sans', sans-serif !important;
    position: relative !important;
    overflow: hidden !important;
}
#intelligraph-sidebar-toggle {
    position: absolute !important;
    top: 8px !important;
    right: 8px !important;
    z-index: 200 !important;
    width: 28px !important;
    height: 28px !important;
    border-radius: 6px !important;
    border: 1px solid rgba(255,255,255,0.06) !important;
    background: rgba(10,10,10,0.85) !important;
    color: #8b949e !important;
    cursor: pointer !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    font-size: 18px !important;
    padding: 0 !important;
    user-select: none !important;
}
#intelligraph-sidebar-toggle:hover {
    background: rgba(255,255,255,0.06) !important;
    color: #c9d1d9 !important;
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
    var sb = document.getElementById('sidebar');
    if (!sb) return;

    // Create toggle button inside #graph
    var btn = document.createElement('button');
    btn.id = 'intelligraph-sidebar-toggle';
    btn.textContent = '\u00D7';
    btn.title = 'Close sidebar';

    var graphDiv = document.getElementById('graph');
    (graphDiv || document.body).appendChild(btn);

    var closed = false;
    btn.addEventListener('click', function() {
      closed = !closed;
      if (closed) {
        sb.style.display = 'none';
        btn.textContent = '\u2630';
        btn.title = 'Open sidebar';
      } else {
        sb.style.display = '';
        btn.textContent = '\u00D7';
        btn.title = 'Close sidebar';
      }
    });

// Graph theme: force background after vis.js renders
    // Theme: force Intelligraph backgrounds (runs once after DOM ready)
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

    # Add Google Fonts link for Space Grotesk + DM Sans
    FONTS = '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=DM+Sans:opsz@9..40&family=Space+Grotesk:wght@400;500;600&display=swap" rel="stylesheet">'
    html = html.replace("<head>", "<head>\n" + FONTS)

    return html, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache, no-store, must-revalidate"}



@app.route("/projects/<int:pid>/upload-data", methods=["POST"])
def project_upload_data(pid):
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"error": "not found"}), 404
    f = request.files.get("graph_file")
    if not f:
        return jsonify({"error": "no file"}), 400
    ftype = request.form.get("type", "")
    if ftype == "graphify":
        try:
            data = json.loads(f.read().decode("utf-8"))
            proj["graphify_data"] = data
            proj["nodes"] = len(data.get("nodes", []))
            proj["edges"] = len(data.get("links", []))
            # Auto-generate graph.html from uploaded JSON
            try:
                import graphify
                import graphify.export as gf_export
                G = graphify.build_from_json(data)
                proj["_G"] = G  # cached for in-process queries
                if G and G.number_of_nodes() > 0:
                    # Build communities from node data (node's 'community' field)
                    comms = {}
                    for nid, ndata in G.nodes(data=True):
                        cid = ndata.get('community', 0)
                        if cid not in comms:
                            comms[cid] = []
                        comms[cid].append(nid)
                    html_path = f"{TEMP_DIR}/intelligraph-gf-html-{_user_key()}-{pid}-{int(time.time())}.html"
                    gf_export.to_html(G, comms, html_path)
                    proj["graph_html_path"] = html_path
            except Exception as e:
                print(f"graph.html generation warning: {e}", file=sys.stderr)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return jsonify({"error": str(e)}), 400
    elif ftype == "crg":
        dest = f"{TEMP_DIR}/intelligraph-crg-{_user_key()}-{pid}.db"
        f.save(dest)
        proj["crg_db_path"] = dest
        try:
            with sqlite3.connect(dest) as conn:
                cn = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                try:
                    ce = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
                except sqlite3.OperationalError:
                    ce = 0
            proj["nodes"] = max(proj.get("nodes", 0), cn)
            proj["edges"] = max(proj.get("edges", 0), ce)
        except Exception as e:
            print(f"CRG count warning: {e}", file=sys.stderr)
    elif ftype == "html":
        dest = f"{TEMP_DIR}/intelligraph-gf-html-{_user_key()}-{pid}.html"
        f.save(dest)
        proj["graph_html_path"] = dest
        proj["has_manual_html"] = True
    else:
        return jsonify({"error": "type must be 'crg', 'graphify', or 'html'"}), 400
    if proj.get("status") == "pending_upload":
        if proj.get("crg_db_path") or proj.get("graphify_data"):
            proj["status"] = "ready"
    _save_project(pid, proj)
    return jsonify({"id": pid, "status": proj["status"],
                    "nodes": proj.get("nodes", 0), "edges": proj.get("edges", 0)})


@app.route("/projects/<int:pid>/mcp-token")
def project_mcp_token(pid):
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"token": ""}), 200
    if proj.get("status") != "ready":
        return jsonify({"error": "project not ready"}), 400
    token = secrets.token_urlsafe(16)
    _project_tokens[token] = (_user_key(), pid)
    endpoint = f"{request.url_root.rstrip('/')}/mcp/message?project_token={token}"
    return jsonify({
        "token": token,
        "endpoint": endpoint,
        "config": {"mcpServers": {"graphify-qa": {
            "url": endpoint,
            "transport": "http",
            "comment": f"Project: {proj['name']}"
        }}}
    })


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


@app.route("/projects/<int:pid>/graphify-query", methods=["POST"])
def project_graphify_query(pid):
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"result": ""}), 200
    G = proj.get("_G")
    if not G:
        return jsonify({"error": "no graph data available for this project"}), 400

    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt required"}), 400

    try:
        from graphify.serve import _query_graph_text
        result = _query_graph_text(G, prompt, mode="bfs", depth=2, token_budget=best_context_limit(prompt))
        return jsonify({"result": result})
    except Exception as e:
        return jsonify({"error": str(e)[:500]}), 500


@app.route("/projects/<int:pid>/graphify-explain", methods=["POST"])
def project_graphify_explain(pid):
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"result": ""}), 200
    G = proj.get("_G")
    if not G:
        return jsonify({"error": "no graph data available for this project"}), 400

    data = request.get_json(silent=True) or {}
    concept = (data.get("concept") or "").strip()
    if not concept:
        return jsonify({"error": "concept required"}), 400

    try:
        from graphify.serve import _find_node
        from graphify.build import edge_data
        matches = _find_node(G, concept)
        if not matches:
            return jsonify({"result": f"No node matching '{concept}' found."})
        nid = matches[0]
        d = G.nodes[nid]
        lines = [f"Node: {d.get('label', nid)}"]
        lines.append(f"  ID:        {nid}")
        sf = f"{d.get('source_file', '')} {d.get('source_location', '')}".rstrip()
        if sf:
            lines.append(f"  Source:    {sf}")
        if d.get("file_type"):
            lines.append(f"  Type:      {d['file_type']}")
        if d.get("community"):
            lines.append(f"  Community: {d['community']}")
        lines.append(f"  Degree:    {G.degree(nid)}")
        conns = []
        for nb in G.successors(nid):
            conns.append(("out", nb, edge_data(G, nid, nb)))
        for nb in G.predecessors(nid):
            conns.append(("in", nb, edge_data(G, nb, nid)))
        if conns:
            conns.sort(key=lambda c: G.degree(c[1]), reverse=True)
            lines.append(f"\nConnections ({len(conns)}):")
            for direction, nb, edata in conns[:20]:
                rel = edata.get("relation", "")
                arrow = "-->" if direction == "out" else "<--"
                lines.append(f"  {arrow} {G.nodes[nb].get('label', nb)} [{rel}]")
            if len(conns) > 20:
                lines.append(f"  ... and {len(conns) - 20} more")
        return jsonify({"result": "\n".join(lines)})
    except Exception as e:
        return jsonify({"error": str(e)[:500]}), 500


@app.route("/projects/<int:pid>/graphify-path", methods=["POST"])
def project_graphify_path(pid):
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"result": ""}), 200
    G = proj.get("_G")
    if not G:
        return jsonify({"error": "no graph data available for this project"}), 400

    data = request.get_json(silent=True) or {}
    a = (data.get("a") or "").strip()
    b = (data.get("b") or "").strip()
    if not a or not b:
        return jsonify({"error": "both 'a' and 'b' required"}), 400

    try:
        from graphify.serve import _score_nodes
        from graphify.build import edge_data
        import networkx as nx
        src_scored = _score_nodes(G, [t.lower() for t in a.split()])
        tgt_scored = _score_nodes(G, [t.lower() for t in b.split()])
        if not src_scored:
            return jsonify({"result": f"No node matching '{a}' found."})
        if not tgt_scored:
            return jsonify({"result": f"No node matching '{b}' found."})
        src_nid, tgt_nid = src_scored[0][1], tgt_scored[0][1]
        if src_nid == tgt_nid:
            return jsonify({"result": f"'{a}' and '{b}' both resolved to '{src_nid}'. Use more specific labels."})
        try:
            path_nodes = nx.shortest_path(G.to_undirected(as_view=True), src_nid, tgt_nid)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return jsonify({"result": f"No path found between '{a}' and '{b}'."})
        hops = len(path_nodes) - 1
        lines = [f"Path ({hops} hops):\n"]
        for i in range(len(path_nodes) - 1):
            u, v = path_nodes[i], path_nodes[i + 1]
            if G.has_edge(u, v):
                edata = edge_data(G, u, v)
                forward = True
            else:
                edata = edge_data(G, v, u)
                forward = False
            rel = edata.get("relation", "")
            if i == 0:
                lines.append(f"  {G.nodes[u].get('label', u)}")
            arrow = f"--{rel}-->" if forward else f"<--{rel}--"
            lines.append(f"  {arrow} {G.nodes[v].get('label', v)}")
        return jsonify({"result": "\n".join(lines)})
    except Exception as e:
        return jsonify({"error": str(e)[:500]}), 500

@app.route("/projects/<int:pid>/graphify-affected", methods=["POST"])
def project_graphify_affected(pid):
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"result": ""}), 200
    G = proj.get("_G")
    if not G:
        return jsonify({"error": "no graph data available for this project"}), 400

    data = request.get_json(silent=True) or {}
    target = (data.get("target") or "").strip()
    if not target:
        return jsonify({"error": "target required"}), 400
    depth = min(data.get("depth", 2), 5)

    try:
        from graphify.affected import format_affected
        output = format_affected(G, target, depth=depth)
        return jsonify({"result": output})
    except Exception as e:
        return jsonify({"error": str(e)[:500]}), 500

@app.route("/projects/<int:pid>/code-chunks", methods=["POST"])
def project_code_chunks(pid):
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"chunks": []}), 200

    data = request.get_json(silent=True) or {}
    file_paths = data.get("file_paths") or []
    if not file_paths:
        return jsonify({"error": "file_paths required"}), 400

    repo_dir = proj.get("repo_dir")
    if repo_dir and os.path.isdir(repo_dir):
        try:
            chunks = code_chunker.chunk_files(file_paths, repo_dir=repo_dir, max_chunks=50)
            return jsonify({"chunks": chunks})
        except Exception as e:
            return jsonify({"error": str(e)[:500]}), 500

    # Upload projects: extract from graphify_data nodes
    MAX_SNIPPET_CHARS = 3000
    gf = proj.get("graphify_data") or {}
    chunks = []
    path_set = set(file_paths)
    for n in gf.get("nodes", []):
        sf = n.get("source_file") or n.get("file_path") or ""
        if sf not in path_set:
            continue
        source = n.get("source") or n.get("content") or ""
        if not source:
            continue
        chunks.append({
            "file_path": sf,
            "name": n.get("name") or n.get("label", ""),
            "start_line": n.get("line_start", 1),
            "end_line": n.get("line_end", min(n.get("line_start", 1) + min(len(source.split("\n")), 50), 9999)),
            "content": source[:MAX_SNIPPET_CHARS],
        })
        if len(chunks) >= 50:
            break
    return jsonify({"chunks": chunks})


@app.route("/projects/<int:pid>/chat-context", methods=["POST"])
def project_chat_context(pid):
    """Return pre-assembled Hackathon-format context for LLM chat."""
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"context": ""}), 200


    data = request.get_json(force=True) or {}
    prompt = data.get("prompt", "")[:200]

    graphify_data = proj.get("graphify_data")
    if not graphify_data:
        return jsonify({"context": ""}), 200
    app.logger.info("chat-context pid=%s graphify_nodes=%s", pid, len(graphify_data.get("nodes", [])))

    parts = []
    parts.append("You are an expert code analyst. Use ONLY the code graph data provided below (nodes, edges, communities, file paths). NEVER reference README.md, markdown files, or documentation — those are not in the graph. Cite only actual source file paths from the graph data (e.g., src/main.py, gui/components.py). Be precise.")

    # Codebase structure overview
    all_files = sorted(set(n.get("source_file", "") for n in graphify_data.get("nodes", []) if n.get("source_file")))
    if all_files:
        structure = "## Codebase Structure\n"
        for f in all_files[:20]:
            structure += f"- `{f}`\n"
        parts.append(structure)

    # Stage 1: graphify BFS traversal
    G = proj.get("_G")
    if not G:
        try:
            import graphify
            G = graphify.build_from_json(graphify_data)
            proj["_G"] = G
        except Exception as e:
            app.logger.warning("build G failed: %s", e)

    if G and G.number_of_nodes() > 0 and prompt:
        try:
            from graphify.serve import _query_graph_text
            bfs_text = _query_graph_text(G, prompt, mode="bfs", depth=2, token_budget=2000)
            if bfs_text and bfs_text != "No matching nodes found.":
                parts.append(f"## Architecture Context\n{bfs_text}")
        except Exception as e:
            app.logger.warning("graphify BFS failed: %s", e)

    # Stage 2: CRG SQLite LIKE substring search
    crg_db = proj.get("crg_db_path")
    crg_matches = []
    if crg_db and os.path.exists(crg_db) and prompt:
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{crg_db}?mode=ro", uri=True)
            terms = [t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", prompt) if len(t) > 2][:5]
            for term in terms:
                rows = conn.execute(
                    "SELECT DISTINCT name, kind, file_path, line_start FROM nodes WHERE kind != 'File' AND name LIKE ? LIMIT 3",
                    (f"%{term}%",)
                ).fetchall()
                for row in rows:
                    crg_matches.append({"name": row[0], "kind": row[1], "file_path": row[2], "line_start": row[3]})
            conn.close()
            if crg_matches:
                crg_text = "## Matching Functions\n"
                seen = set()
                for m in crg_matches[:15]:
                    key = f"{m['name']}:{m['file_path']}"
                    if key not in seen:
                        seen.add(key)
                        crg_text += f"- `{m['name']}` ({m['kind']}) — {m['file_path']}:{m['line_start']}\n"
                parts.append(crg_text)
        except Exception as e:
            app.logger.warning("CRG search failed: %s", e)

    # Stage 3: Direct node content search
    if prompt:
        query_lower = prompt.lower()
        node_hits = []
        nodes = graphify_data.get("nodes", [])
        for n in nodes:
            label = (n.get("label", "") or n.get("id", "")).lower()
            content = (n.get("content") or n.get("text") or "").lower()[:500]
            source = (n.get("source_file", "") or "").lower()
            score = 0
            for word in query_lower.split():
                if len(word) > 2 and word in label:
                    score += 3
                if len(word) > 2 and word in source:
                    score += 2
                if len(word) > 2 and word in content:
                    score += 1
            if score > 0:
                node_hits.append((score, n))
        node_hits.sort(key=lambda x: x[0], reverse=True)
        if node_hits:
            content_text = "## Relevant Code\n"
            seen_f = set()
            for _, n in node_hits[:8]:
                fpath = n.get("source_file", "") or ""
                label = n.get("label", n.get("id", ""))
                if fpath not in seen_f:
                    seen_f.add(fpath)
                    code = (n.get("content") or n.get("text") or "")[:2000]
                    lang = fpath.split(".")[-1] if fpath else ""
                    content_text += f"### {fpath} — `{label}`\n```{lang}\n{code}\n```\n"
            parts.append(content_text)

    # User query
    if prompt:
        parts.append(f"## User Query\n{prompt}")

    # Inject actual file content when results are thin -- use graph-identified files
    repo_dir = proj.get("repo_dir")
    if repo_dir and len(parts) <= 3:
        key_files = []
        # Collect files from graph-identified sources (structure list + node matches)
        if crg_matches:
            for m in crg_matches[:5]:
                fp = m.get("file_path", "")
                if fp and fp not in key_files:
                    key_files.append(fp)
        if "node_hits" in dir() and node_hits:
            for _, n in node_hits[:5]:
                fp = n.get("source_file", "")
                if fp and fp not in key_files:
                    key_files.append(fp)
        if not key_files:
            key_files = all_files[:3]
        if key_files:
            fc_text = "## File Contents\n"
            for f in key_files[:5]:
                fp = os.path.join(repo_dir, f)
                if os.path.exists(fp):
                    try:
                        with open(fp, encoding="utf-8", errors="replace") as fh:
                            fc = fh.read()[:3000]
                        lang = f.split(".")[-1] if "." in f else ""
                        fc_text += f"### {f}\n```{lang}\n{fc}\n```\n"
                    except Exception:
                        pass
            if "### " in fc_text:
                parts.append(fc_text)

    context = "\n\n".join(parts)
    return jsonify({"context": context})

@app.route("/projects/<int:pid>/file-content", methods=["GET"])
def project_file_content(pid):
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"error": "not found"}), 404
    repo_dir = proj.get("repo_dir")
    if not repo_dir:
        return jsonify({"error": "no repo directory — upload-based projects don't support file reads"}), 400

    file_path = request.args.get("path", "").strip()
    if not file_path:
        return jsonify({"error": "path query parameter required"}), 400

    # Security: prevent directory traversal
    safe = os.path.normpath(file_path).lstrip(os.sep).lstrip("\\")
    if ".." in safe.split(os.sep):
        return jsonify({"error": "invalid path"}), 400

    full = os.path.join(repo_dir, safe)
    if not os.path.isfile(full):
        return jsonify({"error": f"file not found: {safe}"}), 404

    try:
        start = request.args.get("start", 1, type=int)
        end = request.args.get("end", 0, type=int)
        with open(full, "r", encoding="utf-8", errors="replace") as f_input:
            file_lines = f_input.readlines()
        if end <= 0 or end > len(file_lines):
            end = len(file_lines)
        content = "".join(file_lines[start - 1:end])
        return jsonify({"path": safe, "content": content, "start": start, "end": end, "total_lines": len(file_lines)})
    except Exception as e:
        return jsonify({"error": str(e)[:500]}), 500

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

    # (mcp_tokens and _projects_ref wired at module level above)

    print(f"OIDC:      {'configured' if OIDC_ISSUER else 'disabled'}")
    print(f"Downloads: /download/mcp-server, /download/graph-builder")
    print(f"MCP:       /mcp/message, /mcp/upload, /mcp/clear")
    print(f"LLM relay: /llm/relay")
    print(f"Server:    http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)