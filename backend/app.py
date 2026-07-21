"""
graphify-qa: thin pod server. Serves static UI, handles SSO (PKCE), relays LLM calls,
provides optional online MCP, and serves downloadable tools (MCP server, graph builder).

Usage: python app.py [--port 5050]
"""

import base64
import hashlib
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
from flask import (Flask, Response, g, jsonify, redirect, render_template,
                   request, send_file, send_from_directory, session,
                   stream_with_context, url_for)

# ── Network mode: "closed" (default) or "open" ────────────────────
# closed = internal LLM hosts, SSL verify off, git sslVerify off (for closed network)
# open   = openrouter.ai + GitHub, SSL verify on, git sslVerify on (for public internet)
# Individual settings can still be overridden via their own env vars.
NETWORK_MODE = os.environ.get("INTELLIGRAPH_NETWORK_MODE", "closed").lower()

if NETWORK_MODE == "open":
    _DEFAULT_SSL_VERIFY = "true"
    _DEFAULT_ALLOWED_HOSTS = "openrouter.ai"
    _DEFAULT_GIT_SSL_VERIFY = "true"
else:
    _DEFAULT_SSL_VERIFY = "false"
    _DEFAULT_ALLOWED_HOSTS = "models.ai-services.idf.cts"
    _DEFAULT_GIT_SSL_VERIFY = "false"

# ── SSL: closed-network internal CAs ────────────────────────────
LLM_SSL_VERIFY = os.environ.get("LLM_SSL_VERIFY", _DEFAULT_SSL_VERIFY).lower() == "true"
GIT_SSL_VERIFY = os.environ.get("INTELLIGRAPH_GIT_SSL_VERIFY", _DEFAULT_GIT_SSL_VERIFY).lower() == "true"

# ── Site URL: used by GuidePanel to generate MCP configs. When set (e.g.
#    INTELLIGRAPH_SITE_URL=https://intelligraph.corp), the GuidePanel uses this
#    instead of window.location.origin — important for os4 routes / private links.
SITE_URL = os.environ.get("INTELLIGRAPH_SITE_URL", "")

# ── Verbose console logging ────────────────────────────────────
# Prints step-by-step progress to stdout so you can see exactly where
# the pipeline is (or where it got stuck). On by default.
# Set INTELLIGRAPH_VERBOSE=false to silence.
VERBOSE = os.environ.get("INTELLIGRAPH_VERBOSE", "true").lower() == "true"


