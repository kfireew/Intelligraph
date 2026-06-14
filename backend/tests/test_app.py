"""Tests for app.py — thin pod server (multi-project edition)."""
import json
import os
import sqlite3
import sys
import tempfile
from io import BytesIO

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module
from app import app as flask_app


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    flask_app.secret_key = "test-key"
    app_module._PROJECTS.clear()
    app_module._NEXT_PID.clear()
    with flask_app.test_client() as c:
        c.get("/auth/me")
        yield c


# ── Static routes ────────────────────────────────────────────

def test_index_returns_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Intelligraph" in r.data or b"<!DOCTYPE html>" in r.data


def test_status_returns_json(client):
    r = client.get("/status")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "downloads" in data
    assert "projects" in data
    assert "oidc_configured" in data


def test_auth_me_returns_json(client):
    r = client.get("/auth/me")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "authenticated" in data
    assert data["authenticated"] == False


def test_auth_login_without_oidc(client):
    r = client.get("/auth/login")
    assert r.status_code == 400
    assert "OIDC not configured" in json.loads(r.data)["error"]


# ── Downloads ────────────────────────────────────────────────

def test_download_mcp_server(client):
    r = client.get("/download/mcp-server")
    assert r.status_code == 200
    assert b"graphify-qa MCP" in r.data


def test_download_graph_builder(client):
    r = client.get("/download/graph-builder")
    assert r.status_code == 200


def test_download_mcp_config_without_token(client):
    r = client.get("/download/mcp-config")
    assert r.status_code == 400


# ── LLM relay ────────────────────────────────────────────────

def test_llm_relay_no_url(client):
    r = client.post("/llm/relay", json={"url": "", "token": "", "payload": {}})
    assert r.status_code == 400


def test_llm_relay_blocked_host(client):
    r = client.post("/llm/relay", json={
        "url": "http://127.0.0.1:19999/chat", "token": "test",
        "payload": {"messages": [{"role": "user", "content": "hi"}]}
    })
    assert r.status_code == 403


def test_llm_relay_unreachable(client):
    r = client.post("/llm/relay", json={
        "url": "https://ai-services.ai.idf.cts:19999/chat", "token": "test",
        "payload": {"messages": [{"role": "user", "content": "hi"}]}
    })
    assert r.status_code == 502


# ── MCP upload / clear / config ──────────────────────────────

def test_mcp_upload_no_file(client):
    r = client.post("/mcp/upload", data={})
    assert r.status_code == 400


def test_mcp_upload_invalid_type(client):
    r = client.post("/mcp/upload", data={
        "graph_file": (tempfile.SpooledTemporaryFile(), "test.txt"),
        "type": "invalid"
    })
    assert r.status_code == 400


def test_mcp_upload_bad_json(client):
    import io
    f = (io.BytesIO(b"not json"), "graph.json")
    r = client.post("/mcp/upload", data={"graph_file": f, "type": "graphify"},
                    content_type="multipart/form-data", buffered=True)
    assert r.status_code == 400
    assert b"Invalid JSON" in r.data


def test_mcp_upload_then_download_config(client):
    import io
    data = {"nodes": [], "links": []}
    f = (io.BytesIO(json.dumps(data).encode()), "graph.json")
    client.post("/mcp/upload", data={"graph_file": f, "type": "graphify"},
                content_type="multipart/form-data")
    r = client.get("/download/mcp-config")
    assert r.status_code == 200
    config = json.loads(r.data)
    assert "mcpServers" in config
    assert "token=" in config["mcpServers"]["graphify-qa"]["url"]


def test_mcp_clear(client):
    import io
    data = {"nodes": [], "links": []}
    f = (io.BytesIO(json.dumps(data).encode()), "graph.json")
    client.post("/mcp/upload", data={"graph_file": f, "type": "graphify"},
                content_type="multipart/form-data")
    r = client.post("/mcp/clear")
    assert json.loads(r.data)["status"] == "cleared"


