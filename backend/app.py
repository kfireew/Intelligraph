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

# ── SSL: closed-network internal CAs ────────────────────────────
# Internal services (LLM, SSO) use certs signed by internal CAs.
# Disable verification by default; override with LLM_SSL_VERIFY=true.
LLM_SSL_VERIFY = os.environ.get("LLM_SSL_VERIFY", "false").lower() == "true"
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

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

def _load_projects(uk=None):
    """Load projects from SQLite on startup."""
    if uk is None:
        return
    try:
        _PROJECTS.setdefault(uk, {})
        conn = _db_conn()
        rows = conn.execute("SELECT id, data FROM projects WHERE user_key = ?", (uk,)).fetchall()
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
        OIDC_CONFIG = requests.get(url, timeout=10, verify=LLM_SSL_VERIFY).json()
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
        return jsonify({"error": "Cannot reach SSO provider. Check that OIDC_ISSUER is accessible from the container."}), 503
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
    try:
        token_resp = requests.post(OIDC_CONFIG["token_endpoint"], data={
            "grant_type": "authorization_code", "code": request.args.get("code"),
            "redirect_uri": url_for("auth_callback", _external=True),
            "client_id": OIDC_CLIENT_ID, "client_secret": OIDC_CLIENT_SECRET,
        }, timeout=10, verify=LLM_SSL_VERIFY).json()
        access_token = token_resp.get("access_token")
        if not access_token:
            return f"Token error: {token_resp.get('error_description', 'unknown')}", 400
        userinfo = requests.get(OIDC_CONFIG["userinfo_endpoint"],
                                headers={"Authorization": f"Bearer {access_token}"},
                                timeout=10, verify=LLM_SSL_VERIFY).json()
    except requests.exceptions.Timeout:
        return "SSO provider timed out during callback", 504
    except requests.exceptions.ConnectionError:
        return "Cannot reach SSO provider during callback", 502
    except Exception as e:
        return f"SSO callback error: {str(e)[:200]}", 502
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

ALLOWED_LLM_HOSTS = set(h.strip() for h in os.environ.get(
    "LLM_ALLOWED_HOSTS", "models.ai-services.idf.cts"
).split(",") if h.strip())

@app.route("/llm/ask", methods=["POST"])
def llm_ask():
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
        resp = requests.post(llm_url, json=payload, headers=headers, timeout=int(os.environ.get("INTELLIGRAPH_LLM_TIMEOUT", "120")), verify=LLM_SSL_VERIFY)
        resp.encoding = "utf-8"
        return jsonify({"status": resp.status_code, "body": resp.text})
    except requests.exceptions.Timeout:
        return jsonify({"error": "LLM request timed out"}), 504
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Cannot reach LLM provider"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 502


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
        resp = requests.get(models_url, headers=headers, timeout=15, verify=LLM_SSL_VERIFY)
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
                    "has_crg": bool(p.get("crg_db_path") and os.path.exists(p.get("crg_db_path", ""))),
                    "git_url": p.get("git_url", "")}
                   for pid, p in _projects().items()])


# ── Git helpers ────────────────────────────────────────────────────

def _git_auth_args(access_token=None):
    """Build git -c arguments for SSL + Bearer auth, matching manual working command.

    Returns list like:
      ["-c", "http.sslVerify=false", "-c", "http.extraHeader=Authorization: Bearer <token>"]
    """
    args = ["-c", "http.sslVerify=false"]
    if access_token:
        args += ["-c", f"http.extraHeader=Authorization: Bearer {access_token}"]
    return args


def _git_env():
    """Minimal git env — no token in here, just suppress interactive prompts."""
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def redact_secret(text, token=None):
    """Replace token or BBDC- patterns with [REDACTED]."""
    import re as _re
    result = text
    if token and token in result:
        result = result.replace(token, "[REDACTED]")
    result = _re.sub(r"BBDC-[A-Za-z0-9+/=_-]+", "[REDACTED]", result)
    result = _re.sub(r"Authorization: Bearer\s+\S+", "Authorization: Bearer [REDACTED]", result)
    return result


def _clean_remote_url(repo_dir):
    """Ensure no token leaked into git remote origin URL."""
    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_dir, capture_output=True, text=True, timeout=10, env=_git_env(),
        )
        if r.returncode == 0:
            url = r.stdout.strip()
            if "BBDC-" in url or "x-token-auth:" in url or "://" in url and "@" in url.split("://", 1)[-1]:
                cleaned = url.split("://", 1)[0] + "://" + url.split("://", 1)[-1].split("@", 1)[-1]
                subprocess.run(
                    ["git", "remote", "set-url", "origin", cleaned],
                    cwd=repo_dir, capture_output=True, timeout=10, env=_git_env(),
                )
                app.logger.warning("Cleaned embedded credentials from remote origin in %s", repo_dir)
    except Exception:
        pass  # non-fatal