def _vmsg(msg, *args):
    """Print a timestamped progress message to stdout (if VERBOSE)."""
    if not VERBOSE:
        return
    ts = datetime.now().strftime("%H:%M:%S")
    if args:
        try:
            msg = msg % args
        except Exception:
            pass
    print(f"[{ts}] {msg}", flush=True)
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
ARTIFACTS_DIR = os.environ.get("INTELLIGRAPH_ARTIFACTS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "artifacts"))
os.makedirs(REPO_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(ARTIFACTS_DIR, exist_ok=True)


def _rmtree_hard(path):
    """shutil.rmtree that handles Windows read-only .git files."""
    import stat
    def _on_error(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=_on_error)


def _cleanup_orphans():
    """Delete orphaned repo dirs and stale temp files on startup.
    Called lazily on first project access to avoid blocking import."""
    try:
        # Clean orphaned repo dirs (dirs in REPO_DIR not referenced by any project)
        if os.path.isdir(REPO_DIR):
            for entry in os.listdir(REPO_DIR):
                d = os.path.join(REPO_DIR, entry)
                if os.path.isdir(d):
                    _rmtree_hard(d)
        # Clean stale temp files older than 24h
        if os.path.isdir(TEMP_DIR):
            cutoff = time.time() - 86400
            for entry in os.listdir(TEMP_DIR):
                p = os.path.join(TEMP_DIR, entry)
                try:
                    if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                        os.unlink(p)
                except Exception:
                    pass
    except Exception:
        pass

# ── Nx MCP: repo_dir deleted after build; live Nx runs on the MCP server (host) ──
KEEP_REPO_AFTER_BUILD = False

# ── SSO enforcement ──────────────────────────────────────────────
# When true, mutating actions (clone, share, join, pull, delete) require SSO login.
# Read-only routes (GET /projects, graph-html, status) remain open so the UI loads.
REQUIRE_SSO = os.environ.get("INTELLIGRAPH_REQUIRE_SSO", "true").lower() == "true"

# Routes that require authentication when REQUIRE_SSO is true
_SSO_PROTECTED_METHODS = {"POST", "DELETE", "PATCH", "PUT"}
_SSO_PROTECTED_PREFIXES = (
    "/projects/clone",
    "/projects/<int:pid>/share",
    "/share/join",
    "/projects/<int:pid>/pull",
    "/projects/<int:pid>/token",
)
# Routes that are always open (auth, health, static, read-only)
_SSO_OPEN_PREFIXES = (
    "/auth/", "/status", "/diagnostics", "/assets/", "/static/",
    "/download/", "/projects/<int:pid>/graph-html",
    "/projects/<int:pid>/graph-data", "/projects/<int:pid>/crg-db",
)
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
    conn.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, project_id INTEGER, user_key TEXT, data TEXT, created_at TEXT, updated_at TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_proj_user ON conversations(project_id, user_key)")
    conn.execute("CREATE TABLE IF NOT EXISTS uploads (user_key TEXT, project_id INTEGER, type TEXT, data TEXT, UNIQUE(user_key, project_id, type))")
    # Per-user fetch tokens (was per-project; migrated for shared projects)
    conn.execute("CREATE TABLE IF NOT EXISTS fetch_tokens_v2 (project_id INTEGER, user_key TEXT, token TEXT, created_at TEXT, PRIMARY KEY(project_id, user_key))")
    conn.execute("CREATE TABLE IF NOT EXISTS project_members (project_id INTEGER, user_key TEXT, joined_at TEXT, PRIMARY KEY(project_id, user_key))")
    conn.execute("CREATE TABLE IF NOT EXISTS project_share_keys (project_id INTEGER, share_key TEXT PRIMARY KEY, created_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS mcp_tokens (project_id INTEGER, user_key TEXT, token TEXT, created_at TEXT, PRIMARY KEY(project_id, user_key))")
    conn.execute("CREATE TABLE IF NOT EXISTS query_logs (id TEXT PRIMARY KEY, project_id INTEGER, user_key TEXT, prompt TEXT, intent TEXT, strategy TEXT, context_tokens INTEGER, answer_tokens INTEGER, retrieval_mode TEXT, trace_id TEXT, created_at TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qlog_proj ON query_logs(project_id)")
    conn.execute("CREATE TABLE IF NOT EXISTS feedback (id TEXT PRIMARY KEY, project_id INTEGER, user_key TEXT, trace_id TEXT, rating INTEGER, comment TEXT, created_at TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fb_proj ON feedback(project_id)")
    _migrate_fetch_tokens(conn)
    return conn

_db = None  # lazy init

def _db_conn():
    global _db
    if _db is None:
        _db = _get_db()
    return _db


def _enrich_community_labels(graphify_data, crg_db_path):
    """Match graphify communities to CRG communities by file overlap.

    CRG communities have meaningful names (e.g. 'graphify-extract', 'tests-file')
    generated during build. Graphify graph.json only stores integer community IDs.
    Returns {graphify_cid: crg_name} for all matchable communities.
    """
    if not crg_db_path or not os.path.exists(crg_db_path):
        return {}
    try:
        conn = sqlite3.connect(f"file:{crg_db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        from os.path import commonpath
        crg_fps = [r[0] for r in conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE file_path IS NOT NULL"
        ).fetchall()]
        if not crg_fps:
            return {}
        prefix = commonpath(crg_fps)
        crg_comms = {}
        for r in conn.execute(
            "SELECT c.id, c.name, n.file_path FROM communities c "
            "JOIN nodes n ON n.community_id = c.id WHERE n.file_path IS NOT NULL"
        ).fetchall():
            fp = (r["file_path"] or "").replace("\\", "/")
            rel = fp[len(prefix):].lstrip("/").replace("\\", "/")
            crg_comms.setdefault(r["id"], {"name": r["name"], "files": set()})["files"].add(rel)
        conn.close()
    except Exception:
        return {}
    gf_comm_files = {}
    for n in graphify_data.get("nodes", []):
        c = n.get("community")
        sf = (n.get("source_file") or "").replace("\\", "/")
        if c is not None and sf:
            gf_comm_files.setdefault(c, set()).add(sf)
    labels = {}
    for gf_cid, gf_files in gf_comm_files.items():
        best_name = None
        best_overlap = 0
        for data in crg_comms.values():
            overlap = len(gf_files & data["files"])
            if overlap > best_overlap:
                best_overlap = overlap
                best_name = data["name"]
        if best_name and best_overlap > 0:
            labels[gf_cid] = best_name
    return labels


# ── Token encryption (Fernet / AES-128-CBC + HMAC-SHA256) ──────────
_ENCRYPTION_SALT = b"intelligraph-token-encryption-v1"
_fernet = None

def _get_fernet():
    """Get or lazily create the Fernet instance for token encryption."""
    global _fernet
    if _fernet is None:
        from hashlib import pbkdf2_hmac
        import base64
        from cryptography.fernet import Fernet
        secret = app.secret_key.encode() if isinstance(app.secret_key, str) else app.secret_key
        key = base64.urlsafe_b64encode(pbkdf2_hmac("sha256", secret, _ENCRYPTION_SALT, 200_000, 32))
        _fernet = Fernet(key)
    return _fernet

def _encrypt_token(token):
    """Encrypt a token using Fernet (AES-128-CBC + HMAC-SHA256)."""
    if not token:
        return None
    try:
        return _get_fernet().encrypt(token.encode()).decode()
    except Exception as e:
        app.logger.warning("Token encryption failed: %s", e)
        return None

def _decrypt_token(encrypted):
    """Decrypt a Fernet-encrypted token."""
    if not encrypted:
        return None
    try:
        return _get_fernet().decrypt(encrypted.encode()).decode()
    except Exception:
        return None

def _migrate_fetch_tokens(conn):
    """One-time migration: old fetch_tokens table (per-project, XOR) → fetch_tokens_v2 (per-user, Fernet)."""
    try:
        # Check if old table exists
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "fetch_tokens" not in tables:
            return
        # Check if migration already done
        migrated = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fetch_tokens_migrated'").fetchone()
        if migrated:
            return
        rows = conn.execute("SELECT project_id, token, created_at FROM fetch_tokens").fetchall()
        for row in rows:
            try:
                # Old method: XOR with secret key
                old_key = app.secret_key
                plain = _xor_obfuscate(row["token"], old_key)
                # New: encrypt with Fernet
                encrypted = _encrypt_token(plain)
                conn.execute(
                    "INSERT OR REPLACE INTO fetch_tokens_v2(project_id, user_key, token, created_at) VALUES(?, ?, ?, ?)",
                    (row["project_id"], "local", encrypted, row["created_at"] or datetime.now(timezone.utc).isoformat())
                )
            except Exception:
                pass
        conn.execute("CREATE TABLE fetch_tokens_migrated (done INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO fetch_tokens_migrated VALUES (1)")
        conn.commit()
        _vmsg("TOKEN MIGRATION: migrated %d tokens from XOR→Fernet", len(rows))
    except Exception as e:
        app.logger.warning("Token migration failed: %s", e)

def _xor_obfuscate(text, key):
    """Legacy XOR obfuscation (used only for one-time migration)."""
    if not text or not key:
        return text
    result = []
    for i, ch in enumerate(text):
        result.append(chr(ord(ch) ^ ord(key[i % len(key)])))
    return "".join(result)

def _save_project(pid, proj, uk=None):
    """Persist project to SQLite. If uk (user_key) is not provided,
    derives it from the current request context (will fail in background threads)."""
    try:
        if uk is None:
            uk = _user_key()
        conn = _db_conn()
        safe = {k: v for k, v in proj.items() if k not in ("_G", "graph_html_path", "_fetch_token")}
        safe["_has_graphify"] = bool(proj.get("graphify_data"))
        safe["_has_crg"] = bool(proj.get("crg_db_path"))
        safe["_has_html"] = bool(proj.get("graph_html_path"))
        conn.execute("INSERT OR REPLACE INTO projects(id, user_key, data) VALUES(?, ?, ?)",
                     (pid, uk, json.dumps(safe)))
        conn.commit()
    except Exception as e:
        app.logger.warning("DB save failed: %s", e)

def _load_projects(uk=None):
    """Load projects from SQLite on startup (owned + shared)."""
    if uk is None:
        return
    try:
        _PROJECTS.setdefault(uk, {})
        conn = _db_conn()
        # Owned projects
        rows = conn.execute("SELECT id, data FROM projects WHERE user_key = ?", (uk,)).fetchall()
        for row in rows:
            data = json.loads(row["data"])
            if row["id"] not in _projects():
                _projects()[row["id"]] = data
        # Shared projects (member of but not owner)
        shared_rows = conn.execute(
            "SELECT pm.project_id, p.data FROM project_members pm "
            "JOIN projects p ON pm.project_id = p.id "
            "WHERE pm.user_key = ? AND p.user_key != ?",
            (uk, uk)
        ).fetchall()
        for row in shared_rows:
            data = json.loads(row["data"])
            if row["project_id"] not in _projects():
                _projects()[row["project_id"]] = data
                _vmsg("SHARED LOAD user=%s pid=%d", uk, row["project_id"])
    except Exception as e:
        app.logger.warning("DB load failed: %s", e)


def _get_shared_project(pid):
    """Get a project that the current user has access to via share membership.
    Looks in all user_keys' projects if not in own."""
    # Already in current user's projects?
    proj = _projects().get(pid)
    if proj:
        return proj
    # Check if user is a member of this shared project
    try:
        conn = _db_conn()
        uk = _user_key()
        row = conn.execute(
            "SELECT p.data FROM project_members pm "
            "JOIN projects p ON pm.project_id = p.id "
            "WHERE pm.project_id = ? AND pm.user_key = ?",
            (pid, uk)
        ).fetchone()
        if row:
            data = json.loads(row["data"])
            _projects()[pid] = data  # cache it
            return data
    except Exception:
        pass
    return None


def _store_fetch_token(pid, token, uk=None):
    """Store a git access token for sparse-fetch (Fernet-encrypted, per-user)."""
    if not token:
        return
    try:
        if uk is None:
            uk = _user_key()
        encrypted = _encrypt_token(token)
        if not encrypted:
            return
        conn = _db_conn()
        conn.execute(
            "INSERT OR REPLACE INTO fetch_tokens_v2(project_id, user_key, token, created_at) VALUES(?, ?, ?, ?)",
            (pid, uk, encrypted, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    except Exception as e:
        app.logger.warning("Failed to store fetch token: %s", e)

def _load_fetch_token(pid, uk=None):
    """Load and decrypt the stored fetch token for a project+user."""
    try:
        if uk is None:
            uk = _user_key()
        conn = _db_conn()
        row = conn.execute("SELECT token FROM fetch_tokens_v2 WHERE project_id=? AND user_key=?", (pid, uk)).fetchone()
        if row and row["token"]:
            return _decrypt_token(row["token"])
    except Exception as e:
        app.logger.warning("Failed to load fetch token: %s", e)
    return None

def _delete_fetch_token(pid, uk=None):
    """Delete fetch token(s) for a project. If uk is given, only that user's token."""
    try:
        conn = _db_conn()
        if uk is None:
            conn.execute("DELETE FROM fetch_tokens_v2 WHERE project_id=?", (pid,))
        else:
            conn.execute("DELETE FROM fetch_tokens_v2 WHERE project_id=? AND user_key=?", (pid, uk))
        conn.commit()
    except Exception:
        pass

_DEFAULT_SECRET_KEY = "intelligraph-dev-key-do-not-use-in-production"
app.secret_key = os.environ.get("SECRET_KEY", _DEFAULT_SECRET_KEY)

# Validate SECRET_KEY when SSO is enforced (production mode)
if REQUIRE_SSO:
    _sk = os.environ.get("SECRET_KEY", "")
    if not _sk or _sk == _DEFAULT_SECRET_KEY:
        print("FATAL: SECRET_KEY must be set when INTELLIGRAPH_REQUIRE_SSO=true", flush=True)
        sys.exit(1)
    if len(_sk) < 32:
        print("FATAL: SECRET_KEY must be at least 32 characters when SSO is enforced", flush=True)
        sys.exit(1)
else:
    _sk = os.environ.get("SECRET_KEY", "")
    if not _sk or _sk == _DEFAULT_SECRET_KEY:
        _sk = secrets.token_hex(32)
        app.secret_key = _sk
        _vmsg("WARNING: SECRET_KEY not set — generated random key. Tokens will not survive restart.")

SSO_ISSUER = os.environ.get("SSO_ISSUER", "") or os.environ.get("OIDC_ISSUER", "")
SSO_CLIENT_ID = os.environ.get("SSO_CLIENT_ID", "") or os.environ.get("OIDC_CLIENT_ID", "")
SSO_CLIENT_SECRET = os.environ.get("SSO_CLIENT_SECRET", "") or os.environ.get("OIDC_CLIENT_SECRET", "")
SSO_CONFIG = {}

# ── Project storage ───────────────────────────────────────────────
# user_key -> {pid: {name, git_url, status, nodes, edges, crg_db_path, graphify_data, sso_token}}
# user_key is session-based: SSO sub or session ID for anonymous users
_PROJECTS = {}  # {user_key: {pid: project_dict}}
_NEXT_PID = {}   # {user_key: next_pid}`


def _user_key():
    """Stable identifier for the current user across the session."""
    # MCP token-authenticated request: use the token's user_key
    mcp_uk = getattr(g, "mcp_user_key", None)
    if not mcp_uk:
        # SSO guard may not have run (REQUIRE_SSO=false) — check token directly
        token = request.headers.get("X-MCP-Token", "").strip() if request else ""
        if token:
            try:
                conn = _db_conn()
                row = conn.execute(
                    "SELECT user_key FROM mcp_tokens WHERE token = ?", (token,)
                ).fetchone()
                if row:
                    mcp_uk = row["user_key"]
                    g.mcp_user_key = mcp_uk
            except Exception:
                pass
    if mcp_uk:
        # Force reload if empty (projects may have been added after first load)
        if mcp_uk not in _PROJECTS or not _PROJECTS.get(mcp_uk):
            _load_projects(mcp_uk)
        return mcp_uk
    # If an MCP token was provided but invalid, don't fall through to "local"
    # — return a throwaway key with no projects
    mcp_token_present = request.headers.get("X-MCP-Token", "").strip() if request else ""
    if mcp_token_present and not mcp_uk:
        return f"_mcp_invalid_{secrets.token_hex(4)}"
    u = get_user()
    if u and u.get("source") == "sso":
        uk = session.get("sso_sub", u["name"])
    elif SSO_ISSUER:
        # SSO configured but not authenticated - keep session-based for multi-user
        uk = session.get("_anon_key") or _init_anon()
    else:
        # No SSO - single-user mode, use stable key so clearing cookies
        # doesn't orphan projects
        uk = "local"
    # Load persisted projects on first access for this user
    if uk not in _PROJECTS:
        _load_projects(uk)
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


def fetch_sso_config():
    global SSO_CONFIG
    if not SSO_ISSUER:
        return
    try:
        url = f"{SSO_ISSUER.rstrip('/')}/.well-known/openid-configuration"
        SSO_CONFIG = requests.get(url, timeout=10, verify=LLM_SSL_VERIFY).json()
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


def _validate_mcp_token(path):
    """Check if the request carries a valid MCP token in the X-MCP-Token header.

    MCP tokens are per-project, stored in the mcp_tokens table.
    Only allows access to /graph/ endpoints (read-only retrieval).
    Returns the project_id if valid, None otherwise.

    Retries with a fresh DB connection if the cached connection is stale
    (e.g. after Docker restart, WAL lock, concurrent write). This fixes
    persistent 401s where the token IS valid but the query fails silently.
    """
    token = request.headers.get("X-MCP-Token", "").strip()
    if not token:
        return None
    for attempt in range(2):
        try:
            conn = _db_conn()
            row = conn.execute(
                "SELECT project_id, user_key FROM mcp_tokens WHERE token = ?", (token,)
            ).fetchone()
            if row:
                g.mcp_user_key = row["user_key"]
                return row["project_id"]
            return None
        except Exception:
            if attempt == 0:
                global _db
                _db = None
            else:
                return None
    return None


@app.before_request
def _sso_guard():
    """Require SSO login for mutating actions when INTELLIGRAPH_REQUIRE_SSO=true."""
    if not REQUIRE_SSO:
        return None
    if not SSO_ISSUER:
        return None  # SSO not configured, can't enforce
    if request.method not in _SSO_PROTECTED_METHODS:
        return None  # read-only methods allowed
    path = request.path
    # Check if this path is open (no auth needed)
    for prefix in _SSO_OPEN_PREFIXES:
        if path.startswith(prefix.replace("<int:pid>", "").replace("<pid>", "")):
            return None
    # /graph/ endpoints: allow with valid MCP token (X-MCP-Token header)
    if path.startswith("/graph/"):
        pid = _validate_mcp_token(path)
        if pid is not None:
            return None  # valid MCP token — allow
        # No valid MCP token — fall through to SSO check
    # /api/ endpoints: allow with valid MCP token (X-MCP-Token header)
    if path.startswith("/api/"):
        pid = _validate_mcp_token(path)
        if pid is not None:
            return None  # valid MCP token — allow
        # No valid MCP token — fall through to SSO check
    # Mutating route — require SSO auth
    u = get_user()
    if not u:
        if request.path.startswith("/api/"):
            return jsonify({"error": "login_required", "message": "SSO login required for this action.",
                            "login_url": "/auth/login"}), 401
        return jsonify({"error": "login_required", "message": "SSO login required for this action.",
                        "login_url": "/auth/login"}), 401
    return None


@app.route("/auth/login")
def auth_login():
    if not SSO_ISSUER:
        return jsonify({"error": "SSO not configured"}), 400
    if not SSO_CONFIG:
        fetch_sso_config()
    if not SSO_CONFIG:
        return jsonify({"error": "Cannot reach SSO provider. Check that SSO_ISSUER is accessible from the container."}), 503
    state = secrets.token_urlsafe(16)
    session["sso_state"] = state
    params = {
        "client_id": SSO_CLIENT_ID,
        "response_type": "code",
        "scope": "openid profile email",
        "redirect_uri": f"{SITE_URL.rstrip('/')}/auth/callback" if SITE_URL else url_for("auth_callback", _external=True),
        "state": state,
    }
    # PKCE: when no client secret, use code challenge/verifier (RFC 7636)
    if not SSO_CLIENT_SECRET:
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        session["sso_code_verifier"] = code_verifier
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return redirect(f"{SSO_CONFIG['authorization_endpoint']}?{urlencode(params)}")


@app.route("/auth/callback")
def auth_callback():
    if request.args.get("state") != session.pop("sso_state", None):
        return "Invalid state", 400
    try:
        token_data = {
            "grant_type": "authorization_code",
            "code": request.args.get("code"),
            "redirect_uri": f"{SITE_URL.rstrip('/')}/auth/callback" if SITE_URL else url_for("auth_callback", _external=True),
            "client_id": SSO_CLIENT_ID,
        }
        # PKCE: send code_verifier when no client secret; otherwise send secret
        if SSO_CLIENT_SECRET:
            token_data["client_secret"] = SSO_CLIENT_SECRET
        else:
            token_data["code_verifier"] = session.pop("sso_code_verifier", "")
        token_resp = requests.post(SSO_CONFIG["token_endpoint"], data=token_data,
                                   timeout=10, verify=LLM_SSL_VERIFY).json()
        access_token = token_resp.get("access_token")
        if not access_token:
            return f"Token error: {token_resp.get('error_description', 'unknown')}", 400
        userinfo = requests.get(SSO_CONFIG["userinfo_endpoint"],
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
        "source": "sso",
    }
    session["sso_access_token"] = access_token
    session["sso_sub"] = userinfo.get("sub", "")
    return redirect("/")


@app.route("/auth/logout")
def auth_logout():
    session.clear()
    if SSO_CONFIG.get("end_session_endpoint"):
        return redirect(SSO_CONFIG["end_session_endpoint"])
    return redirect("/")


@app.route("/auth/me")
def auth_me():
    u = get_user()
    return jsonify({"authenticated": bool(u), "user": u,
                     "sso_configured": bool(SSO_ISSUER),
                     "login_url": "/auth/login" if SSO_ISSUER else None,
                     "logout_url": "/auth/logout"})


# ── LLM relay ────────────────────────────────────────────────────

ALLOWED_LLM_HOSTS = set(h.strip() for h in os.environ.get(
    "LLM_ALLOWED_HOSTS", _DEFAULT_ALLOWED_HOSTS
).split(",") if h.strip())

VIZ_NODE_LIMIT = int(os.environ.get("INTELLIGRAPH_VIZ_NODE_LIMIT", "5000"))

@app.route("/llm/ask", methods=["POST"])
def llm_ask():
    """Relay LLM requests - forwards user's LLM call through the pod."""
    data = request.get_json(force=True)
    llm_url = data.get("url", "").strip().rstrip("/")
    llm_token = data.get("token", "").strip()
    payload = data.get("payload", {})

    if not llm_url:
        return jsonify({"error": "llm_url required"}), 400

    host = urlparse(llm_url).hostname
    if host not in ALLOWED_LLM_HOSTS:
        print(f"[LLM] BLOCKED host={host} allowed={ALLOWED_LLM_HOSTS}", flush=True)
        return jsonify({"error": "provider not allowed"}), 403

    headers = {"Content-Type": "application/json"}
    if llm_token:
        headers["Authorization"] = f"Bearer {llm_token}"

    try:
        print(f"[LLM] -> URL={llm_url}", flush=True)
        print(f"[LLM] -> model={payload.get('model')!r} msgs={len(payload.get('messages', []))} max_tokens={payload.get('max_tokens')} stream={'stream' in payload} temp={payload.get('temperature')}", flush=True)
        print(f"[LLM] -> payload_json={json.dumps(payload)[:800]}", flush=True)
        resp = requests.post(llm_url, json=payload, headers=headers, timeout=int(os.environ.get("INTELLIGRAPH_LLM_TIMEOUT", "120")), verify=LLM_SSL_VERIFY)
        resp.encoding = "utf-8"
        print(f"[LLM] <- status={resp.status_code} ct={resp.headers.get('content-type','')} body={resp.text[:500]}", flush=True)
        return jsonify({"status": resp.status_code, "body": resp.text})
    except requests.exceptions.Timeout:
        return jsonify({"error": "LLM request timed out"}), 504
    except requests.exceptions.ConnectionError as e:
        print(f"[LLM] CONN_ERR {str(e)[:300]}", flush=True)
        return jsonify({"error": "Cannot reach LLM provider"}), 503
    except Exception as e:
        print(f"[LLM] ERROR {str(e)[:500]}", flush=True)
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

# ── Intent classification REMOVED - client-side only via intentDetector.js ──

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


@app.route("/download/agent")
def download_agent():
    """Download the MCP agent guide (markdown)."""
    agent_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.md")
    return send_file(agent_path, as_attachment=True,
                     download_name="intelligraph-agent.md",
                     mimetype="text/markdown")


@app.route("/download/test-mcp")
def download_test_mcp():
    """Download the MCP connectivity test script."""
    test_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_mcp_conn.py")
    return send_file(test_path, as_attachment=True,
                     download_name="test_mcp_conn.py",
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
    args = ["-c", f"http.sslVerify={'true' if GIT_SSL_VERIFY else 'false'}"]
    if access_token:
        args += ["-c", f"http.extraHeader=Authorization: Bearer {access_token}"]
    return args


def _git_env():
    """Minimal git env - no token in here, just suppress interactive prompts."""
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

        _vmsg("CLONE START pid=%d name=%s url=%s type=%s", pid, name, git_url, clone_type)

        if not git_url and clone_type != "upload":
            return jsonify({"error": "git_url required (GitHub or Bitbucket)"}), 400

        if clone_type == "upload" or (not git_url and clone_type == "upload"):
            proj["status"] = "pending_upload"
            _projects()[pid] = proj
            _save_project(pid, proj)
            _vmsg("CLONE UPLOAD pid=%d - waiting for file upload", pid)

        if clone_type in ("bitbucket", "git") and git_url:
            proj["status"] = "cloning"
            proj["crg_nodes"] = 0
            _projects()[pid] = proj
            repo_dir = os.path.join(REPO_DIR, f"{_user_key()}-{pid}-{uuid.uuid4().hex[:12]}")
            os.makedirs(repo_dir)

            auth_mode = data.get("auth_mode")
            use_bearer = auth_mode == "bitbucket_datacenter_bearer"

            if use_bearer and access_token is None:
                _vmsg("CLONE FAIL pid=%d - bearer mode but no token", pid)
                return jsonify({"error": "missing_repo_credentials", "message": "Provide a Bitbucket Data Center HTTP access token for Bearer auth."}), 400

            # ── Dry-run: return redacted command shape, no git calls ──
            if data.get("dry_run"):
                _vmsg("CLONE DRY-RUN pid=%d - returning command shape only", pid)
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
            _vmsg("CLONE AUTH pid=%d - use_bearer=%s", pid, use_bearer)
            proj["auth_mode"] = auth_mode or ""
            git_auth = _git_auth_args(access_token=access_token if use_bearer else None)
            git_env = _git_env()

            # Clone URL stays clean - token goes in http.extraHeader, not the URL
            clone_url = git_url
            if not use_bearer and "bitbucket" in git_url.lower():
                use_token = access_token or session.get("sso_access_token", "")
                if use_token:
                    try:
                        host = git_url.split("://", 1)[-1].split("/", 1)[0]
                        path = git_url.split("://", 1)[-1].split("/", 1)[1]
                        clone_url = f"https://x-token-auth:{use_token}@{host}/{path}"
                    except (ValueError, IndexError):
                        app.logger.warning("Could not parse git URL for token embedding")

            # ── Preflight: ls-remote ──
            _vmsg("CLONE PREFLIGHT pid=%d - git ls-remote %s", pid, git_url)
            r = subprocess.run(["git"] + git_auth + ["ls-remote", clone_url],
                             capture_output=True, text=True, timeout=30, env=git_env)
            if r.returncode != 0:
                _vmsg("CLONE PREFLIGHT FAIL pid=%d - rc=%d stderr=%s", pid, r.returncode, (r.stderr or "")[:200])
                _projects().pop(pid, None)
                _rmtree_hard(repo_dir)
                err = (r.stderr or "").lower()
                if use_bearer and ("401" in err or "403" in err or "authentication" in err or "access denied" in err or "could not read" in err):
                    return jsonify({"error": "bitbucket_auth_failed", "message": "Bitbucket rejected the Bearer token. Check repo read permission."}), 401
                if "not found" in err or "could not read" in err:
                    return jsonify({"error": "repo_not_found_or_no_access", "message": "Repository was not found or the token does not have access."}), 500
                if "certificate" in err or "tls" in err or "ssl" in err or "verify" in err:
                    return jsonify({"error": "git_tls_ca_untrusted", "message": "Git SSL certificate verification error (http.sslVerify=false is set). The Bitbucket server SSL certificate may be misconfigured. Contact your infrastructure team."}), 500
                return jsonify({"error": "clone_failed", "message": redact_secret(r.stderr[:500], access_token)}), 500
            _vmsg("CLONE PREFLIGHT OK pid=%d", pid)

            # ── Clone ──
            _vmsg("CLONE GIT pid=%d - git clone --depth 1 %s", pid, git_url)
            proj["status"] = "building"
            r = subprocess.run(["git"] + git_auth + ["clone", "--depth", "1", clone_url, repo_dir],
                             capture_output=True, text=True, timeout=120, env=git_env)
            if r.returncode != 0:
                _vmsg("CLONE GIT FAIL pid=%d - rc=%d stderr=%s", pid, r.returncode, (r.stderr or "")[:200])
                _projects().pop(pid, None)
                _rmtree_hard(repo_dir)
                return jsonify({"error": "clone_failed", "message": redact_secret(r.stderr[:500], access_token)}), 500
            _vmsg("CLONE GIT OK pid=%d - repo at %s", pid, repo_dir)

            # Scrub any leaked token from remote origin
            _clean_remote_url(repo_dir)

            # Store fetch token for on-demand sparse fetch (Fernet-encrypted in SQLite)
            fetch_token = access_token or session.get("sso_access_token", "")
            if fetch_token:
                _store_fetch_token(pid, fetch_token, uk=_user_key())
                _vmsg("CLONE TOKEN STORED pid=%d - saved for sparse fetch", pid)

            proj["repo_dir"] = repo_dir
            proj["status"] = "queued"
            _save_project(pid, proj)

            # Capture user_key for worker thread (Flask session not available there)
            uk = _user_key()

            # ── Enqueue build (async via build queue, or sync in TESTING mode) ──
            def _build_job(pid=pid, proj=proj, repo_dir=repo_dir, uk=uk):
                _vmsg("BUILD START pid=%d - graphify + CRG", pid)
                _build_graphs(pid, proj, repo_dir, user_key=uk)
                proj["status"] = "ready"
                _save_project(pid, proj, uk=uk)
                _vmsg("BUILD DONE pid=%d - status=ready nodes=%s edges=%s", pid, proj.get("nodes", 0), proj.get("edges", 0))

            if app.config.get("TESTING"):
                _build_job()
            else:
                from build_queue import build_queue
                build_queue.submit(_build_job)
                _vmsg("BUILD QUEUED pid=%d - enqueued to build_queue", pid)

            # Return immediately - frontend polls /status until ready
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

        _projects()[pid] = proj
        # Return lightweight response - graph data is fetched separately via /graph-data
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


def _tail_log(path, lines=5):
    """Read the last N lines of a log file (for error reporting)."""
    try:
        with open(path, "r", errors="replace") as f:
            all_lines = f.readlines()
            return "".join(all_lines[-lines:])[:500]
    except Exception:
        return "(log unavailable)"


def _build_graphs(pid, proj, repo_dir, user_key=None):
    """Run graphify + CRG build, parse results, generate HTML - shared by clone and pull.
    
    user_key: passed from caller to avoid accessing Flask session in worker threads.
    """
    # graphify update - stream output to temp log file (avoids RAM buffering)
    _vmsg("GRAPHIFY START pid=%d - graphify update . (cwd=%s)", pid, repo_dir)
    graphify_env = {**os.environ, "GRAPHIFY_MAX_WORKERS": os.environ.get("GRAPHIFY_MAX_WORKERS", "4")}
    gf_log = os.path.join(TEMP_DIR, f"graphify-{pid}-{int(time.time())}.log")
    try:
        with open(gf_log, "w") as logf:
            r = subprocess.run(["graphify", "update", "."], cwd=repo_dir,
                             stdout=logf, stderr=subprocess.STDOUT, timeout=300, env=graphify_env)
        if r.returncode != 0:
            _vmsg("GRAPHIFY WARN pid=%d - rc=%d (continuing with partial data)", pid, r.returncode)
            app.logger.warning("graphify update failed (rc=%d): %s", r.returncode, _tail_log(gf_log))
        else:
            _vmsg("GRAPHIFY OK pid=%d", pid)
    except subprocess.TimeoutExpired:
        _vmsg("GRAPHIFY TIMEOUT pid=%d - 300s exceeded (continuing)", pid)
        app.logger.warning("graphify update timed out after 300s - continuing with partial data")
    except FileNotFoundError:
        _vmsg("GRAPHIFY SKIP pid=%d - graphify CLI not found", pid)
        app.logger.warning("graphify CLI not found - skipping graph build")

    # code-review-graph build - stream output to temp log file
    _vmsg("CRG START pid=%d - code-review-graph build (cwd=%s)", pid, repo_dir)

    # Write .code-review-graphignore to exclude build artifacts from indexing
    ignore_path = os.path.join(repo_dir, ".code-review-graphignore")
    try:
        with open(ignore_path, "w", encoding="utf-8") as igf:
            igf.write(
                "# Auto-generated by Intelligraph — excludes build artifacts from CRG index\n"
                "build-resources/**\nbundle/**\ndevtools/**\nredux-dev-tools/**\n"
                "out/**\n.nuxt/**\n.cache/**\n*.chunk.js\n*.bundle.js\n*.pack.js\n"
                "*.dev.js\n*.umd.js\n**/generated/**\n**/codegen/**\n**/__generated__/**\n"
                "*.ngfactory.ts\n*.ngstyle.ts\n*.shim.ngstyle.ts\nwebpack/**\n.vite/**\n"
            )
    except Exception:
        pass

    crg_env = {**os.environ, "CRG_PARSE_WORKERS": os.environ.get("CRG_PARSE_WORKERS", "4")}
    crg_log = os.path.join(TEMP_DIR, f"crg-{pid}-{int(time.time())}.log")
    try:
        with open(crg_log, "w") as logf:
            r = subprocess.run(["code-review-graph", "build"], cwd=repo_dir,
                              stdout=logf, stderr=subprocess.STDOUT, timeout=300, env=crg_env)
        if r.returncode != 0:
            _vmsg("CRG WARN pid=%d - rc=%d (continuing with partial data)", pid, r.returncode)
            app.logger.warning("code-review-graph build failed (rc=%d): %s", r.returncode, _tail_log(crg_log))
        else:
            _vmsg("CRG OK pid=%d", pid)
    except subprocess.TimeoutExpired:
        _vmsg("CRG TIMEOUT pid=%d - 300s exceeded (continuing)", pid)
        app.logger.warning("code-review-graph build timed out after 300s - continuing with partial data")
    except FileNotFoundError:
        _vmsg("CRG SKIP pid=%d - code-review-graph CLI not found", pid)
        app.logger.warning("code-review-graph CLI not found - skipping CRG build")

    # Parse results
    _vmsg("PARSE START pid=%d - reading graph.json + graph.db", pid)
    gf_path = os.path.join(repo_dir, "graphify-out", "graph.json")
    crg_path = os.path.join(repo_dir, ".code-review-graph", "graph.db")

    if os.path.exists(gf_path):
        with open(gf_path) as f:
            proj["graphify_data"] = json.load(f)
        proj["nodes"] = len(proj["graphify_data"].get("nodes", []))
        proj["edges"] = len(proj["graphify_data"].get("links", []))
        _vmsg("PARSE graph.json pid=%d - nodes=%d edges=%d", pid, proj["nodes"], proj["edges"])
    else:
        _vmsg("PARSE graph.json pid=%d - NOT FOUND", pid)

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
        _vmsg("PARSE graph.db pid=%d - crg_nodes=%d crg_edges=%d", pid, cn, ce)
    else:
        _vmsg("PARSE graph.db pid=%d - NOT FOUND", pid)

    # ── Store source code snippets in CRG DB for later retrieval ──
    # Reads source files from repo_dir before it's deleted, stores ~500 char
    # snippets per node. Enables code snippets in lightweight path + MCP tools.
    if os.path.exists(crg_path) and repo_dir:
        try:
            snip_conn = sqlite3.connect(crg_path)
            snip_conn.execute("CREATE TABLE IF NOT EXISTS node_snippets (node_name TEXT PRIMARY KEY, snippet TEXT)")
            nodes_with_lines = snip_conn.execute(
                "SELECT name, file_path, line_start, line_end FROM nodes "
                "WHERE line_start IS NOT NULL AND file_path IS NOT NULL AND name IS NOT NULL"
            ).fetchall()
            file_groups = defaultdict(list)
            for n in nodes_with_lines:
                file_groups[n[1]].append(n)
            stored = 0
            for fp, node_list in file_groups.items():
                full_path = fp if os.path.isabs(fp) else os.path.join(repo_dir, fp)
                if not os.path.isfile(full_path):
                    continue
                try:
                    with open(full_path, "r", errors="replace") as src_f:
                        lines = src_f.readlines()
                except Exception:
                    continue
                for n in node_list:
                    start = max(0, (n[2] or 1) - 1)
                    end = min(len(lines), n[3] or start + 20)
                    snippet = "".join(lines[start:end])[:500]
                    if snippet.strip():
                        snip_conn.execute(
                            "INSERT OR REPLACE INTO node_snippets (node_name, snippet) VALUES (?, ?)",
                            (n[0], snippet)
                        )
                        stored += 1
            snip_conn.commit()
            snip_conn.close()
            _vmsg("SNIPPETS pid=%d - stored %d snippets from %d files", pid, stored, len(file_groups))
        except Exception as e:
            _vmsg("SNIPPETS pid=%d - failed: %s", pid, str(e)[:200])

    # Generate graph.html with CRG-enriched community labels
    pre_built_html = os.path.join(repo_dir, "graphify-out", "graph.html") if repo_dir else None
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
                crg_path = proj.get("crg_db_path") or (os.path.join(repo_dir, ".code-review-graph", "graph.db") if repo_dir else None)
                community_labels = _enrich_community_labels(proj["graphify_data"], crg_path)
                for cid in comms:
                    if cid not in community_labels or not community_labels[cid]:
                        community_labels[cid] = f"Community {cid}"
                html_path = f"{TEMP_DIR}/intelligraph-gf-html-{user_key or 'unknown'}-{pid}-{int(time.time())}.html"
                gf_export.to_html(G, comms, html_path, community_labels=community_labels, node_limit=VIZ_NODE_LIMIT)
                proj["graph_html_path"] = html_path
                if pre_built_html and os.path.exists(pre_built_html):
                    os.remove(pre_built_html)
        except Exception as e:
            app.logger.warning("graph.html generation failed: %s", e, exc_info=True)

    # Nx workspace detection (uses global nx binary from Docker image - no npm install needed)
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

    # ── Relocate artifacts + delete repo_dir (saves disk + RAM) ──
    _relocate_artifacts(pid, proj, repo_dir)


def _relocate_artifacts(pid, proj, repo_dir):
    """Move graph.json, graph.db, graph.html to ARTIFACTS_DIR and delete repo_dir.

    Skipped when KEEP_REPO_AFTER_BUILD=True (Nx MCP needs node_modules live).
    """
    if not repo_dir or not os.path.isdir(repo_dir):
        _vmsg("RELOCATE SKIP pid=%d - repo_dir missing or not a dir", pid)
        return

    _vmsg("RELOCATE START pid=%d - moving artifacts to %s", pid, ARTIFACTS_DIR)
    artifacts_proj_dir = os.path.join(ARTIFACTS_DIR, str(pid))
    os.makedirs(artifacts_proj_dir, exist_ok=True)

    # Move graph.json
    gf_src = os.path.join(repo_dir, "graphify-out", "graph.json")
    if os.path.exists(gf_src):
        gf_dst = os.path.join(artifacts_proj_dir, "graph.json")
        shutil.move(gf_src, gf_dst)
        proj["graphify_path"] = gf_dst
        _vmsg("RELOCATE graph.json pid=%d - moved to %s", pid, gf_dst)

    # Move graph.db
    crg_src = os.path.join(repo_dir, ".code-review-graph", "graph.db")
    if os.path.exists(crg_src):
        crg_dst = os.path.join(artifacts_proj_dir, "graph.db")
        shutil.move(crg_src, crg_dst)
        proj["crg_db_path"] = crg_dst
        _vmsg("RELOCATE graph.db pid=%d - moved to %s", pid, crg_dst)

    # Move graph.html (pre-built by graphify CLI) — skip if we already generated enriched HTML
    html_src = os.path.join(repo_dir, "graphify-out", "graph.html")
    if os.path.exists(html_src) and not proj.get("graph_html_path"):
        html_dst = os.path.join(artifacts_proj_dir, "graph.html")
        shutil.move(html_src, html_dst)
        proj["graph_html_path"] = html_dst
        _vmsg("RELOCATE graph.html pid=%d - moved to %s", pid, html_dst)

    # Delete repo_dir unless Nx MCP needs it
    if KEEP_REPO_AFTER_BUILD:
        _vmsg("RELOCATE SKIP DELETE pid=%d - keeping repo_dir (NX_MCP=true)", pid)
        app.logger.info("Keeping repo_dir (INTELLIGRAPH_ENABLE_NX_MCP=true): %s", repo_dir)
    else:
        _vmsg("RELOCATE DELETE pid=%d - removing repo_dir %s", pid, repo_dir)
        _rmtree_hard(repo_dir)
        proj["repo_dir"] = None
        _vmsg("RELOCATE DONE pid=%d - artifacts at %s, repo_dir deleted", pid, artifacts_proj_dir)
        app.logger.info("Repo dir deleted after artifact relocation: pid=%d artifacts=%s", pid, artifacts_proj_dir)


@app.route("/projects/<int:pid>/pull", methods=["POST"])
def pull_project(pid):
    """Pull latest from git and rebuild graph + CRG for an existing cloned project.
    
    Accepts optional {branch: "name"} to switch branches.
    If repo_dir was deleted (post-build cleanup), creates a fresh temp clone.
    """
    try:
        proj = _projects().get(pid)
        if not proj:
            return jsonify({"error": "project not found"}), 404
        repo_dir = proj.get("repo_dir")
        git_url = proj.get("git_url", "")
        if not git_url:
            return jsonify({"error": "not a cloned project (no git_url)"}), 400

        data = request.get_json(silent=True) or {}
        target_branch = data.get("branch", "").strip()

        _vmsg("PULL START pid=%d name=%s url=%s branch=%s", pid, proj.get("name"), git_url, target_branch or "(default)")
        proj["status"] = "pulling"
        _save_project(pid, proj)

        git_env = _git_env()
        # Use stored Bitbucket token first (the one provided during clone),
        # then fall back to SSO session token
        access_token = _load_fetch_token(pid, uk=_user_key()) or session.get("sso_access_token", "") or ""
        auth_mode = proj.get("auth_mode", "")
        use_bearer = auth_mode == "bitbucket_datacenter_bearer"
        git_auth = _git_auth_args(access_token=access_token if use_bearer else None) if access_token else []

        # Build clone_url with token embedded for non-bearer auth (same as clone)
        clone_url = git_url
        if not use_bearer and "bitbucket" in git_url.lower() and access_token:
            try:
                host = git_url.split("://", 1)[-1].split("/", 1)[0]
                path = git_url.split("://", 1)[-1].split("/", 1)[1]
                clone_url = f"https://x-token-auth:{access_token}@{host}/{path}"
            except (ValueError, IndexError):
                app.logger.warning("Could not parse git URL for token embedding")

        # If repo_dir was deleted after build, re-clone to a fresh temp dir
        if not repo_dir or not os.path.isdir(repo_dir):
            _vmsg("PULL RE-CLONE pid=%d - repo_dir was deleted, re-cloning", pid)
            repo_dir = os.path.join(REPO_DIR, f"{_user_key()}-{pid}-pull-{uuid.uuid4().hex[:12]}")
            os.makedirs(repo_dir)
            clone_cmd = ["git"] + git_auth + ["clone", "--depth", "1"]
            if target_branch:
                clone_cmd += ["--branch", target_branch]
            clone_cmd += [clone_url, repo_dir]
            r = subprocess.run(clone_cmd,
                             capture_output=True, text=True, timeout=120, env=git_env)
            if r.returncode != 0:
                _vmsg("PULL RE-CLONE FAIL pid=%d - %s", pid, (r.stderr or "")[:200])
                proj["status"] = "ready"
                _save_project(pid, proj)
                _rmtree_hard(repo_dir)
                err_lower = (r.stderr or "").lower()
                if any(p in err_lower for p in ("401", "403", "authentication", "access denied", "could not read", "authorization")):
                    return jsonify({"error": "token_expired_or_invalid", "message": "Your Bitbucket access token has expired or is invalid. Please update it."}), 401
                return jsonify({"error": "pull_failed", "message": redact_secret(r.stderr[:500], access_token)}), 500
            proj["repo_dir"] = repo_dir
            if target_branch:
                proj["branch"] = target_branch
            _vmsg("PULL RE-CLONE OK pid=%d - repo at %s", pid, repo_dir)
        else:
            _vmsg("PULL FETCH pid=%d - git fetch + reset (repo_dir exists)", pid)
            fetch_cmd = ["git"] + git_auth + ["fetch", "--depth", "1", "origin"]
            if target_branch:
                fetch_cmd += [target_branch]
            r = subprocess.run(fetch_cmd,
                             capture_output=True, text=True, timeout=120, env=git_env, cwd=repo_dir)
            if r.returncode != 0:
                _vmsg("PULL FETCH FAIL pid=%d - %s", pid, (r.stderr or "")[:200])
                proj["status"] = "ready"
                _save_project(pid, proj)
                err_lower = (r.stderr or "").lower()
                if any(p in err_lower for p in ("401", "403", "authentication", "access denied", "could not read", "authorization")):
                    return jsonify({"error": "token_expired_or_invalid", "message": "Your Bitbucket access token has expired or is invalid. Please update it."}), 401
                return jsonify({"error": "pull_failed", "message": redact_secret(r.stderr[:500], access_token)}), 500
            reset_ref = f"origin/{target_branch}" if target_branch else "origin/HEAD"
            r = subprocess.run(["git", "reset", "--hard", reset_ref],
                             capture_output=True, text=True, timeout=60, env=git_env, cwd=repo_dir)
            if r.returncode != 0 and not target_branch:
                _vmsg("PULL RESET origin/HEAD FAIL pid=%d - trying default branch", pid)
                r2 = subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                                  capture_output=True, text=True, timeout=10, env=git_env, cwd=repo_dir)
                if r2.returncode == 0:
                    default_branch = r2.stdout.strip().split("origin/")[-1]
                    r = subprocess.run(["git", "reset", "--hard", f"origin/{default_branch}"],
                                     capture_output=True, text=True, timeout=60, env=git_env, cwd=repo_dir)
            if r.returncode != 0:
                _vmsg("PULL RESET FAIL pid=%d - %s", pid, (r.stderr or "")[:200])
                proj["status"] = "ready"
                _save_project(pid, proj)
                return jsonify({"error": "pull_failed", "message": redact_secret(r.stderr[:500], access_token)}), 500
            if target_branch:
                proj["branch"] = target_branch
            _vmsg("PULL FETCH OK pid=%d", pid)

        # Rebuild graphs (shared logic)
        _vmsg("PULL BUILD pid=%d - rebuilding graphs", pid)
        uk = _user_key()
        _build_graphs(pid, proj, repo_dir, user_key=uk)

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


@app.route("/projects/<int:pid>/branches")
def project_branches(pid):
    """List remote branches for a cloned project using git ls-remote."""
    proj = _projects().get(pid) or _get_shared_project(pid)
    if not proj:
        return jsonify({"error": "project not found"}), 404
    git_url = proj.get("git_url", "")
    if not git_url:
        return jsonify({"error": "not a cloned project (no git_url)"}), 400
    try:
        access_token = _load_fetch_token(pid, uk=_user_key()) or session.get("sso_access_token", "") or ""
        auth_mode = proj.get("auth_mode", "")
        use_bearer = auth_mode == "bitbucket_datacenter_bearer"
        git_auth = _git_auth_args(access_token=access_token if use_bearer else None) if access_token else []
        clone_url = git_url
        if not use_bearer and "bitbucket" in git_url.lower() and access_token:
            try:
                host = git_url.split("://", 1)[-1].split("/", 1)[0]
                path = git_url.split("://", 1)[-1].split("/", 1)[1]
                clone_url = f"https://x-token-auth:{access_token}@{host}/{path}"
            except (ValueError, IndexError):
                pass
        r = subprocess.run(["git"] + git_auth + ["ls-remote", "--heads", clone_url],
                         capture_output=True, text=True, timeout=30, env=_git_env())
        if r.returncode != 0:
            err_lower = (r.stderr or "").lower()
            if any(p in err_lower for p in ("401", "403", "authentication", "access denied", "could not read", "authorization")):
                return jsonify({"error": "token_expired_or_invalid", "message": "Your Bitbucket access token has expired or is invalid."}), 401
            return jsonify({"error": "branch_list_failed", "message": redact_secret((r.stderr or "")[:500], access_token)}), 500
        branches = []
        for line in r.stdout.strip().split("\n"):
            if "\trefs/heads/" in line:
                branches.append(line.split("\trefs/heads/")[1])
        return jsonify({"branches": sorted(branches), "current": proj.get("branch", "")})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "branch_list_timeout", "message": "Timed out listing branches."}), 504
    except Exception as e:
        return jsonify({"error": str(e)[:500]}), 500

@app.route("/projects/<int:pid>", methods=["GET"])
def get_project(pid):
    proj = _projects().get(pid) or _get_shared_project(pid)
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
    """Remove the project from the current user's view.
    If no more members remain, clean up artifacts + DB rows for good."""
    _vmsg("DELETE START pid=%d", pid)
    uk = _user_key()
    proj = _projects().pop(pid, None)

    # Remove current user's membership
    try:
        conn = _db_conn()
        conn.execute("DELETE FROM project_members WHERE project_id=? AND user_key=?", (pid, uk))
        conn.execute("DELETE FROM fetch_tokens_v2 WHERE project_id=? AND user_key=?", (pid, uk))
        conn.commit()
    except Exception:
        pass

    # Check if any members remain
    remaining_members = 0
    try:
        row = conn.execute("SELECT COUNT(*) as cnt FROM project_members WHERE project_id=?", (pid,)).fetchone()
        remaining_members = row["cnt"] if row else 0
    except Exception:
        pass

    # Also check if the owner still has it (owner might not be in project_members)
    try:
        owner_row = conn.execute("SELECT 1 FROM projects WHERE id=? AND user_key=?", (pid, uk)).fetchone()
        if owner_row and remaining_members == 0:
            # Owner is deleting their own project — remove from projects table so full cleanup runs
            conn.execute("DELETE FROM projects WHERE id=? AND user_key=?", (pid, uk))
            conn.commit()
    except Exception:
        pass

    if remaining_members == 0:
        # Last user out — clean up everything
        _vmsg("DELETE LAST MEMBER pid=%d - full cleanup", pid)
        if proj:
            if proj.get("repo_dir"):
                _vmsg("DELETE pid=%d - removing repo_dir %s", pid, proj["repo_dir"])
                _rmtree_hard(proj["repo_dir"])
            artifacts_proj_dir = os.path.join(ARTIFACTS_DIR, str(pid))
            _vmsg("DELETE pid=%d - removing artifacts %s", pid, artifacts_proj_dir)
            _rmtree_hard(artifacts_proj_dir)
            # Clean individual artifact files not in artifacts dir (e.g., in TEMP_DIR)
            for path_key in ("graph_html_path", "graphify_path", "crg_db_path"):
                p = proj.get(path_key)
                if p and os.path.exists(p):
                    try:
                        norm_p = os.path.normpath(p)
                        norm_art = os.path.normpath(artifacts_proj_dir)
                        if not norm_p.startswith(norm_art):
                            if os.path.isfile(p):
                                os.unlink(p)
                            elif os.path.isdir(p):
                                _rmtree_hard(p)
                            _vmsg("DELETE pid=%d - removed %s=%s", pid, path_key, p)
                    except Exception as e:
                        _vmsg("DELETE pid=%d - could not remove %s: %s", pid, path_key, e)
            # Clean temp files matching this pid
            if os.path.isdir(TEMP_DIR):
                for entry in os.listdir(TEMP_DIR):
                    if f"-{pid}-" in entry or f"-{pid}." in entry or entry.endswith(f"-{pid}.html"):
                        try:
                            fp = os.path.join(TEMP_DIR, entry)
                            if os.path.isfile(fp):
                                os.unlink(fp)
                            elif os.path.isdir(fp):
                                _rmtree_hard(fp)
                        except Exception:
                            pass
        _delete_fetch_token(pid)  # cleans all remaining tokens
        try:
            conn.execute("DELETE FROM projects WHERE id=?", (pid,))
            conn.execute("DELETE FROM project_share_keys WHERE project_id=?", (pid,))
            conn.execute("DELETE FROM project_members WHERE project_id=?", (pid,))
            conn.execute("DELETE FROM fetch_tokens_v2 WHERE project_id=?", (pid,))
            conn.execute("DELETE FROM mcp_tokens WHERE project_id=?", (pid,))
            conn.commit()
        except Exception as e:
            app.logger.warning("DB delete failed: %s", e)
    else:
        _vmsg("DELETE pid=%d - %d members remain, keeping project alive", pid, remaining_members)
    _vmsg("DELETE DONE pid=%d", pid)
    return jsonify({"status": "deleted"})


@app.route("/projects/<int:pid>/token", methods=["POST"])
def update_project_token(pid):
    """Update the Bitbucket HTTP access token for the current user.
    Verifies the token with git ls-remote before storing."""
    proj = _projects().get(pid) or _get_shared_project(pid)
    if not proj:
        return jsonify({"error": "project not found"}), 404
    data = request.get_json(force=True) or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"error": "token required"}), 400
    git_url = proj.get("git_url", "")
    if not git_url:
        return jsonify({"error": "not a cloned project"}), 400

    # Verify token with git ls-remote preflight
    _ssl = "true" if GIT_SSL_VERIFY else "false"
    git_auth = ["-c", f"http.sslVerify={_ssl}", "-c", f"http.extraHeader=Authorization: Bearer {token}"]
    git_env = _git_env()
    r = subprocess.run(["git"] + git_auth + ["ls-remote", git_url],
                       capture_output=True, text=True, timeout=30, env=git_env)
    if r.returncode != 0:
        err_lower = (r.stderr or "").lower()
        if any(p in err_lower for p in ("401", "403", "authentication", "access denied")):
            return jsonify({"error": "bitbucket_auth_failed", "message": "Bitbucket rejected the token."}), 401
        return jsonify({"error": "preflight_failed", "message": redact_secret(r.stderr[:300], token)}), 500

    # Store token (Fernet-encrypted, per-user)
    _store_fetch_token(pid, token, uk=_user_key())
    _vmsg("TOKEN UPDATED pid=%d user=%s", pid, _user_key())
    return jsonify({"status": "ok", "message": "Token updated successfully"})