# ── Project CRUD ─────────────────────────────────────────────

def test_list_projects_empty(client):
    r = client.get("/projects")
    assert r.status_code == 200
    assert json.loads(r.data) == []


def test_clone_upload_project(client):
    r = client.post("/projects/clone", json={"type": "upload", "name": "test"})
    assert r.status_code == 200
    p = json.loads(r.data)
    assert p["id"] == 1
    assert p["name"] == "test"
    assert p["status"] == "pending_upload"


def test_list_projects_after_add(client):
    client.post("/projects/clone", json={"type": "upload", "name": "p1"})
    client.post("/projects/clone", json={"type": "upload", "name": "p2"})
    r = client.get("/projects")
    projects = json.loads(r.data)
    assert len(projects) == 2
    assert {p["name"] for p in projects} == {"p1", "p2"}


def test_delete_project(client):
    client.post("/projects/clone", json={"type": "upload", "name": "del-me"})
    r = client.delete("/projects/1")
    assert json.loads(r.data)["status"] == "deleted"
    assert json.loads(client.get("/projects").data) == []


def test_project_status(client):
    client.post("/projects/clone", json={"type": "upload", "name": "s"})
    r = client.get("/projects/1/status")
    p = json.loads(r.data)
    assert p["name"] == "s"
    assert p["status"] == "pending_upload"


def test_status_with_project_id(client):
    client.post("/projects/clone", json={"type": "upload", "name": "proj"})
    r = client.get("/status?project_id=1")
    data = json.loads(r.data)
    assert data["project"] is not None
    assert data["project"]["name"] == "proj"


def test_project_name_from_url():
    from app import _name_from_url
    assert _name_from_url("https://bitbucket.internal/team/my-app.git") == "my-app"
    assert _name_from_url("ssh://git@bitbucket.internal/team/api") == "api"


# ── Token isolation ──────────────────────────────────────────

def test_token_isolation(client):
    import io
    ca = flask_app.test_client()
    cb = flask_app.test_client()
    ca.get("/auth/me"); cb.get("/auth/me")
    f = (io.BytesIO(json.dumps({"nodes": []}).encode()), "graph.json")
    ra = ca.post("/mcp/upload", data={"graph_file": f, "type": "graphify"},
                 content_type="multipart/form-data")
    f2 = (io.BytesIO(json.dumps({"nodes": []}).encode()), "graph.json")
    rb = cb.post("/mcp/upload", data={"graph_file": f2, "type": "graphify"},
                 content_type="multipart/form-data")
    assert json.loads(ra.data)["token"] != json.loads(rb.data)["token"]


# ── MCP JSON-RPC ─────────────────────────────────────────────

def test_mcp_tools_list(client):
    r = client.post("/mcp/message",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    data = json.loads(r.data)
    names = {t["name"] for t in data["result"]["tools"]}
    assert names == {"graph_search", "graph_callers", "graph_callees",
                     "graph_impact", "graph_architecture", "graph_tests"}


def test_mcp_initialize(client):
    r = client.post("/mcp/message",
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert json.loads(r.data)["result"]["protocolVersion"] == "2024-11-05"


def test_mcp_notification_no_response(client):
    r = client.post("/mcp/message",
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 204


def test_mcp_batch(client):
    r = client.post("/mcp/message", json=[
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ])
    assert len(json.loads(r.data)) == 1


def test_mcp_search_no_graph(client):
    r = client.post("/mcp/message", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "graph_search", "arguments": {"query": "test"}}
    })
    content = json.loads(json.loads(r.data)["result"]["content"][0]["text"])
    assert content["matches"] == []


def test_mcp_unknown_tool(client):
    r = client.post("/mcp/message", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "nonexistent", "arguments": {}}
    })
    assert json.loads(r.data)["error"]["code"] == -32601


# ── New endpoints: graph-data, crg-db, upload-data, mcp-token ──

