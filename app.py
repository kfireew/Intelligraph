"""
graphify-qa: thin pod server. Serves static UI, handles OIDC SSO, relays LLM calls,
provides optional online MCP, and serves downloadable tools (MCP server, graph builder).

Usage: python app.py [--port 5050]
"""

import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, session, url_for)

# ── App setup ────────────────────────────────────────────────────

TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
DOWNLOADS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app = Flask(__name__, template_folder=TEMPLATES,
            static_folder=STATIC, static_url_path="/static")
TEMP_DIR = os.environ.get("INTELLISCAN_TEMP", os.path.join(tempfile.gettempdir(), "intelliscan"))
os.makedirs(TEMP_DIR, exist_ok=True)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

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
        return session.get("oidc_sub", u["name"])
    return session.get("_anon_key") or _init_anon()


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

@app.route("/llm/relay", methods=["POST"])
def llm_relay():
    """Relay LLM requests — forwards user's LLM call through the pod."""
    data = request.get_json(force=True)
    llm_url = data.get("url", "").strip()
    llm_token = data.get("token", "").strip()
    payload = data.get("payload", {})

    if not llm_url:
        return jsonify({"error": "llm_url required"}), 400

    headers = {"Content-Type": "application/json"}
    if llm_token:
        headers["Authorization"] = f"Bearer {llm_token}"

    try:
        resp = requests.post(llm_url, json=payload, headers=headers, timeout=30)
        return jsonify({"status": resp.status_code, "body": resp.text[:8000]})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


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
    else:
        return jsonify({"error": "type must be 'crg' or 'graphify'"}), 400

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
app.register_blueprint(mcp_server.mcp_bp)

# Module-level wiring — must execute in ALL startup modes (app.run, gunicorn, WSGI)
mcp_server.mcp_tokens = mcp_tokens
mcp_server._projects_ref = _PROJECTS


# ── Project management ───────────────────────────────────────────

def _name_from_url(url):
    m = __import__('re').search(r"/([^/]+?)(?:\.git)?$", url.rstrip("/"))
    return m.group(1) if m else url


@app.route("/projects", methods=["GET"])
def list_projects():
    return jsonify([
        {"id": pid, "name": p["name"], "status": p.get("status", "unknown"),
         "git_url": p.get("git_url", ""), "nodes": p.get("nodes", 0),
         "edges": p.get("edges", 0)}
        for pid, p in sorted(_projects().items())
    ])


@app.route("/projects/clone", methods=["POST"])
def clone_project():
    data = request.get_json(force=True)
    git_url = data.get("git_url", "").strip()
    clone_type = data.get("type", "bitbucket")  # "bitbucket" or "upload"

    if not git_url and clone_type == "bitbucket":
        return jsonify({"error": "git_url required"}), 400

    oidc_token = session.get("oidc_access_token", "")
    name = data.get("name") or _name_from_url(git_url)
    pid = _next_pid()
    proj = {"name": name, "git_url": git_url, "status": "cloning", "nodes": 0, "edges": 0}

    if clone_type == "bitbucket" and git_url:
        proj["status"] = "cloning"
        _projects()[pid] = proj

        try:
            repo_dir = f"{TEMP_DIR}/intelliscan-clone-{_user_key()}-{pid}"
            os.makedirs(repo_dir, exist_ok=True)

            # Build clone URL with OIDC token
            host = git_url.split("://", 1)[-1].split("/", 1)[0]
            path = git_url.split("://", 1)[-1].split("/", 1)[1]
            if oidc_token:
                auth_url = f"https://x-token-auth:{oidc_token}@{host}/{path}"
            else:
                auth_url = git_url

            proj["status"] = "building"
            r = subprocess.run(["git", "clone", "--depth", "1", auth_url, repo_dir],
                             capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                proj["status"] = "error"
                proj["error"] = r.stderr[:500]
                return jsonify(proj), 500

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
                proj["edges"] = max(proj["edges"], ce)

            proj["status"] = "ready"
            proj["repo_dir"] = repo_dir

        except Exception as e:
            proj["status"] = "error"
            proj["error"] = str(e)[:500]
            return jsonify(proj), 500

    elif clone_type == "upload":
        proj["status"] = "pending_upload"
        _projects()[pid] = proj

    return jsonify({"id": pid, **proj})


@app.route("/projects/<int:pid>", methods=["DELETE"])
def delete_project(pid):
    proj = _projects().pop(pid, None)
    if proj and proj.get("repo_dir"):
        import shutil
        shutil.rmtree(proj["repo_dir"], ignore_errors=True)
    return jsonify({"status": "deleted"})


@app.route("/projects/<int:pid>/status")
def project_status(pid):
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"error": "not found"}), 404
    return jsonify({"id": pid, "status": proj["status"], "name": proj["name"],
                    "nodes": proj.get("nodes", 0), "edges": proj.get("edges", 0),
                    "error": proj.get("error", "")})


@app.route("/projects/<int:pid>/graph-data")
def project_graph_data(pid):
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"error": "not found"}), 404
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
        return jsonify({"error": "not found"}), 404
    crg_path = proj.get("crg_db_path")
    if not crg_path or not os.path.exists(crg_path):
        return jsonify({"error": "no CRG DB available"}), 404
    return send_file(crg_path, as_attachment=False,
                     download_name=f"project-{pid}.db",
                     mimetype="application/octet-stream")


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
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return jsonify({"error": str(e)}), 400
    elif ftype == "crg":
        dest = f"{TEMP_DIR}/intelliscan-crg-{_user_key()}-{pid}.db"
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
    else:
        return jsonify({"error": "type must be 'crg' or 'graphify'"}), 400
    if proj.get("status") == "pending_upload":
        if proj.get("crg_db_path") or proj.get("graphify_data"):
            proj["status"] = "ready"
    return jsonify({"id": pid, "status": proj["status"],
                    "nodes": proj.get("nodes", 0), "edges": proj.get("edges", 0)})


@app.route("/projects/<int:pid>/mcp-token")
def project_mcp_token(pid):
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"error": "not found"}), 404
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


# ── UI ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", oidc_configured=bool(OIDC_ISSUER))


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