# ── MCP token endpoints ───────────────────────────────────────────

def _store_mcp_token(pid, user_key, token):
    conn = _db_conn()
    conn.execute(
        "INSERT OR REPLACE INTO mcp_tokens(project_id, user_key, token, created_at) VALUES(?, ?, ?, ?)",
        (pid, user_key, token, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()


@app.route("/projects/<int:pid>/mcp-token", methods=["POST"])
def create_mcp_token(pid):
    """Generate an MCP API token for a project. Requires SSO auth.

    The token is stored in the mcp_tokens table and allows the MCP
    server to access /graph/ endpoints without a session cookie.
    Scoped per-project, revocable.

    On each generate, any previous token for this project (across ALL
    user_keys) is deleted first — guaranteeing exactly one active token
    per project. This prevents stale-token accumulation when the user_key
    drifts (anon session regeneration, SSO sub changes) and implements
    "disable old + assign new" semantics for the local MCP single-user flow.
    """
    proj = _projects().get(pid) or _get_shared_project(pid)
    if not proj:
        return jsonify({"error": "project not found"}), 404
    uk = _user_key()
    token = "mcp_" + secrets.token_urlsafe(32)
    conn = _db_conn()
    conn.execute("DELETE FROM mcp_tokens WHERE project_id=?", (pid,))
    _store_mcp_token(pid, uk, token)
    _vmsg("MCP TOKEN CREATED pid=%d user=%s (old tokens cleared)", pid, uk)
    return jsonify({"mcp_token": token, "project_id": pid})


@app.route("/projects/<int:pid>/mcp-token", methods=["GET"])
def get_mcp_token(pid):
    """Return the current MCP token for this project + user. Requires SSO auth.

    Lets the UI repopulate the token field on a fresh browser/session so
    the user does not need to regenerate (which would invalidate the old
    token they may still have wired into their MCP config).
    """
    proj = _projects().get(pid) or _get_shared_project(pid)
    if not proj:
        return jsonify({"error": "project not found"}), 404
    uk = _user_key()
    try:
        conn = _db_conn()
        row = conn.execute(
            "SELECT token FROM mcp_tokens WHERE project_id=? AND user_key=?",
            (pid, uk),
        ).fetchone()
    except Exception:
        row = None
    return jsonify({"token": (row["token"] if row else ""), "mcp_token": (row["token"] if row else ""), "project_id": pid})


@app.route("/projects/<int:pid>/mcp-token", methods=["DELETE"])
def revoke_mcp_token(pid):
    """Revoke the MCP API token for a project. Requires SSO auth."""
    proj = _projects().get(pid) or _get_shared_project(pid)
    if not proj:
        return jsonify({"error": "project not found"}), 404
    uk = _user_key()
    conn = _db_conn()
    conn.execute("DELETE FROM mcp_tokens WHERE project_id=? AND user_key=?", (pid, uk))
    conn.commit()
    _vmsg("MCP TOKEN REVOKED pid=%d user=%s", pid, uk)
    return jsonify({"status": "ok", "message": "MCP token revoked"})


# ── Share key endpoints ──────────────────────────────────────────

def _generate_share_key(pid):
    """Generate a share key: <pid>-<8 random chars>."""
    return f"{pid}-{secrets.token_urlsafe(6)}"


@app.route("/projects/<int:pid>/share", methods=["POST"])
def create_share_link(pid):
    """Generate a share key for a project. Returns the key to share with others."""
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"error": "project not found"}), 404
    share_key = _generate_share_key(pid)
    try:
        conn = _db_conn()
        # Remove old share keys for this project
        conn.execute("DELETE FROM project_share_keys WHERE project_id=?", (pid,))
        conn.execute(
            "INSERT INTO project_share_keys(project_id, share_key, created_at) VALUES(?, ?, ?)",
            (pid, share_key, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        _vmsg("SHARE CREATED pid=%d key=%s", pid, share_key)
    except Exception as e:
        return jsonify({"error": "share_failed", "message": str(e)[:200]}), 500
    return jsonify({"share_key": share_key, "project_id": pid, "project_name": proj.get("name", "")})


@app.route("/share/join", methods=["POST"])
def join_shared_project():
    """Join a shared project using a share key.
    Requires SSO auth + a Bitbucket HTTP access token (for private repos)."""
    data = request.get_json(force=True) or {}
    share_key = (data.get("share_key") or "").strip()
    bitbucket_token = (data.get("bitbucket_token") or data.get("token") or "").strip()
    if not share_key:
        return jsonify({"error": "share_key required"}), 400

    # Look up the share key
    try:
        conn = _db_conn()
        row = conn.execute(
            "SELECT project_id FROM project_share_keys WHERE share_key = ?", (share_key,)
        ).fetchone()
        if not row:
            return jsonify({"error": "invalid_share_key", "message": "Share key not found or revoked."}), 404
        pid = row["project_id"]
    except Exception as e:
        return jsonify({"error": "lookup_failed", "message": str(e)[:200]}), 500

    # Get the project data
    try:
        proj_row = conn.execute("SELECT user_key, data FROM projects WHERE id = ?", (pid,)).fetchone()
        if not proj_row:
            return jsonify({"error": "project_not_found"}), 404
        proj = json.loads(proj_row["data"])
        git_url = proj.get("git_url", "")
    except Exception:
        return jsonify({"error": "project_load_failed"}), 500

    uk = _user_key()
    # Already a member?
    try:
        existing = conn.execute(
            "SELECT 1 FROM project_members WHERE project_id=? AND user_key=?", (pid, uk)
        ).fetchone()
        if existing:
            return jsonify({"status": "already_member", "project_id": pid, "message": "You already have access to this project."})
    except Exception:
        pass

    # If it's a Bitbucket repo, verify the user's token
    if bitbucket_token and git_url:
        _ssl = "true" if GIT_SSL_VERIFY else "false"
        git_auth = ["-c", f"http.sslVerify={_ssl}", "-c", f"http.extraHeader=Authorization: Bearer {bitbucket_token}"]
        git_env = _git_env()
        r = subprocess.run(["git"] + git_auth + ["ls-remote", git_url],
                           capture_output=True, text=True, timeout=30, env=git_env)
        if r.returncode != 0:
            err_lower = (r.stderr or "").lower()
            if any(p in err_lower for p in ("401", "403", "authentication", "access denied")):
                return jsonify({"error": "bitbucket_auth_failed", "message": "Bitbucket rejected the token. Check repo read permission."}), 401
            return jsonify({"error": "preflight_failed", "message": redact_secret(r.stderr[:300], bitbucket_token)}), 500
        # Store the user's token (Fernet-encrypted, per-user)
        _store_fetch_token(pid, bitbucket_token, uk=uk)

    # Add membership
    try:
        conn.execute(
            "INSERT OR REPLACE INTO project_members(project_id, user_key, joined_at) VALUES(?, ?, ?)",
            (pid, uk, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    except Exception as e:
        return jsonify({"error": "join_failed", "message": str(e)[:200]}), 500

    # Load project into user's session
    _projects()[pid] = proj
    _vmsg("SHARE JOIN user=%s pid=%d", uk, pid)
    return jsonify({
        "status": "joined",
        "project_id": pid,
        "project_name": proj.get("name", ""),
        "git_url": git_url,
    })


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
        print(f"[GRAPH-DATA] project {pid} not found", flush=True)
        return jsonify({"graphify": None, "nodes": 0, "edges": 0}), 200
    gf = proj.get("graphify_data")
    print(f"[GRAPH-DATA] pid={pid} name={proj.get('name')} graphify_data={bool(gf)} nodes={proj.get('nodes')} edges={proj.get('edges')} crg_db={proj.get('crg_db_path')}", flush=True)
    result = {"id": pid, "name": proj["name"], "status": proj.get("status"),
              "nodes": proj.get("nodes", 0), "edges": proj.get("edges", 0)}
    if gf:
        result["graphify"] = gf
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


@app.route("/projects/<int:pid>/upload-data", methods=["POST"])
def project_upload_data(pid):
    """Accept graphify JSON or CRG SQLite DB upload for upload-type projects."""
    proj = _projects().get(pid)
    if not proj:
        return jsonify({"error": "project not found"}), 404
    if "graph_file" not in request.files:
        return jsonify({"error": "graph_file required"}), 400
    upload_type = request.form.get("type", "graphify")
    f = request.files["graph_file"]

    if upload_type == "graphify":
        try:
            raw = f.read()
            data = json.loads(raw)
        except Exception as e:
            return jsonify({"error": f"invalid JSON: {e}"}), 400
        if "nodes" not in data:
            return jsonify({"error": "graph JSON must contain 'nodes' key"}), 400
        proj["graphify_data"] = data
        proj["nodes"] = len(data.get("nodes", []))
        proj["edges"] = len(data.get("links", []))
        _vmsg("UPLOAD graphify pid=%d - nodes=%d edges=%d", pid, proj["nodes"], proj["edges"])

    elif upload_type == "crg":
        import sqlite3, tempfile
        tmp_dir = os.path.join(REPO_DIR, f"upload-{pid}")
        os.makedirs(tmp_dir, exist_ok=True)
        db_path = os.path.join(tmp_dir, "graph.db")
        f.save(db_path)
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cn = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            ce = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            conn.close()
        except Exception as e:
            os.unlink(db_path)
            return jsonify({"error": f"invalid SQLite DB: {e}"}), 400
        proj["crg_db_path"] = db_path
        proj["crg_nodes"] = cn
        proj["nodes"] = max(proj.get("nodes", 0), cn)
        proj["edges"] = max(proj.get("edges", 0), ce)
        _vmsg("UPLOAD crg pid=%d - crg_nodes=%d crg_edges=%d", pid, cn, ce)

    else:
        return jsonify({"error": f"unknown type '{upload_type}'; expected 'graphify' or 'crg'"}), 400

    has_gf = bool(proj.get("graphify_data"))
    has_crg = bool(proj.get("crg_db_path"))
    if has_gf or has_crg:
        proj["status"] = "ready"
    _save_project(pid, proj)
    return jsonify({"id": pid, "status": proj["status"], "nodes": proj.get("nodes", 0),
                    "edges": proj.get("edges", 0), "has_graphify": has_gf, "has_crg": has_crg})


@app.route("/projects/<int:pid>/graph-html")
def project_graph_html(pid):
    """Serve graphify's graph.html with Intelligraph dark theme injected.
    Works for cloned repos (reads graphify-out/graph.html) AND uploads (generates HTML from graphify_data JSON)."""
    proj = _projects().get(pid)
    if not proj:
        app.logger.warning("graph-html: project %d not found", pid)
        return """<!DOCTYPE html><html><head><meta charset="utf-8"><style>body{background:rgba(0,0,0,0.8);color:#c9d1d9;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}p{text-align:center}</style></head><body><p>Project deleted or not found.<br>Select another project from the sidebar.</p></body></html>""", 200

    html = None
    repo_dir = proj.get("repo_dir")
    app.logger.info("graph-html: pid=%d repo_dir=%s graphify_data=%s graph_html_path=%s",
                    pid, repo_dir, bool(proj.get("graphify_data")), proj.get("graph_html_path"))

    # 1. Try relocated artifact (post-build cleanup)
    graph_html_path = proj.get("graph_html_path")
    if graph_html_path and os.path.exists(graph_html_path):
        try:
            with open(graph_html_path, "r", encoding="utf-8") as f:
                html = f.read()
            app.logger.info("graph-html: loaded from artifact path (%d bytes)", len(html))
        except Exception as e:
            app.logger.warning("graph-html: failed to read %s: %s", graph_html_path, e)

    # 2. Try cloned repo's pre-built graph.html (if repo_dir still alive)
    if not html and repo_dir:
        p = os.path.join(repo_dir, "graphify-out", "graph.html")
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    html = f.read()
                app.logger.info("graph-html: loaded from repo_dir graph.html (%d bytes)", len(html))
            except Exception as e:
                app.logger.warning("graph-html: failed to read %s: %s", p, e)

    # 3. Fallback: generate from graphify_data JSON
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
                community_labels = {}
                for c in (gf_data.get("communities") or []):
                    cid = c.get("id")
                    if cid is None:
                        cid = c.get("community_id")
                    if cid is not None:
                        community_labels[cid] = c.get("label") or c.get("name") or ""
                crg_labels = _enrich_community_labels(gf_data, proj.get("crg_db_path"))
                comms = {}
                for nid, ndata in G.nodes(data=True):
                    cid = ndata.get('community', 0)
                    if cid not in comms:
                        comms[cid] = []
                        if cid not in community_labels or not community_labels.get(cid):
                            community_labels[cid] = crg_labels.get(cid) or f"Community {cid}"
                    comms[cid].append(nid)
                tmp_path = f"{TEMP_DIR}/intelligraph-gf-html-{_user_key()}-{pid}.html"
                gf_export.to_html(G, comms, tmp_path, community_labels=community_labels, node_limit=VIZ_NODE_LIMIT)
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

    # Replace vis-network CDN with local copy for closed-network support
    html = html.replace(
        "https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js",
        "/static/vis-network.min.js"
    )

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
/* ── Sidebar (graph internal nav) - restored ── */
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
    # Relocated artifact (post-build cleanup)
    gf_path = proj.get("graphify_path")
    if gf_path and os.path.exists(gf_path):
        return gf_path
    # Original repo_dir location (if repo not yet deleted)
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

    proj = (_projects().get(project_id) or _get_shared_project(project_id)) if project_id else None
    if not proj:
        _vmsg("RETRIEVE SKIP pid=%s - project not found", project_id)
        return jsonify({"context": "", "files": [], "strategy": "no_project", "plan": {}}), 200

    # Ensure id + user_key are set so downstream modules (retriever sparse fetch) can load tokens
    proj["id"] = project_id
    proj["_user_key"] = _user_key()

    _vmsg("RETRIEVE START pid=%s prompt=%s", project_id, prompt[:100])
    from retrieval import retrieve_context
    # Optional tuning overrides (used by tune.py sweep)
    overrides = {}
    for k in ("file_count", "crg_ratio", "depth"):
        v = request.args.get(k) or data.get(k)
        if v is not None:
            try:
                overrides[k] = int(v) if k != "crg_ratio" else float(v)
            except (ValueError, TypeError):
                pass
    # Resolution % — scales the context budget (merger DEFAULT_TOKEN_BUDGET).
    # 100 = full budget (12000 chars), 25 = quarter (3000 chars). Floor at 2000.
    rpct = data.get("resolution_pct")
    if rpct is not None:
        try:
            overrides["resolution_pct"] = max(1, min(100, int(rpct)))
        except (ValueError, TypeError):
            pass
    try:
        result = retrieve_context(proj, prompt, overrides=overrides if overrides else None)
        _vmsg("RETRIEVE DONE pid=%s strategy=%s files=%d context_len=%d",
               project_id, result.get("strategy", "?"),
               len(result.get("files", [])),
               len(result.get("context", "")))
    except Exception as e:
        _vmsg("RETRIEVE ERROR pid=%s - %s", project_id, str(e)[:300])
        app.logger.warning("retrieve_context failed: %s", e, exc_info=True)
        result = {"context": "", "files": [], "strategy": "retrieval_error", "plan": {},
                  "matched_nodes": [], "context_stats": {"error": str(e)[:200]}}
    return jsonify(result)


@app.route("/graph/crg", methods=["POST"])
def graph_crg():
    """Direct CRG intelligence endpoint for MCP server and external tools.

    Body: { project_id, mode, query, embedding_weight, max_hops, max_nodes, max_tokens, snippet_chars }
    mode: "search" | "semantic" | "hybrid" | "architecture" | "impact" | "flows" | "traverse" | "snippets" | "rationale"
    query: symbol name or search text

    Returns structured CRG data (symbols, communities, blast-radius, flows, subgraphs, snippets, rationale).
    """
    data = request.get_json(force=True) or {}
    project_id = data.get("project_id")
    mode = data.get("mode", "search")
    query = (data.get("query") or "").strip()

    if not project_id:
        return jsonify({"error": "project_id required"}), 400

    proj = (_projects().get(project_id) or _get_shared_project(project_id)) if project_id else None
    if not proj:
        return jsonify({"error": "project not found"}), 404
    proj["id"] = project_id

    try:
        from crg_intelligence import get_providers
        providers = get_providers(proj)
        if not providers:
            return jsonify({"error": "CRG not available for this project", "results": []}), 200

        provider = providers[0]
        if mode == "search":
            results = provider.search(query, max_results=20)
        elif mode == "semantic":
            results = provider.semantic_search(query, max_results=20)
        elif mode == "hybrid":
            ew = float(data.get("embedding_weight", 0.4))
            results = provider.hybrid_search(query, max_results=20, embedding_weight=ew)
            # Enrich results with source snippets so MCP clients get enough
            # context without needing to call local_files separately.
            try:
                top_names = [r.get("name") for r in results[:5] if r.get("name")]
                if top_names:
                    snips = provider.get_snippets(top_names, max_chars=250)
                    for r in results:
                        s = snips.get(r.get("name", ""))
                        if s and s.get("snippet"):
                            r["snippet"] = s["snippet"][:200]
            except Exception:
                pass
            # Enrich results with symbols-per-file so LLM can node() specific
            # symbols instead of reading the whole file with local_files().
            try:
                seen_fps = set()
                for r in results:
                    fp = r.get("file_path", "")
                    if fp and fp not in seen_fps:
                        seen_fps.add(fp)
                        syms = provider.get_symbols_in_file(fp, limit=5)
                        if syms:
                            r["symbols_in_file"] = [s["name"] for s in syms]
            except Exception:
                pass
        elif mode == "architecture":
            results = provider.architecture()
        elif mode == "impact":
            results = provider.impact(query, max_depth=2)
        elif mode == "flows":
            results = provider.flows(query)
        elif mode == "traverse":
            max_hops = int(data.get("max_hops", 2))
            max_nodes = int(data.get("max_nodes", 30))
            max_tokens = int(data.get("max_tokens", 400))
            results = provider.traverse(query, max_hops=max_hops, max_nodes=max_nodes, max_tokens=max_tokens)
        elif mode == "snippets":
            names = data.get("node_names", [query] if query else [])
            max_chars = int(data.get("snippet_chars", 500))
            results = provider.get_snippets(names, max_chars=max_chars)
        elif mode == "rationale":
            results = provider.get_rationale(query)
        else:
            return jsonify({"error": f"unknown mode: {mode}"}), 400

        _vmsg("CRG ENDPOINT: pid=%s mode=%s query=%s -> %d results", project_id, mode, query[:50], len(results) if isinstance(results, list) else len(results.keys()))
        return jsonify({"mode": mode, "query": query, "results": results})
    except Exception as e:
        _vmsg("CRG ENDPOINT ERROR: %s", str(e)[:300])
        app.logger.warning("graph_crg failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)[:200]}), 500


# ── Graph traversal endpoints (lightweight, always-fresh) ───────────

_JUNK_PATH_PATTERNS = [
    "/build/", "/bundle/", "/devtools/", "/dist/", "/out/",
    ".min.js", ".chunk.js", ".bundle.js", ".pack.js",
    "/generated/", "/codegen/", "/__generated__/",
    ".ngfactory.ts", "redux-dev-tools", "build-resources",
]


def _is_junk_path(fp):
    if not fp:
        return True
    lower = fp.lower() if isinstance(fp, str) else ""
    return any(p in lower for p in _JUNK_PATH_PATTERNS)


@app.route("/graph/node", methods=["GET"])
def graph_node():
    """Get node details + neighbors from graph.json + CRG metadata.

    Query params: project_id, name, depth (1-3, default 1),
                  include_rationale (default true), include_snippets (default false)
    """
    project_id = request.args.get("project_id", type=int)
    name = request.args.get("name", "").strip()
    depth = min(3, max(1, request.args.get("depth", 1, type=int)))
    include_rationale = request.args.get("include_rationale", "true").lower() != "false"
    include_snippets = request.args.get("include_snippets", "false").lower() == "true"
    if not project_id or not name:
        return jsonify({"error": "project_id and name required"}), 400

    proj = _projects().get(project_id) or _get_shared_project(project_id)
    if not proj:
        return jsonify({"error": "project not found"}), 404

    gf = proj.get("graphify_data")
    if not gf:
        return jsonify({"error": "no graph data"}), 200

    nodes = gf.get("nodes", [])
    links = gf.get("links", [])
    name_lower = name.lower()

    # Find node: exact match first, then substring
    matched = None
    for n in nodes:
        for key in (n.get("id"), n.get("label"), n.get("qualified_name")):
            if key and key.lower() == name_lower:
                matched = n
                break
        if matched:
            break
    if not matched:
        for n in nodes:
            for key in (n.get("id"), n.get("label"), n.get("qualified_name")):
                if key and name_lower in key.lower():
                    matched = n
                    break
            if matched:
                break

    if not matched:
        return jsonify({"node": None, "neighbors": []}), 200

    node_id = matched.get("id") or matched.get("label")
    node_label = matched.get("label") or node_id
    source_file = matched.get("source_file") or ""

    # CRG enrichment (signature, is_test)
    crg_meta = {}
    crg_path = proj.get("crg_db_path")
    if crg_path and os.path.exists(crg_path):
        try:
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(f"file:{crg_path}?mode=ro", uri=True)
            conn.row_factory = _sqlite3.Row
            rows = conn.execute(
                "SELECT name, kind, signature, is_test, community_id FROM nodes WHERE name = ? OR qualified_name = ? LIMIT 5",
                (node_label, node_label)
            ).fetchall()
            if rows:
                r = rows[0]
                crg_meta = {
                    "kind": r["kind"], "signature": r["signature"] or "",
                    "is_test": bool(r["is_test"]), "community_id": r["community_id"],
                }
            conn.close()
        except Exception:
            pass

    # Build 1-hop neighbors from links
    neighbors = []
    for l in links:
        src = l.get("source") or l.get("from")
        tgt = l.get("target") or l.get("to")
        edge_type = l.get("type") or l.get("kind") or "link"
        confidence = l.get("confidence", "")
        if src == node_id or src == node_label:
            # Outgoing edge — this node calls/uses/imports the target
            tgt_node = None
            for n in nodes:
                if (n.get("id") or n.get("label")) == tgt:
                    tgt_node = n
                    break
            tgt_file = (tgt_node or {}).get("source_file", "")
            if not _is_junk_path(tgt_file):
                neighbors.append({
                    "name": tgt, "edge": edge_type, "direction": "outgoing",
                    "confidence": confidence, "file": tgt_file,
                })
        elif tgt == node_id or tgt == node_label:
            # Incoming edge — something calls/uses/imports this node
            src_node = None
            for n in nodes:
                if (n.get("id") or n.get("label")) == src:
                    src_node = n
                    break
            src_file = (src_node or {}).get("source_file", "")
            if not _is_junk_path(src_file):
                neighbors.append({
                    "name": src, "edge": edge_type, "direction": "incoming",
                    "confidence": confidence, "file": src_file,
                })

    # Degree = total neighbors
    degree = len(neighbors)

    # Community from graphify
    community = matched.get("community") or matched.get("community_id")

    result = {
        "node": {
            "name": node_label,
            "id": node_id,
            "file": source_file,
            "kind": crg_meta.get("kind") or matched.get("file_type") or "unknown",
            "community": community,
            "degree": degree,
            "signature": crg_meta.get("signature", ""),
            "is_test": crg_meta.get("is_test", False),
        },
        "neighbors": neighbors[:30],
    }

    # Multi-hop: when depth > 1, also get CRG-based subgraph
    if depth > 1 and crg_path and os.path.exists(crg_path):
        try:
            from crg_intelligence import get_providers
            proj["id"] = project_id
            providers = get_providers(proj)
            if providers:
                provider = providers[0]
                subgraph = provider.traverse(node_label, max_hops=depth, max_nodes=30, max_tokens=400)
                result["subgraph"] = subgraph
        except Exception:
            pass

    # Rationale notes (from graphify rationale_for edges)
    if include_rationale:
        try:
            rationale_nodes = {n.get("id") or n.get("label"): n for n in nodes if n.get("file_type") == "rationale"}
            if rationale_nodes:
                rationale = []
                for l in links:
                    src = l.get("source") or l.get("from")
                    tgt = l.get("target") or l.get("to")
                    rel = l.get("type") or l.get("kind") or ""
                    rn = None
                    if src == node_id and rel == "rationale_for" and tgt in rationale_nodes:
                        rn = rationale_nodes[tgt]
                    elif tgt == node_id and rel == "rationale_for" and src in rationale_nodes:
                        rn = rationale_nodes[src]
                    if rn:
                        text = rn.get("label") or rn.get("id") or ""
                        if text:
                            rationale.append({
                                "text": text[:500],
                                "confidence": l.get("confidence", ""),
                                "source_file": rn.get("source_file", ""),
                            })
                if rationale:
                    result["rationale"] = rationale
        except Exception:
            pass

    # Source code snippets (from CRG node_snippets table)
    # Get snippets for ALL subgraph nodes (up to 15), not just target + top 5
    if include_snippets and crg_path and os.path.exists(crg_path):
        try:
            from crg_intelligence import get_providers
            proj["id"] = project_id
            providers = get_providers(proj)
            if providers:
                provider = providers[0]
                # Collect all node names from subgraph + neighbors + target
                snippet_names = [node_label]
                if result.get("subgraph"):
                    for sn in result["subgraph"].get("nodes", []):
                        if sn.get("name") and sn["name"] not in snippet_names:
                            snippet_names.append(sn["name"])
                for n in neighbors:
                    if n.get("name") and n["name"] not in snippet_names:
                        snippet_names.append(n["name"])
                snippets = provider.get_snippets(snippet_names[:15], max_chars=500)
                if snippets:
                    result["snippets"] = snippets
        except Exception:
            pass

    # Build reading plan: group subgraph + neighbors by file, list symbols to look for
    # This tells the LLM WHAT to read (which files, which symbols) instead of
    # reading 15 files blindly with local_files.
    try:
        file_symbols = {}
        # Add target
        if source_file:
            file_symbols.setdefault(source_file, {"symbols": set(), "edges": set()})
            file_symbols[source_file]["symbols"].add(node_label)
        # Add subgraph nodes
        if result.get("subgraph"):
            for sn in result["subgraph"].get("nodes", []):
                fp = sn.get("file", "")
                nm = sn.get("name", "")
                if fp and nm:
                    if not _is_junk_path(fp) and not _is_test_path(fp):
                        file_symbols.setdefault(fp, {"symbols": set(), "edges": set()})
                        file_symbols[fp]["symbols"].add(nm)
        # Add neighbors
        for n in neighbors:
            fp = n.get("file", "")
            nm = n.get("name", "")
            if fp and nm:
                if not _is_junk_path(fp) and not _is_test_path(fp):
                    file_symbols.setdefault(fp, {"symbols": set(), "edges": set()})
                    file_symbols[fp]["symbols"].add(nm)
        # Convert to list
        reading_plan = []
        for fp, info in sorted(file_symbols.items(), key=lambda x: -len(x[1]["symbols"])):
            reading_plan.append({
                "file": fp,
                "symbols": sorted(info["symbols"])[:8],
            })
        if reading_plan:
            result["reading_plan"] = reading_plan
    except Exception:
        pass

    return jsonify(result)


@app.route("/graph/path", methods=["GET"])
def graph_path():
    """Shortest path between two nodes via BFS on graph.json links."""
    project_id = request.args.get("project_id", type=int)
    src_name = request.args.get("from", "").strip()
    dst_name = request.args.get("to", "").strip()
    if not project_id or not src_name or not dst_name:
        return jsonify({"error": "project_id, from, and to required"}), 400

    proj = _projects().get(project_id) or _get_shared_project(project_id)
    if not proj:
        return jsonify({"error": "project not found"}), 404

    gf = proj.get("graphify_data")
    if not gf:
        return jsonify({"error": "no graph data"}), 200

    nodes = gf.get("nodes", [])
    links = gf.get("links", [])

    # Build adjacency list
    adj = {}
    node_lookup = {}
    for n in nodes:
        nid = n.get("id") or n.get("label")
        if nid:
            adj.setdefault(nid, [])
            node_lookup[nid] = n
            node_lookup[nid.lower()] = n

    for l in links:
        src = l.get("source") or l.get("from")
        tgt = l.get("target") or l.get("to")
        if src and tgt:
            adj.setdefault(src, []).append((tgt, l))
            adj.setdefault(tgt, []).append((src, l))

    # Resolve source and target (exact, then substring)
    src_id = None
    dst_id = None
    src_lower = src_name.lower()
    dst_lower = dst_name.lower()
    for nid in node_lookup:
        if nid.lower() == src_lower:
            src_id = [k for k in node_lookup if k.lower() == src_lower][0]
            break
    if not src_id:
        for nid, n in node_lookup.items():
            label = (n.get("label") or "").lower()
            if src_lower in label or src_lower in nid.lower():
                src_id = nid
                break
    for nid in node_lookup:
        if nid.lower() == dst_lower:
            dst_id = [k for k in node_lookup if k.lower() == dst_lower][0]
            break
    if not dst_id:
        for nid, n in node_lookup.items():
            label = (n.get("label") or "").lower()
            if dst_lower in label or dst_lower in nid.lower():
                dst_id = nid
                break

    if not src_id or not dst_id:
        return jsonify({"path": [], "hops": 0, "error": "node not found"}), 200

    # BFS
    from collections import deque
    visited = {src_id}
    queue = deque([(src_id, [(src_id, None)])])
    while queue:
        current, path = queue.popleft()
        if current == dst_id:
            result_path = []
            for nid, edge_link in path:
                n = node_lookup.get(nid, {})
                fp = n.get("source_file", "")
                entry = {"name": n.get("label") or nid, "file": fp}
                if edge_link:
                    entry["edge"] = edge_link.get("type") or edge_link.get("kind") or "link"
                result_path.append(entry)
            return jsonify({"path": result_path, "hops": len(result_path) - 1})
        for neighbor, edge_link in adj.get(current, []):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [(neighbor, edge_link)]))

    return jsonify({"path": [], "hops": 0, "error": "no path found"}), 200


@app.route("/graph/communities", methods=["GET"])
def graph_communities():
    """List communities from graph.json with key nodes."""
    project_id = request.args.get("project_id", type=int)
    if not project_id:
        return jsonify({"error": "project_id required"}), 400

    proj = _projects().get(project_id) or _get_shared_project(project_id)
    if not proj:
        return jsonify({"error": "project not found"}), 404

    gf = proj.get("graphify_data")
    if not gf:
        return jsonify([]), 200

    nodes = gf.get("nodes", [])
    links = gf.get("links", [])

    # Group nodes by community
    communities = {}
    for n in nodes:
        cid = n.get("community") or n.get("community_id")
        if cid is None:
            continue
        communities.setdefault(cid, []).append(n)

    # Build degree scores
    degree = {}
    for l in links:
        for key in (l.get("source"), l.get("target"), l.get("from"), l.get("to")):
            if key:
                degree[key] = degree.get(key, 0) + 1

    result = []
    for cid, cnodes in sorted(communities.items(), key=lambda x: -len(x[1])):
        # Key nodes = top 3 by degree
        key_nodes = sorted(cnodes, key=lambda n: -degree.get(n.get("id") or n.get("label"), 0))[:3]
        key_nodes_list = [
            {"name": n.get("label") or n.get("id"), "file": n.get("source_file", ""),
             "degree": degree.get(n.get("id") or n.get("label"), 0)}
            for n in key_nodes if not _is_junk_path(n.get("source_file", ""))
        ]
        # Dominant language
        langs = {}
        for n in cnodes:
            ft = n.get("file_type") or ""
            if ft:
                langs[ft] = langs.get(ft, 0) + 1
        dominant = max(langs, key=langs.get) if langs else "unknown"

        result.append({
            "id": cid,
            "name": f"Community {cid}",
            "size": len(cnodes),
            "key_nodes": key_nodes_list,
            "dominant_language": dominant,
        })

    return jsonify(result[:20])


def _build_system_prompt(intent: str) -> str:
    """Build LLM system prompt tailored to the question intent.

    The intent is detected by planner.detect_intent() and flows through
    retrieval_strategy. Different question types need different answer
    structures — an architecture question shouldn't dump code, and an
    implementation question shouldn't skip it.
    """
    base = (
        "You are an expert software architect helping a developer understand a codebase.\n"
        "Do not invent files, functions, imports, or APIs.\n"
        "Answer based on what you have -- do not declare context insufficient.\n\n"
    )

    if intent == "architecture":
        return base + (
            "Answer in this structure:\n"
            "## Summary\n"
            "2-3 sentences on what the project is and its overall shape.\n\n"
            "## Architecture\n"
            "The major layers/components and how they connect. Include:\n"
            "- Layers: what are the major subsystems and what is each responsible for\n"
            "- Hubs: high-degree nodes that everything touches (name them)\n"
            "- Communities: tightly-coupled groups of symbols that work together\n"
            "- Dependencies: how the layers depend on each other\n"
            "Use the community data and neighbor data from the context. "
            "Describe structure, not implementation.\n\n"
            "## Key Files\n"
            "One file per major component, with its role (hub / connector / leaf) "
            "and what it does in 1-2 sentences.\n\n"
            "## References\n"
            "- `path/to/file.py` -- what it does\n\n"
            "Avoid code snippets unless absolutely necessary to illustrate a "
            "structural point. Architecture is higher-level than implementation. "
            "If you find yourself wanting to show code, describe what the code "
            "does in prose instead.\n"
            "Do not give one-line answers. Be thorough on the structure.\n"
        )

    if intent == "impact":
        return base + (
            "Answer in this structure:\n"
            "## Summary\n"
            "2-3 sentences on what would be affected by changing the target.\n\n"
            "## Affected Files\n"
            "List the affected files with their depth (direct = depth 1, indirect "
            "= depth 2+) and risk level (high / medium / low based on the score). "
            "Group by depth. Explain WHY each file is affected (caller? callee? "
            "importer?).\n\n"
            "## Recommendations\n"
            "What to check, what tests to run, what to be careful about.\n\n"
            "## References\n"
            "- `path/to/file.py` -- why it's affected\n\n"
            "Avoid code snippets unless absolutely necessary. Focus on the "
            "blast radius: which files, why, and how deep.\n"
        )

    if intent == "coverage":
        return base + (
            "Answer in this structure:\n"
            "## Summary\n"
            "2-3 sentences on what was found.\n\n"
            "## Files Found\n"
            "List every relevant file with a one-line description of what it does "
            "and why it matches the query. Group by category if useful (e.g. "
            "tests / implementation / config).\n\n"
            "## References\n"
            "- `path/to/file.py` -- what it does\n\n"
            "Avoid code snippets. This is a find-all question — the user wants "
            "the complete list of relevant files, not implementations.\n"
        )

    if intent == "nx_architecture":
        return base + (
            "Answer in this structure:\n"
            "## Summary\n"
            "2-3 sentences on the workspace shape.\n\n"
            "## Workspace Structure\n"
            "The projects, their dependencies, and how they're organized. "
            "Include: project names, what each does, dependency graph between "
            "them, and which are affected by changes.\n\n"
            "## References\n"
            "- `path/to/project.json` -- what it does\n\n"
            "Avoid code snippets. Describe the workspace structure.\n"
        )

    if intent == "how_works":
        return base + (
            "Answer in this structure:\n"
            "## Summary\n"
            "2-3 sentences answering the question directly.\n\n"
            "## Explanation\n"
            "Detailed walk-through of the logic. Why, not just what. Trace the "
            "flow step by step, naming the functions/files involved.\n\n"
            "## Code\n"
            "Include a focused code snippet showing the flow — not a full dump. "
            "Trim to the relevant lines only.\n\n"
            "## References\n"
            "- `path/to/file.py` -- what it does\n\n"
            "Do not give one-line answers. Be thorough.\n"
        )

    # what_is and default — balanced prompt
    return base + (
        "Answer in this structure:\n"
        "## Summary\n"
        "2-3 sentences answering the question directly.\n\n"
        "## Explanation\n"
        "Detailed walk-through of the logic. Why, not just what.\n\n"
        "## Code\n"
        "Include code snippets when the question is about implementation. "
        "For coverage, find-all, or architecture questions, list the relevant "
        "files and explain what they do -- code is not always needed.\n\n"
        "## References\n"
        "- `path/to/file.py` -- what it does\n\n"
        "Do not give one-line answers. Be thorough.\n"
    )


@app.route("/api/v1/projects/<int:pid>/completions", methods=["POST"])
def project_completions(pid):
    """Stateless completion endpoint for n8n/external automation + web chat.

    Each call creates a fresh LLM request with fresh project context.
    No LLM conversation state is persisted between calls — callers pass
    conversation_history as input.

    Request:
        {
            "prompt": "Show me code",
            "conversation_history": [     # optional, last N messages for follow-up context
                {"role": "user", "content": "How to add entities to map"},
                {"role": "assistant", "content": "To add entities, modify..."}
            ],
            "include_context": true,       # default true; retrieve project context
            "llm_url": "...",              # optional, falls back to INTELLIGRAPH_LLM_URL env
            "llm_token": "...",            # optional, falls back to INTELLIGRAPH_LLM_TOKEN env
            "model": "Qwen/Qwen3.6-27B-FP8",   # optional, falls back to INTELLIGRAPH_LLM_MODEL env
            "max_tokens": 4096,            # optional, default 4096
            "resolution_pct": 100          # optional, 1-100, scales context budget
        }

    Response:
        {
            "answer": "...",
            "sources": {                   # null if no context retrieved
                "context": "...",
                "files": ["src/map.py", ...],
                "context_stats": {}
            },
            "matched_nodes": [...],
            "model": "Qwen/Qwen3.6-27B-FP8",
            "session_mode": "stateless",
            "trace_id": "req_...",
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

    proj = _projects().get(pid)
    if not proj:
        return jsonify({"error": "project not found", "intent": "planner", "context_used": False}), 404

    # LLM provider: request body > env vars > default
    llm_url = (data.get("llm_url") or os.environ.get("INTELLIGRAPH_LLM_URL") or "").strip().rstrip("/")
    llm_token = data.get("llm_token") or os.environ.get("INTELLIGRAPH_LLM_TOKEN") or ""
    model = data.get("model") or os.environ.get("INTELLIGRAPH_LLM_MODEL") or "Qwen/Qwen3.6-27B-FP8"
    max_tokens = int(data.get("max_tokens") or 4096)

    if not llm_url:
        return jsonify({"error": "llm_url required (set INTELLIGRAPH_LLM_URL env var or pass in body)", "intent": "planner", "context_used": False}), 400

    host = urlparse(llm_url).hostname
    if host not in ALLOWED_LLM_HOSTS:
        return jsonify({"error": "provider not allowed", "intent": "planner", "context_used": False}), 403

    # Retrieval always uses the current prompt as-is.
    # The LLM resolves follow-up references (e.g. "show me code") from
    # conversation history — no heuristic query composition needed.
    conversation_history = data.get("conversation_history") or []
    retrieval_query = prompt

    # Retrieve fresh context per request (default: yes)
    include_context = data.get("include_context")
    if include_context is None:
        include_context = True
    retrieved = ""
    retrieved_files = []
    context_stats = {}
    matched_nodes = []
    retrieval_strategy = "planner"

    # ── Lightweight intent routing ──
    # For simple intents (what_is, coverage), use graph traversal instead of
    # the heavy 7-step pipeline. Saves ~80% tokens for simple questions.
    if include_context and proj.get("graphify_data"):
        try:
            from planner import detect_intent
            intent_info = detect_intent(prompt)
            intent = intent_info.get("intent", "what_is")
            target = intent_info.get("target", prompt[:80])
            retrieval_strategy = intent

            if intent in ("what_is", "coverage") and target:
                # Read tuning params
                embedding_weight = float(data.get("embedding_weight", 0.4))
                snippet_chars = int(data.get("snippet_chars", 1500))
                traversal_depth = int(data.get("traversal_depth", 1))

                # Lightweight path: search CRG + get node from graph
                gf = proj["graphify_data"]
                nodes = gf.get("nodes", [])
                links = gf.get("links", [])
                target_lower = target.lower().strip()

                # Find node (exact → substring)
                matched_node = None
                for n in nodes:
                    for key in (n.get("id"), n.get("label"), n.get("qualified_name")):
                        if key and key.lower() == target_lower:
                            matched_node = n
                            break
                    if matched_node:
                        break
                if not matched_node:
                    for n in nodes:
                        label = (n.get("label") or "").lower()
                        if target_lower in label:
                            matched_node = n
                            break

                # Semantic search fallback: if no graphify match, try CRG hybrid search
                # to find the closest symbol by meaning (e.g. "add entity" → "upsertEntity")
                if not matched_node and proj.get("crg_db_path"):
                    try:
                        from crg_intelligence import get_providers
                        proj["id"] = pid
                        providers = get_providers(proj)
                        if providers:
                            provider = providers[0]
                            sem_results = provider.hybrid_search(target, max_results=5, embedding_weight=embedding_weight)
                            for sr in sem_results:
                                sem_name = sr.get("name", "")
                                if sem_name:
                                    for n in nodes:
                                        for key in (n.get("id"), n.get("label"), n.get("qualified_name")):
                                            if key and key.lower() == sem_name.lower():
                                                matched_node = n
                                                break
                                        if matched_node:
                                            break
                                if matched_node:
                                    break
                    except Exception:
                        pass

                if matched_node:
                    node_id = matched_node.get("id") or matched_node.get("label")
                    # Build 1-hop neighbors
                    neighbors_list = []
                    for l in links:
                        src = l.get("source") or l.get("from")
                        tgt = l.get("target") or l.get("to")
                        edge_type = l.get("type") or l.get("kind") or "link"
                        conf = l.get("confidence", "")
                        if src == node_id:
                            tgt_node = next((n for n in nodes if (n.get("id") or n.get("label")) == tgt), {})
                            fp = tgt_node.get("source_file", "")
                            if not _is_junk_path(fp):
                                neighbors_list.append(f"  -> {tgt} ({edge_type}) -- {fp}")
                        elif tgt == node_id:
                            src_node = next((n for n in nodes if (n.get("id") or n.get("label")) == src), {})
                            fp = src_node.get("source_file", "")
                            if not _is_junk_path(fp):
                                neighbors_list.append(f"  <- {src} ({edge_type}) -- {fp}")

                    retrieved_files = list(set(
                        n.get("source_file", "") for n in nodes
                        if (n.get("id") or n.get("label")) == node_id or
                           any((n.get("id") or n.get("label")) == l.get("target", l.get("to")) and (l.get("source", l.get("from")) == node_id) for l in links)
                    ))
                    retrieved_files = [f for f in retrieved_files if f and not _is_junk_path(f)]

                    # Build compact context
                    # Try to enrich community name from CRG
                    comm_name = f"Community {matched_node.get('community', '?')}"
                    crg_db = proj.get("crg_db_path")
                    if crg_db and os.path.exists(crg_db) and matched_node.get("community") is not None:
                        try:
                            import sqlite3 as _sqlite3
                            _conn = _sqlite3.connect(f"file:{crg_db}?mode=ro", uri=True)
                            _conn.row_factory = _sqlite3.Row
                            _row = _conn.execute("SELECT name FROM communities WHERE id = ?", (matched_node["community"],)).fetchone()
                            if _row and _row["name"]:
                                comm_name = _row["name"]
                            _conn.close()
                        except Exception:
                            pass

                    context_parts = [
                        f"Symbol: {matched_node.get('label', node_id)}",
                        f"File: {matched_node.get('source_file', 'unknown')}",
                        f"Community: {comm_name}",
                        f"Connections ({len(neighbors_list)}):",
                        "\n".join(neighbors_list[:20]),
                    ]

                    # Source code snippets (from CRG node_snippets table)
                    snippet_count = 0
                    if snippet_chars > 0 and crg_db and os.path.exists(crg_db):
                        try:
                            from crg_intelligence import get_providers
                            providers = get_providers(proj)
                            if providers:
                                provider = providers[0]
                                snippet_names = [matched_node.get("label", node_id)] + [n.split("(")[0].strip().replace("-> ", "").replace("<- ", "") for n in neighbors_list[:3]]
                                snippets = provider.get_snippets(snippet_names, max_chars=snippet_chars // 3)
                                if snippets:
                                    snippet_lines = ["Source:"]
                                    chars_used = 0
                                    for sname, sdata in snippets.items():
                                        snip = sdata.get("snippet", "")
                                        if snip and chars_used + len(snip) < snippet_chars:
                                            snippet_lines.append(f"  {sname} ({sdata.get('file_path', '')}:{sdata.get('line_start', 0)}):")
                                            snippet_lines.append(f"    {snip[:snippet_chars // 3]}")
                                            chars_used += len(snip)
                                            snippet_count += 1
                                    if snippet_count > 0:
                                        context_parts.append("\n".join(snippet_lines))
                        except Exception:
                            pass

                    # Rationale notes (from graphify rationale_for edges)
                    rationale_count = 0
                    try:
                        rationale_nodes = {n.get("id") or n.get("label"): n for n in nodes if n.get("file_type") == "rationale"}
                        if rationale_nodes:
                            notes = []
                            for l in links:
                                src = l.get("source") or l.get("from")
                                tgt = l.get("target") or l.get("to")
                                rel = l.get("type") or l.get("kind") or ""
                                rn = None
                                if src == node_id and rel == "rationale_for" and tgt in rationale_nodes:
                                    rn = rationale_nodes[tgt]
                                elif tgt == node_id and rel == "rationale_for" and src in rationale_nodes:
                                    rn = rationale_nodes[src]
                                if rn:
                                    text = rn.get("label") or rn.get("id") or ""
                                    if text and len(notes) < 5:
                                        notes.append(f"  {text[:200]}")
                                        rationale_count += 1
                            if notes:
                                context_parts.append("Notes:")
                                context_parts.extend(notes)
                    except Exception:
                        pass

                    retrieved = "\n".join(context_parts)
                    context_stats = {
                        "mode": "lightweight", "intent": intent,
                        "neighbors": len(neighbors_list),
                        "snippets": snippet_count,
                        "rationale": rationale_count,
                    }
                    matched_nodes = [{"id": node_id, "label": matched_node.get("label", node_id)}]
                else:
                    # Node not found — fall through to heavy pipeline
                    retrieval_strategy = "planner"  # reset, will be set by retrieve_context
                    raise RuntimeError("node not found, falling back to heavy pipeline")

            if retrieval_strategy in ("what_is", "coverage") and retrieved:
                pass  # Already have lightweight context
            else:
                raise RuntimeError("use heavy pipeline")

        except RuntimeError:
            # Fall through to heavy pipeline
            pass
        except Exception as e:
            app.logger.warning("Lightweight intent routing failed: %s", e)

    # Heavy pipeline (for complex intents or lightweight fallback)
    if include_context and proj.get("graphify_data") and not retrieved:
        try:
            from retrieval import retrieve_context
            overrides = {}
            # Budget chars — explicit context budget (from TuningPanel slider)
            budget_chars = data.get("budget_chars")
            if budget_chars is not None:
                try:
                    overrides["budget_chars"] = max(3000, min(48000, int(budget_chars)))
                except (ValueError, TypeError):
                    pass
            # Chunk caps — per-task-type chunk count overrides (from TuningPanel table)
            chunk_caps = data.get("chunk_caps")
            if chunk_caps and isinstance(chunk_caps, dict):
                overrides["chunk_caps"] = chunk_caps
            result = retrieve_context(proj, retrieval_query, overrides=overrides if overrides else None)
            retrieved = result.get("context", "")
            retrieved_files = result.get("files", [])
            retrieval_strategy = result.get("strategy", "planner")
            context_stats = result.get("context_stats", {})
            matched_nodes = result.get("matched_nodes", [])
        except Exception as e:
            app.logger.warning("Completions context retrieval failed: %s", e)

    # ── Build LLM messages ──
    system_msg = _build_system_prompt(retrieval_strategy)
    messages = [{"role": "system", "content": system_msg}]

    # Inject retrieved context as a separate user message (ground truth)
    if retrieved:
        messages.append({"role": "user", "content": f"Project context:\n{retrieved}"})

    # Add conversation history (last 4 messages, each capped at 2000 chars)
    history = conversation_history[-4:] if conversation_history else []
    for m in history:
        role = m.get("role", "user")
        content = (m.get("content") or "")[:2000]
        if content:
            messages.append({"role": role, "content": content})

    # Current prompt (after context + history so LLM sees it last)
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
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
                "intent": retrieval_strategy,
                "context_used": bool(retrieved),
                "context_preview": retrieved[:500] if retrieved else "",
                "context_stats": context_stats,
                "files": retrieved_files[:10],
                "trace_id": trace_id,
            }), 502
        body = resp.json()
        choices = body.get("choices", [])
        if not choices:
            return jsonify({
                "error": "empty LLM response",
                "intent": retrieval_strategy,
                "context_used": bool(retrieved),
                "context_preview": retrieved[:500] if retrieved else "",
                "context_stats": context_stats,
                "trace_id": trace_id,
            }), 502
        answer = choices[0].get("message", {}).get("content", "")
        if not answer.strip():
            answer = "(No response content. Try rephrasing your question.)"
        path_warnings = _verify_paths(pid, answer) or []

        # Compute context savings metadata
        context_tokens = len(retrieved) // 4 if retrieved else 0
        full_corpus_tokens = (proj.get("nodes", 0) * 200) // 4
        reduction_factor = round(full_corpus_tokens / context_tokens, 1) if context_tokens > 0 else 0
        context_savings = {
            "context_tokens": context_tokens,
            "full_corpus_tokens": full_corpus_tokens,
            "reduction_factor": reduction_factor,
            "baseline": "full_corpus",
        }

        # Beta telemetry: log query for analytics
        try:
            uk = _user_key()
            conn = _db_conn()
            log_id = secrets.token_hex(12)
            conn.execute(
                "INSERT INTO query_logs(id, project_id, user_key, prompt, intent, strategy, context_tokens, answer_tokens, retrieval_mode, trace_id, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (log_id, pid, uk, prompt[:500], retrieval_strategy, context_stats.get("mode", "heavy"),
                 context_tokens, len(answer) // 4, "lightweight" if context_stats.get("mode") == "lightweight" else "heavy",
                 trace_id, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
        except Exception:
            pass

        return jsonify({
            "answer": answer,
            "intent": retrieval_strategy,
            "sources": {
                "context": retrieved,
                "files": retrieved_files,
                "context_stats": context_stats,
            } if retrieved else None,
            "matched_nodes": matched_nodes,
            "model": model,
            "trace_id": trace_id,
            "context_used": bool(retrieved),
            "context_stats": context_stats if retrieved else {},
            "context_savings": context_savings,
            "path_warnings": path_warnings,
            "usage": body.get("usage", {}),
        })
    except requests.exceptions.Timeout:
        return jsonify({
            "error": "LLM request timed out",
            "intent": retrieval_strategy,
            "context_used": bool(retrieved),
            "context_preview": retrieved[:500] if retrieved else "",
            "context_stats": context_stats,
            "files": retrieved_files[:10],
            "trace_id": trace_id,
        }), 504
    except Exception as e:
        return jsonify({
            "error": str(e),
            "intent": retrieval_strategy,
            "context_used": bool(retrieved),
            "context_preview": retrieved[:500] if retrieved else "",
            "context_stats": context_stats,
            "files": retrieved_files[:10],
            "trace_id": trace_id,
        }), 502


# ── Conversation persistence (SQLite-backed) ────────────────────

@app.route("/api/v1/projects/<int:pid>/conversations", methods=["GET"])
def get_conversations(pid):
    """Load all conversations for a project from SQLite."""
    uk = _user_key()
    try:
        conn = _db_conn()
        rows = conn.execute(
            "SELECT data FROM conversations WHERE project_id = ? AND user_key = ? ORDER BY created_at DESC",
            (pid, uk)
        ).fetchall()
        convs = [json.loads(r["data"]) for r in rows]
        return jsonify(convs)
    except Exception as e:
        app.logger.warning("Failed to load conversations: %s", e)
        return jsonify([])


@app.route("/api/v1/projects/<int:pid>/conversations", methods=["PUT"])
def save_conversations(pid):
    """Save full conversation list for a project (replace all)."""
    uk = _user_key()
    convs = request.get_json(force=True) or []
    if not isinstance(convs, list):
        return jsonify({"error": "expected array"}), 400
    try:
        conn = _db_conn()
        conn.execute("DELETE FROM conversations WHERE project_id = ? AND user_key = ?", (pid, uk))
        now = datetime.now(timezone.utc).isoformat()
        for c in convs[:50]:
            cid = c.get("id") or secrets.token_hex(8)
            conn.execute(
                "INSERT OR REPLACE INTO conversations(id, project_id, user_key, data, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                (cid, pid, uk, json.dumps(c), c.get("createdAt", now), now)
            )
        conn.commit()
        return jsonify({"status": "ok", "count": len(convs)})
    except Exception as e:
        app.logger.warning("Failed to save conversations: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/projects/<int:pid>/conversations/<conv_id>", methods=["DELETE"])
def delete_conversation(pid, conv_id):
    """Delete a single conversation from SQLite."""
    uk = _user_key()
    try:
        conn = _db_conn()
        conn.execute(
            "DELETE FROM conversations WHERE id = ? AND project_id = ? AND user_key = ?",
            (conv_id, pid, uk)
        )
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        app.logger.warning("Failed to delete conversation: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Beta telemetry: feedback + query logs ────────────────────────

@app.route("/api/v1/projects/<int:pid>/feedback", methods=["POST"])
def submit_feedback(pid):
    """Submit feedback (thumbs up/down + optional comment) for a response."""
    uk = _user_key()
    data = request.get_json(force=True) or {}
    trace_id = data.get("trace_id", "")
    rating = int(data.get("rating", 0))  # 1 = up, -1 = down, 0 = neutral
    comment = (data.get("comment") or "").strip()[:1000]
    if not trace_id:
        return jsonify({"error": "trace_id required"}), 400
    try:
        conn = _db_conn()
        fb_id = secrets.token_hex(12)
        conn.execute(
            "INSERT INTO feedback(id, project_id, user_key, trace_id, rating, comment, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (fb_id, pid, uk, trace_id, rating, comment, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/projects/<int:pid>/query-logs", methods=["GET"])
def get_query_logs(pid):
    """Get query logs for analytics (beta telemetry dashboard)."""
    uk = _user_key()
    try:
        conn = _db_conn()
        rows = conn.execute(
            "SELECT prompt, intent, strategy, context_tokens, answer_tokens, retrieval_mode, trace_id, created_at "
            "FROM query_logs WHERE project_id = ? AND user_key = ? ORDER BY created_at DESC LIMIT 200",
            (pid, uk)
        ).fetchall()
        logs = [dict(r) for r in rows]
        # Aggregate stats
        total = len(logs)
        lightweight_count = sum(1 for l in logs if l["retrieval_mode"] == "lightweight")
        avg_context = sum(l["context_tokens"] or 0 for l in logs) / total if total else 0
        avg_answer = sum(l["answer_tokens"] or 0 for l in logs) / total if total else 0
        intent_counts = defaultdict(int)
        for l in logs:
            intent_counts[l["intent"] or "unknown"] += 1
        return jsonify({
            "logs": logs,
            "stats": {
                "total_queries": total,
                "lightweight_queries": lightweight_count,
                "heavy_queries": total - lightweight_count,
                "avg_context_tokens": round(avg_context),
                "avg_answer_tokens": round(avg_answer),
                "intent_distribution": dict(intent_counts),
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    from build_queue import build_queue
    return jsonify({
        "sso_configured": bool(SSO_ISSUER),
        "site_url": SITE_URL,
        "downloads": {"mcp_server": "/download/mcp-server",
                     "graph_builder": "/download/graph-builder",
                     "agent": "/download/agent",
                     "test_mcp": "/download/test-mcp"},
        "project": proj,
        "projects": list(_projects().keys()),
        "build_queue_depth": build_queue.depth,
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
    result["status"]["artifacts_dir"] = ARTIFACTS_DIR
    result["status"]["artifacts_dir_exists"] = os.path.exists(ARTIFACTS_DIR)

    result["healthy"] = len(result["errors"]) == 0
    return jsonify(result)


# ── Entry point ──────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5050)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--sso-issuer")
    p.add_argument("--sso-client-id")
    p.add_argument("--sso-client-secret")
    args = p.parse_args()

    if args.sso_issuer:
        SSO_ISSUER = args.sso_issuer
        SSO_CLIENT_ID = args.sso_client_id or ""
        SSO_CLIENT_SECRET = args.sso_client_secret or ""
        fetch_sso_config()

    # (_projects_ref wired at module level above)

    _cleanup_orphans()

    print(f"Network:   {NETWORK_MODE} (SSL verify={'on' if LLM_SSL_VERIFY else 'off'}, git SSL={'on' if GIT_SSL_VERIFY else 'off'})")
    print(f"LLM hosts: {ALLOWED_LLM_HOSTS}")
    print(f"SSO:       {'configured' if SSO_ISSUER else 'disabled'}{' (PKCE)' if SSO_ISSUER and not SSO_CLIENT_SECRET else ''}")
    print(f"Downloads: /download/mcp-server, /download/graph-builder")
    print(f"LLM relay: /llm/ask")
    print(f"Server:    http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)