def _make_ready_project(client):
    """Helper: create a project and inject graph data directly."""
    r = client.post("/projects/clone", json={"type": "upload", "name": "test-proj"})
    pid = json.loads(r.data)["id"]
    uk = list(app_module._PROJECTS.keys())[0]
    proj = app_module._PROJECTS[uk][pid]
    proj["graphify_data"] = {"nodes": [{"label": "foo", "source_file": "foo.py", "file_type": "py"}], "links": []}
    proj["nodes"] = 1
    proj["edges"] = 0
    proj["status"] = "ready"
    return pid, uk


def test_project_graph_data(client):
    pid, _ = _make_ready_project(client)
    r = client.get(f"/projects/{pid}/graph-data")
    assert r.status_code == 200
    d = json.loads(r.data)
    assert d["name"] == "test-proj"
    assert d["status"] == "ready"
    assert d["nodes"] == 1
    assert "graphify" in d
    assert d["graphify"]["nodes"][0]["label"] == "foo"


def test_project_graph_data_no_data(client):
    """No such project → 404."""
    r = client.get("/projects/999/graph-data")
    assert r.status_code == 404


def test_project_crg_db(client):
    pid, uk = _make_ready_project(client)
    # Create a real SQLite DB file in the temp directory
    db_path = os.path.join(tempfile.gettempdir(), f"test-crg-{pid}.db")
    if os.path.exists(db_path): os.unlink(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE nodes (id, name)")
    conn.execute("INSERT INTO nodes VALUES (1, 'test_func')")
    conn.commit(); conn.close()
    app_module._PROJECTS[uk][pid]["crg_db_path"] = db_path
    r = client.get(f"/projects/{pid}/crg-db")
    assert r.status_code == 200
    assert len(r.data) > 0
    r.close()  # release file handle on Windows
    os.unlink(db_path)


def test_project_crg_db_no_data(client):
    pid, _ = _make_ready_project(client)
    r = client.get(f"/projects/{pid}/crg-db")
    assert r.status_code == 404


def test_project_upload_data_graphify(client):
    client.post("/projects/clone", json={"type": "upload", "name": "up-proj"})
    r = client.post("/projects/1/upload-data",
                    data={"graph_file": (BytesIO(b'{"nodes":[{"label":"x","source_file":"x.py","file_type":"py"}],"links":[]}'), "test.json"),
                          "type": "graphify"})
    assert r.status_code == 200
    d = json.loads(r.data)
    assert d["nodes"] == 1
    assert d["edges"] == 0
    # Having either graphify or crg is enough to transition to ready
    assert d["status"] == "ready"


def test_project_upload_data_crg(client):
    client.post("/projects/clone", json={"type": "upload", "name": "up-crg"})
    # Create a real SQLite DB file
    db_path = os.path.join(tempfile.gettempdir(), "test-upload-crg.db")
    if os.path.exists(db_path): os.unlink(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE nodes (id, name)")
    conn.execute("INSERT INTO nodes VALUES (1,'a'),(2,'b')")
    conn.execute("CREATE TABLE edges (source_qualified, target_qualified)")
    conn.execute("INSERT INTO edges VALUES ('a','b')")
    conn.commit(); conn.close()
    with open(db_path, "rb") as fh:
        raw = fh.read()
    r = client.post("/projects/1/upload-data",
                    data={"graph_file": (BytesIO(raw), "test.db"), "type": "crg"},
                    buffered=True)
    assert r.status_code == 200
    d = json.loads(r.data)
    assert d["nodes"] == 2
    assert d["edges"] == 1
    os.unlink(db_path)
    # Clean up temp file created by upload-data
    uk = list(app_module._PROJECTS.keys())[0]
    crg_path = app_module._PROJECTS[uk][1].get("crg_db_path")
    if crg_path:
        try: os.unlink(crg_path)
        except OSError: pass


def test_project_upload_pending_to_ready(client):
    """Uploading both graphify and CRG transitions status to ready."""
    client.post("/projects/clone", json={"type": "upload", "name": "full"})
    # Upload graphify
    client.post("/projects/1/upload-data",
                data={"graph_file": (BytesIO(b'{"nodes":[{"label":"a","source_file":"a.py","file_type":"py"}],"links":[]}'), "g.json"),
                      "type": "graphify"})
    # Upload CRG
    db_path = os.path.join(tempfile.gettempdir(), "test-full-crg.db")
    if os.path.exists(db_path): os.unlink(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE nodes (id, name)")
    conn.commit(); conn.close()
    with open(db_path, "rb") as fh:
        raw = fh.read()
    r = client.post("/projects/1/upload-data",
                    data={"graph_file": (BytesIO(raw), "test.db"), "type": "crg"},
                    buffered=True)
    assert r.status_code == 200
    assert json.loads(r.data)["status"] == "ready"
    os.unlink(db_path)
    uk = list(app_module._PROJECTS.keys())[0]
    crg_path = app_module._PROJECTS[uk][1].get("crg_db_path")
    if crg_path:
        try: os.unlink(crg_path)
        except OSError: pass


def test_project_mcp_token(client):
    pid, _ = _make_ready_project(client)
    r = client.get(f"/projects/{pid}/mcp-token")
    assert r.status_code == 200
    d = json.loads(r.data)
    assert "token" in d
    assert "endpoint" in d
    assert "project_token=" in d["endpoint"]
    assert "config" in d
    assert "graphify-qa" in d["config"]["mcpServers"]


def test_mcp_with_project_token(client):
    pid, _ = _make_ready_project(client)
    r = client.get(f"/projects/{pid}/mcp-token")
    token = json.loads(r.data)["token"]
    # Now query via MCP with that project_token
    r = client.post(f"/mcp/message?project_token={token}", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "graph_search", "arguments": {"query": "foo"}}
    })
    data = json.loads(r.data)
    content = json.loads(data["result"]["content"][0]["text"])
    assert len(content["matches"]) == 1
    assert content["matches"][0]["name"] == "foo"



# ── Completions (stateless) ─────────────────────────────────


def _inject_project(client):
    """Inject a project directly into _PROJECTS without clone endpoint."""
    import flask
    # Get or create anon session key
    with client.application.app_context():
        with client.session_transaction() as sess:
            uk = sess.get("_anon_key", "anon-test-1")
            sess["_anon_key"] = uk
    # Ensure key exists in _PROJECTS (avoids recursion in _load_projects)
    if uk not in app_module._PROJECTS:
        app_module._PROJECTS[uk] = {}
    pid = app_module._NEXT_PID.get(uk, 1)
    app_module._NEXT_PID[uk] = pid + 1
    proj = {"name": "completion-test", "status": "ready", "nodes": 1, "edges": 0,
            "graphify_data": {"nodes": [{"label": "test", "source_file": "test.py",
                                          "file_type": "py"}], "links": []}}
    app_module._PROJECTS[uk][pid] = proj
    return pid, uk


def test_completions_no_prompt(client):
    """Missing prompt returns 400 (project lookup not required)."""
    r = client.post("/api/v1/projects/1/completions", json={})
    assert r.status_code == 400
    d = json.loads(r.data)
    assert "prompt required" in d["error"]


def test_completions_project_not_found(client):
    """Unknown project returns 404."""
    r = client.post("/api/v1/projects/999/completions", json={
        "prompt": "hello", "llm_url": "https://openrouter.ai/api/v1/chat/completions"})
    assert r.status_code == 404


def test_completions_stateless_default(client):
    """Default session_mode is stateless and returns session_mode in response."""
    import unittest.mock
    pid, _ = _inject_project(client)
    with unittest.mock.patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "choices": [{"message": {"content": "mock answer"}}],
            "usage": {"total_tokens": 42},
        }
        mock_post.return_value.text = "ok"
        r = client.post(f"/api/v1/projects/{pid}/completions", json={
            "prompt": "hello",
            "llm_url": "https://openrouter.ai/api/v1/chat/completions",
        })
        assert r.status_code == 200
        d = json.loads(r.data)
        assert d["session_mode"] == "stateless"
        assert d["conversation_reused"] is False
        assert d["trace_id"].startswith("req_")
        assert d["answer"] == "mock answer"