@app.route("/projects/clone", methods=["POST"])
def clone_project():
    try:
        data = request.get_json(force=True)
        git_url = data.get("git_url", "").strip()
        clone_type = data.get("type", "bitbucket")
        access_token = data.get("access_token")
        if access_token is not None:
            access_token = access_token.strip() or None
        name = data.get("name") or _name_from_url(git_url)
        pid = _next_pid()
        proj = {"name": name, "git_url": git_url, "status": "cloning", "nodes": 0, "edges": 0}

        if not git_url and clone_type != "upload":
            return jsonify({"error": "git_url required (GitHub or Bitbucket)"}), 400

        if clone_type in ("bitbucket", "git") and git_url:
            proj["status"] = "cloning"
            proj["crg_nodes"] = 0
            _projects()[pid] = proj
            repo_dir = os.path.join(REPO_DIR, f"{_user_key()}-{pid}-{uuid.uuid4().hex[:12]}")
            os.makedirs(repo_dir)

            auth_mode = data.get("auth_mode")
            use_bearer = auth_mode == "bitbucket_datacenter_bearer"

            if use_bearer and access_token is None:
                return jsonify({"error": "missing_repo_credentials", "message": "Provide a Bitbucket Data Center HTTP access token for Bearer auth."}), 400

            # ── Dry-run: return redacted command shape, no git calls ──
            if data.get("dry_run"):
                _projects().pop(pid, None)
                redacted_token = access_token if (use_bearer and access_token) else None
                git_auth = _git_auth_args(access_token=redacted_token)
                def _redact_cmd(cmd_list):
                    return [redact_secret(c, redacted_token) for c in cmd_list]
                preflight_cmd = _redact_cmd(["git"] + git_auth + ["ls-remote", git_url])
                clone_cmd = _redact_cmd(["git"] + git_auth + ["clone", "--depth", "1", git_url, "<repo_dir>"])
                result = {
                    "ok": True, "dry_run": True,
                    "git_url": git_url,
                    "auth_mode": auth_mode or None,
                    "token_present": access_token is not None,
                    "token_prefix": access_token[:5] + "..." if access_token else None,
                    "token_length": len(access_token) if access_token else 0,
                    "preflight_cmd_redacted": preflight_cmd,
                    "clone_cmd_redacted": clone_cmd,
                    "git_terminal_prompt": "0",
                }
                return jsonify(result)

            # ── Auth mode ──
            # Build git -c args (matches working manual command)
            git_auth = _git_auth_args(access_token=access_token if use_bearer else None)
            git_env = _git_env()

            # Clone URL stays clean — token goes in http.extraHeader, not the URL
            clone_url = git_url
            if not use_bearer and "bitbucket" in git_url.lower():
                # Backward compat: URL-embedding fallback
                use_token = access_token or session.get("oidc_access_token", "")
                if use_token:
                    try:
                        host = git_url.split("://", 1)[-1].split("/", 1)[0]
                        path = git_url.split("://", 1)[-1].split("/", 1)[1]
                        clone_url = f"https://x-token-auth:{use_token}@{host}/{path}"
                    except (ValueError, IndexError):
                        app.logger.warning("Could not parse git URL for token embedding")

            r = subprocess.run(["git"] + git_auth + ["ls-remote", clone_url],
                             capture_output=True, text=True, timeout=30, env=git_env)
            if r.returncode != 0:
                _projects().pop(pid, None)
                err = (r.stderr or "").lower()
                if use_bearer and ("401" in err or "403" in err or "authentication" in err or "access denied" in err or "could not read" in err):
                    return jsonify({"error": "bitbucket_auth_failed", "message": "Bitbucket rejected the Bearer token. Check repo read permission."}), 401
                if "not found" in err or "could not read" in err:
                    return jsonify({"error": "repo_not_found_or_no_access", "message": "Repository was not found or the token does not have access."}), 500
                if "certificate" in err or "tls" in err or "ssl" in err or "verify" in err:
                    return jsonify({"error": "git_tls_ca_untrusted", "message": "Git SSL certificate verification error (http.sslVerify=false is set). The Bitbucket server SSL certificate may be misconfigured. Contact your infrastructure team."}), 500
                return jsonify({"error": "clone_failed", "message": redact_secret(r.stderr[:500], access_token)}), 500

            # ── Clone (same -c flags) ──
            proj["status"] = "building"
            r = subprocess.run(["git"] + git_auth + ["clone", "--depth", "1", clone_url, repo_dir],
                             capture_output=True, text=True, timeout=120, env=git_env)
            if r.returncode != 0:
                _projects().pop(pid, None)
                return jsonify({"error": "clone_failed", "message": redact_secret(r.stderr[:500], access_token)}), 500

            # Scrub any leaked token from remote origin
            _clean_remote_url(repo_dir)
            _save_project(pid, proj)

            # Build graphs (shared with pull endpoint)
            _build_graphs(pid, proj, repo_dir)

            proj["status"] = "ready"
            proj["repo_dir"] = repo_dir
            _save_project(pid, proj)

        _projects()[pid] = proj
        # Return lightweight response — graph data is fetched separately via /graph-data
        return jsonify({
            "id": pid,
            "name": proj.get("name"),
            "status": proj.get("status"),
            "nodes": proj.get("nodes", 0),
            "edges": proj.get("edges", 0),
            "has_graphify": bool(proj.get("graphify_data")),
            "has_crg": bool(proj.get("crg_db_path") and os.path.exists(proj.get("crg_db_path", ""))),
            "workspace_type": proj.get("workspace_type", "standard"),
        })

    except Exception as e:
        import traceback
        app.logger.warning("Clone error [%s]: %s\n%s", _user_key(), str(e)[:500], traceback.format_exc())
        return jsonify({"error": redact_secret(str(e)[:500], access_token)}), 500