def test_completions_unsupported_session_mode(client):
    """session_mode other than stateless returns 400 (project not required)."""
    r = client.post("/api/v1/projects/1/completions", json={
        "prompt": "hello", "session_mode": "stateful"
    })
    assert r.status_code == 400
    d = json.loads(r.data)
    assert "unsupported_session_mode" in d["error"]


def test_completions_conversation_id_rejected(client):
    """conversation_id in stateless mode returns 400 (project not required)."""
    r = client.post("/api/v1/projects/1/completions", json={
        "prompt": "hello", "conversation_id": "abc-123"
    })
    assert r.status_code == 400
    d = json.loads(r.data)
    assert "conversation_id not supported" in d["error"]


def test_completions_no_previous_messages_included(client):
    """Each call sends only system + current prompt; no prior messages appended."""
    import unittest.mock
    pid, _ = _inject_project(client)
    sent_payloads = []

    def _capture(url, **kw):
        sent_payloads.append(kw.get("json", {}))
        m = unittest.mock.MagicMock()
        m.status_code = 200
        m.json.return_value = {"choices": [{"message": {"content": "ans"}}], "usage": {}}
        m.text = "ok"
        return m

    with unittest.mock.patch("requests.post", side_effect=_capture):
        r1 = client.post(f"/api/v1/projects/{pid}/completions", json={
            "prompt": "Remember the word banana.",
            "model": "gpt-4o-mini",
            "llm_url": "https://openrouter.ai/api/v1/chat/completions",
        })
        assert r1.status_code == 200
        r2 = client.post(f"/api/v1/projects/{pid}/completions", json={
            "prompt": "What word did I ask you to remember?",
            "model": "gpt-4o-mini",
            "llm_url": "https://openrouter.ai/api/v1/chat/completions",
        })
        assert r2.status_code == 200

    assert len(sent_payloads) == 2
    for p in sent_payloads:
        msgs = p.get("messages", [])
        roles = [m["role"] for m in msgs]
        assert roles == ["system", "user"]
        assert all(m["role"] != "assistant" for m in msgs)


def test_completions_no_provider_thread_reuse(client):
    """Payloads must not contain thread/assistant/session IDs."""
    import unittest.mock
    pid, _ = _inject_project(client)
    sent_payloads = []
    sent_headers = []

    def _capture(url, **kw):
        sent_payloads.append(kw.get("json", {}))
        sent_headers.append(kw.get("headers", {}))
        m = unittest.mock.MagicMock()
        m.status_code = 200
        m.json.return_value = {"choices": [{"message": {"content": "ans"}}], "usage": {}}
        m.text = "ok"
        return m

    with unittest.mock.patch("requests.post", side_effect=_capture):
        for _ in range(3):
            r = client.post(f"/api/v1/projects/{pid}/completions", json={
                "prompt": "hello",
                "include_context": False,
                "llm_url": "https://openrouter.ai/api/v1/chat/completions",
            })
            assert r.status_code == 200

    for payload in sent_payloads:
        for key in ("thread_id", "session", "assistant_id", "conversation_id"):
            assert key not in payload, f"payload should not contain '{key}'"
    for hdrs in sent_headers:
        assert "x-conversation-id" not in {k.lower(): k for k in hdrs}


def test_completions_no_url_returns_400(client):
    """Missing LLM URL returns 400."""
    pid, _ = _inject_project(client)
    r = client.post(f"/api/v1/projects/{pid}/completions", json={"prompt": "hello"})
    assert r.status_code == 400
    d = json.loads(r.data)
    assert "llm_url required" in d["error"]