def _build_graphs(pid, proj, repo_dir):
    """Run graphify + CRG build, parse results, generate HTML — shared by clone and pull."""
    # graphify update
    graphify_env = {**os.environ, "GRAPHIFY_MAX_WORKERS": os.environ.get("GRAPHIFY_MAX_WORKERS", "4")}
    try:
        r = subprocess.run(["graphify", "update", "."], cwd=repo_dir,
                         capture_output=True, text=True, timeout=300, env=graphify_env)
        if r.returncode != 0:
            app.logger.warning("graphify update failed (rc=%d): %s", r.returncode, r.stderr[:200])
    except subprocess.TimeoutExpired:
        app.logger.warning("graphify update timed out after 300s — continuing with partial data")
    except FileNotFoundError:
        app.logger.warning("graphify CLI not found — skipping graph build")

    # code-review-graph build
    crg_env = {**os.environ, "CRG_PARSE_WORKERS": os.environ.get("CRG_PARSE_WORKERS", "4")}
    try:
        r = subprocess.run(["code-review-graph", "build"], cwd=repo_dir,
                          capture_output=True, text=True, timeout=300, env=crg_env)
        if r.returncode != 0:
            app.logger.warning("code-review-graph build failed (rc=%d): %s", r.returncode, r.stderr[:200])
    except subprocess.TimeoutExpired:
        app.logger.warning("code-review-graph build timed out after 300s — continuing with partial data")
    except FileNotFoundError:
        app.logger.warning("code-review-graph CLI not found — skipping CRG build")

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

    # Generate graph.html
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

    # Nx workspace detection
    try:
        from nx_adapter import extract_nx_context
        nx_ctx = extract_nx_context(repo_dir)
        if nx_ctx.get("available"):
            proj["nx_metadata"] = {k: v for k, v in nx_ctx.items() if k != "raw"}
            proj["nx_raw"] = nx_ctx.get("raw", {})
            proj["workspace_type"] = "nx"
            proj["nx_available"] = True
        else:
            proj["workspace_type"] = "standard"
            proj["nx_available"] = False
    except Exception as e:
        proj["workspace_type"] = "standard"
        proj["nx_available"] = False
        app.logger.warning("Nx detection failed (non-fatal): %s", str(e)[:200])
    if "nx_metadata" not in proj:
        proj["nx_metadata"] = {}