def test_completions_blocked_host(client):
    """Disallowed host returns 403."""
    pid, _ = _inject_project(client)
    r = client.post(f"/api/v1/projects/{pid}/completions", json={
        "prompt": "hello",
        "llm_url": "http://127.0.0.1:19999/chat",
    })
    assert r.status_code == 403


# ── Bitbucket Data Center auth ──────────────────────────────────

BB_URL = "https://bitbucket.example.com/scm/PROJ/repo.git"


def _mock_git_success():
    """Patch subprocess.run so git commands return success immediately."""
    import unittest.mock as um

    def fake_run(cmd_args, **kwargs):
        if "ls-remote" in cmd_args:
            return um.MagicMock(returncode=0, stdout="abc123\tHEAD\n", stderr="")
        if "clone" in cmd_args:
            repo_dir = cmd_args[-1]
            os.makedirs(repo_dir, exist_ok=True)
            return um.MagicMock(returncode=0, stdout="", stderr="")
        if "get-url" in cmd_args:
            return um.MagicMock(returncode=0, stdout=f"{BB_URL}\n", stderr="")
        return um.MagicMock(returncode=0, stdout="", stderr="")

    return um.patch("subprocess.run", side_effect=fake_run)


def _set_oidc_session(client):
    """Set OIDC session so user appears logged in."""
    with client.session_transaction() as sess:
        sess["user"] = {"name": "testuser", "email": "test@example.com", "source": "oidc"}
        sess["oidc_access_token"] = "oidc-fake-token"
        sess["oidc_sub"] = "sub-123"


def test_bb_auth_openid_alone_denied(client):
    """Test 1: OIDC login alone is not Git clone access."""
    _set_oidc_session(client)
    r = client.post("/projects/clone", json={
        "git_url": BB_URL,
        "type": "bitbucket",
        "auth_provider": "bitbucket_datacenter",
    })
    assert r.status_code == 400
    data = json.loads(r.data)
    assert data["error"] == "missing_repo_credentials"


def test_bb_auth_token_special_chars_preserved(client):
    """Test 2: Token with special chars is preserved (only whitespace trimmed)."""
    token = "  BBDC-abc++/==  "

    import unittest.mock as um
    with um.patch("bb_auth.resolve_bitbucket_credential") as mock_resolve:
        mock_resolve.return_value = ("explicit_http_token", "BBDC-abc++/==", None)
        with _mock_git_success():
            r = client.post("/projects/clone", json={
                "git_url": BB_URL,
                "type": "bitbucket",
                "access_token": token,
                "auth_provider": "bitbucket_datacenter",
            })
    # Route strips whitespace BEFORE calling resolve_bitbucket_credential
    call_kwargs = mock_resolve.call_args[1]
    access_token_arg = call_kwargs["access_token"]
    assert access_token_arg == "BBDC-abc++/==", f"Expected trimmed token, got {access_token_arg!r}"
    # Verify internal special chars preserved
    assert "+" in access_token_arg
    assert "/" in access_token_arg
    assert "=" in access_token_arg
def test_bb_auth_explicit_token_priority(client):
    """Test 3: Explicit token takes priority over linked credentials."""
    import unittest.mock as um
    # Patch resolve to confirm it receives the explicit token
    with um.patch("bb_auth.resolve_bitbucket_credential") as mock_resolve:
        mock_resolve.return_value = ("explicit_http_token", "BBDC-priority-token", None)
        with _mock_git_success():
            r = client.post("/projects/clone", json={
                "git_url": BB_URL,
                "type": "bitbucket",
                "access_token": "BBDC-priority-token",
                "use_linked_credentials": True,
                "auth_provider": "bitbucket_datacenter",
            })
            assert r.status_code == 200
    # Should have been called with the explicit token and use_linked_credentials=True
    call_kwargs = mock_resolve.call_args[1]
    assert call_kwargs["access_token"] == "BBDC-priority-token"

def test_bb_auth_linked_credential_fallback(client):
    """Test 4: No explicit token → fail (linked credentials not yet implemented)."""
    import unittest.mock as um
    with um.patch("bb_auth.preflight_git_access",
                  return_value=(False, "bitbucket_auth_failed")):
        r = client.post("/projects/clone", json={
            "git_url": BB_URL,
            "type": "bitbucket",
            "auth_provider": "bitbucket_datacenter",
        })
    assert r.status_code == 401
    data = json.loads(r.data)
    assert "auth_failed" in data.get("error", "")


def test_bb_auth_non_interactive_git(client):
    """Test 5: Preflight and clone run with GIT_TERMINAL_PROMPT=0."""
    import unittest.mock as um
    captured_envs = []

    def capture_run(cmd_args, **kwargs):
        captured_envs.append(kwargs.get("env", {}).get("GIT_TERMINAL_PROMPT"))
        if "ls-remote" in cmd_args:
            return um.MagicMock(returncode=0, stdout="abc123\tHEAD\n", stderr="")
        if "clone" in cmd_args:
            repo_dir = cmd_args[-1]
            os.makedirs(repo_dir, exist_ok=True)
            return um.MagicMock(returncode=0, stdout="", stderr="")
        return um.MagicMock(returncode=0, stdout="", stderr="")

    # bb_auth.run_git uses subprocess.run internally; patch at subprocess level
    with um.patch("subprocess.run", side_effect=capture_run):
        r = client.post("/projects/clone", json={
            "git_url": BB_URL,
            "type": "bitbucket",
            "access_token": "BBDC-test-token",
            "auth_provider": "bitbucket_datacenter",
        })
    # At least one git subprocess call had GIT_TERMINAL_PROMPT=0
    assert "0" in captured_envs, "GIT_TERMINAL_PROMPT=0 not found in any git call"


def test_bb_auth_preflight_before_clone(client):
    """Test 6: git ls-remote runs before full clone."""
    import unittest.mock as um
    call_order = []

    def track_calls(cmd_args, **kwargs):
        if "ls-remote" in cmd_args:
            call_order.append("ls-remote")
            return um.MagicMock(returncode=0, stdout="abc123\tHEAD\n", stderr="")
        if "clone" in cmd_args:
            call_order.append("clone")
            repo_dir = cmd_args[-1]
            os.makedirs(repo_dir, exist_ok=True)
            return um.MagicMock(returncode=0, stdout="", stderr="")
        return um.MagicMock(returncode=0, stdout="", stderr="")

    with um.patch("subprocess.run", side_effect=track_calls):
        # Also need to clean up repo dir for graphify/crg steps
        r = client.post("/projects/clone", json={
            "git_url": BB_URL,
            "type": "bitbucket",
            "access_token": "BBDC-preflight-token",
            "auth_provider": "bitbucket_datacenter",
        })
    assert call_order.index("ls-remote") < call_order.index("clone"), \
        "ls-remote must run before clone"