@app.route("/projects/<int:pid>/pull", methods=["POST"])
def pull_project(pid):
    """Pull latest from git and rebuild graph + CRG for an existing cloned project."""
    try:
        proj = _projects().get(pid)
        if not proj:
            return jsonify({"error": "project not found"}), 404
        repo_dir = proj.get("repo_dir")
        git_url = proj.get("git_url", "")
        if not repo_dir or not os.path.isdir(repo_dir):
            return jsonify({"error": "repo_dir missing — cannot pull. Re-clone the project."}), 400
        if not git_url:
            return jsonify({"error": "not a cloned project (no git_url)"}), 400

        proj["status"] = "pulling"
        _save_project(pid, proj)

        git_env = _git_env()
        # Use stored access token if available for auth
        access_token = session.get("oidc_access_token", "")
        git_auth = _git_auth_args(access_token=access_token) if access_token else []

        # git pull — fetch + reset to origin/HEAD (shallow clone has no local branch tracking)
        r = subprocess.run(["git"] + git_auth + ["fetch", "--depth", "1", "origin"],
                         capture_output=True, text=True, timeout=120, env=git_env, cwd=repo_dir)
        if r.returncode != 0:
            proj["status"] = "ready"
            _save_project(pid, proj)
            return jsonify({"error": "pull_failed", "message": redact_secret(r.stderr[:500], access_token)}), 500
        r = subprocess.run(["git", "reset", "--hard", "origin/HEAD"],
                         capture_output=True, text=True, timeout=60, env=git_env, cwd=repo_dir)
        if r.returncode != 0:
            proj["status"] = "ready"
            _save_project(pid, proj)
            return jsonify({"error": "pull_failed", "message": redact_secret(r.stderr[:500], access_token)}), 500

        # Rebuild graphs (shared logic)
        _build_graphs(pid, proj, repo_dir)

        proj["status"] = "ready"
        _save_project(pid, proj)
        _projects()[pid] = proj

        return jsonify({
            "id": pid,
            "name": proj.get("name"),
            "status": proj.get("status"),
            "nodes": proj.get("nodes", 0),
            "edges": proj.get("edges", 0),
            "has_graphify": bool(proj.get("graphify_data")),
            "has_crg": bool(proj.get("crg_db_path") and os.path.exists(proj.get("crg_db_path", ""))),
            "workspace_type": proj.get("workspace_type", "standard"),
        })
    except Exception as e:
        import traceback
        app.logger.warning("Pull error [%s]: %s\n%s", _user_key(), str(e)[:500], traceback.format_exc())
        try:
            proj = _projects().get(pid)
            if proj:
                proj["status"] = "ready"
                _save_project(pid, proj)
        except Exception:
            pass
        return jsonify({"error": str(e)[:500]}), 500


@app.route("/projects/<int:pid>", methods=["GET"])
def get_project(pid):
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"error": "project not found"}), 404
    return jsonify({"id": pid, "name": proj.get("name"), "status": proj.get("status"),
                    "nodes": proj.get("nodes", 0), "edges": proj.get("edges", 0),
                    "has_graphify": bool(proj.get("graphify_data")),
                    "has_crg": bool(proj.get("crg_db_path") and os.path.exists(proj.get("crg_db_path", ""))),
                    "workspace_type": proj.get("workspace_type", "standard")})


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
    try:
        result = retrieve_context(proj, prompt)
    except Exception as e:
        app.logger.warning("retrieve_context failed: %s", e, exc_info=True)
        result = {"context": "", "files": [], "strategy": "retrieval_error", "plan": {},
                  "matched_nodes": [], "context_stats": {"error": str(e)[:200]}}
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
        "You are an expert software architect helping a developer understand a codebase. "
        "Give a direct, concise answer. Do not output your thinking process or say \"Let me analyze\" -- just answer. "
        "Use the provided context as your only source of truth. Mention specific file paths. "
        "If context is insufficient, state what is missing. "
        "Do not invent files, functions, imports, or APIs. Format file references as a markdown list with newlines."
    )
    messages = [{"role": "system", "content": system_msg}]
    if retrieved:
        messages.append({"role": "user", "content": f"Project context:\n{retrieved}\n\nQuestion: {prompt}"})
    else:
        messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 4096,
        "temperature": 0.2,
    }

    headers = {"Content-Type": "application/json"}
    if llm_token:
        headers["Authorization"] = f"Bearer {llm_token}"
    if host == "openrouter.ai":
        headers["HTTP-Referer"] = "https://localhost"
        headers["X-Title"] = "Intelligraph"

    trace_id = f"req_{uuid.uuid4().hex[:12]}"

    try:
        resp = requests.post(llm_url, json=payload, headers=headers, timeout=int(os.environ.get("INTELLIGRAPH_LLM_TIMEOUT", "120")), verify=LLM_SSL_VERIFY)
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
    react_dist = os.path.join(os.path.dirname(__file__), "..", "dist")
    react_index = os.path.join(react_dist, "index.html")
    if os.path.isfile(react_index):
        return send_file(react_index)
    return jsonify({"error": "Frontend build (dist/) not found. Build with npm run build."}), 503


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
    print(f"LLM relay: /llm/ask")
    print(f"Server:    http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)