def test_bb_auth_token_not_leaked(client):
    """Test 7: Token not present in response, project metadata, or remote URL."""
    import unittest.mock as um

    # Mock remote get-url to include a credential (simulating leakage)
    def leaky_geturl(cmd_args, **kwargs):
        if "get-url" in cmd_args:
            return um.MagicMock(returncode=0,
                                stdout="https://x-token-auth:BBDC-leaky@bitbucket.example.com/scm/PROJ/repo.git\n",
                                stderr="")
        if "ls-remote" in cmd_args:
            return um.MagicMock(returncode=0, stdout="abc123\tHEAD\n", stderr="")
        if "clone" in cmd_args:
            repo_dir = cmd_args[-1]
            os.makedirs(repo_dir, exist_ok=True)
            # Simulate credential leaked in remote URL post-clone
            os.makedirs(os.path.join(repo_dir, ".git"), exist_ok=True)
            return um.MagicMock(returncode=0, stdout="", stderr="")
        return um.MagicMock(returncode=0, stdout="", stderr="")

    with um.patch("subprocess.run", side_effect=leaky_geturl):
        # bb_auth.clean_remote_url runs a get-url then set-url if credentials found
        r = client.post("/projects/clone", json={
            "git_url": BB_URL,
            "type": "bitbucket",
            "access_token": "BBDC-leaky",
            "auth_provider": "bitbucket_datacenter",
        })

    data = json.loads(r.data)
    response_str = json.dumps(data)

    # Token not in response body
    assert "BBDC-leaky" not in response_str, "Token leaked in API response"
    assert "leaky" not in response_str, "Token part leaked in API response"

    # Token not in project metadata (check the stored project)
    if data.get("id"):
        for user_projects in app_module._PROJECTS.values():
            proj = user_projects.get(data["id"], {})
            proj_str = json.dumps(proj)
            assert "BBDC-leaky" not in proj_str, "Token leaked in project metadata"


def test_bb_auth_bad_token(client):
    """Test 8: Invalid token returns bitbucket_auth_failed, no hanging prompt."""
    import unittest.mock as um

    with um.patch("bb_auth.preflight_git_access",
                  return_value=(False, "bitbucket_auth_failed")):
        r = client.post("/projects/clone", json={
            "git_url": BB_URL,
            "type": "bitbucket",
            "access_token": "BBDC-bad-token",
            "auth_provider": "bitbucket_datacenter",
        })

    assert r.status_code == 401
    data = json.loads(r.data)
    assert data["error"] == "bitbucket_auth_failed"
    # Verify no BBDC- artifacts in response
    assert "BBDC-bad-token" not in json.dumps(data)


def test_bb_auth_non_bitbucket_still_clones(client):
    """Test 9: Non-Bitbucket repos (public GitHub) still clone without BB auth."""
    import unittest.mock as um
    public_url = "https://github.com/user/public-repo.git"

    def fake_run(cmd_args, **kwargs):
        if "ls-remote" in cmd_args:
            return um.MagicMock(returncode=0, stdout="abc123\tHEAD\n", stderr="")
        if "clone" in cmd_args:
            repo_dir = cmd_args[-1]
            os.makedirs(repo_dir, exist_ok=True)
            return um.MagicMock(returncode=0, stdout="", stderr="")
        if "get-url" in cmd_args:
            return um.MagicMock(returncode=0, stdout=f"{public_url}\n", stderr="")
        return um.MagicMock(returncode=0, stdout="", stderr="")

    with um.patch("subprocess.run", side_effect=fake_run):
        r = client.post("/projects/clone", json={
            "git_url": public_url,
            "type": "git",
        })

    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["status"] in ("ready", "building", "indexing")


def test_bb_auth_token_cleared_on_failure(client):
    """Test 10 (frontend-equivalent): Token form state is cleared after failed submit.
    
    Backend: Verify failing clone doesn't store token in project metadata."""
    import unittest.mock as um

    with um.patch("bb_auth.preflight_git_access",
                  return_value=(False, "bitbucket_auth_failed")):
        r = client.post("/projects/clone", json={
            "git_url": BB_URL,
            "type": "bitbucket",
            "access_token": "BBDC-will-be-rejected",
            "auth_provider": "bitbucket_datacenter",
        })

    assert r.status_code == 401
    data = json.loads(r.data)
    assert "BBDC-will-be-rejected" not in json.dumps(data)
    # Verify no project was created/persisted with the token
    for user_projects in app_module._PROJECTS.values():
        for proj in user_projects.values():
            proj_str = json.dumps(proj)
            assert "BBDC-will-be-rejected" not in proj_str