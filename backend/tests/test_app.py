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


BB_URL = "https://bitbucket.example.com/scm/PROJ/repo.git"


def _mock_git_success():
    """Patch subprocess.run so all git commands return success."""
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
        if "set-url" in cmd_args:
            return um.MagicMock(returncode=0, stdout="", stderr="")
        return um.MagicMock(returncode=0, stdout="", stderr="")

    return um.patch("subprocess.run", side_effect=fake_run)


def _clone_capture_helper():
    """Return (captured, patcher) — intercepts subprocess.run and captures git args + env."""
    import unittest.mock as um
    captured = {"ls_remote_args": None, "clone_args": None, "ls_remote_env": None, "calls": []}

    def capture(cmd_args, **kwargs):
        captured["calls"].append(cmd_args)
        if "ls-remote" in cmd_args:
            captured["ls_remote_args"] = cmd_args
            captured["ls_remote_env"] = kwargs.get("env", {}).copy()
            return um.MagicMock(returncode=0, stdout="abc123\tHEAD\n", stderr="")
        if "clone" in cmd_args:
            captured["clone_args"] = cmd_args
            captured["clone_env"] = kwargs.get("env", {}).copy()
            repo_dir = cmd_args[-1]
            os.makedirs(repo_dir, exist_ok=True)
            return um.MagicMock(returncode=0, stdout="", stderr="")
        if "get-url" in cmd_args:
            return um.MagicMock(returncode=0, stdout=f"{BB_URL}\n", stderr="")
        if "set-url" in cmd_args:
            return um.MagicMock(returncode=0, stdout="", stderr="")
        return um.MagicMock(returncode=0, stdout="", stderr="")

    return captured, um.patch("subprocess.run", side_effect=capture)


def test_bb_bearer_token_special_chars(client):
    """Token with ++, /, = is preserved in -c http.extraHeader arg."""
    captured, patcher = _clone_capture_helper()
    with patcher:
        r = client.post("/projects/clone", json={
            "git_url": BB_URL,
            "type": "bitbucket",
            "access_token": "  BBDC-abc++/==  ",
            "auth_mode": "bitbucket_datacenter_bearer",
        })

    assert r.status_code == 200
    args = captured["ls_remote_args"]
    assert args is not None, "ls-remote was never called"
    # Find the -c http.extraHeader argument
    extra_idx = None
    for i, arg in enumerate(args):
        if arg.startswith("http.extraHeader="):
            extra_idx = i
            break
    assert extra_idx is not None, f"http.extraHeader not found in args: {args}"
    assert "Authorization: Bearer BBDC-abc++/==" in args[extra_idx]
    # Also verify -c http.sslVerify=false present
    assert "-c" in args
    assert any("http.sslVerify=false" in a for a in args)
    # GIT_CONFIG_COUNT not used
    env = captured["ls_remote_env"]
    assert "GIT_CONFIG_COUNT" not in env

def test_bb_bearer_preflight_uses_bearer_header(client):
    """ls-remote command contains -c http.sslVerify=false and -c http.extraHeader=Authorization: Bearer ..."""
    captured, patcher = _clone_capture_helper()
    with patcher:
        r = client.post("/projects/clone", json={
            "git_url": BB_URL,
            "type": "bitbucket",
            "access_token": "BBDC-secret-token",
            "auth_mode": "bitbucket_datacenter_bearer",
        })

    assert r.status_code == 200
    args = captured["ls_remote_args"]
    assert args is not None
    # Verify -c http.sslVerify=false
    assert "-c" in args
    assert any("http.sslVerify=false" in a for a in args)
    # Verify -c http.extraHeader=Authorization: Bearer ...
    assert any("Authorization: Bearer BBDC-secret-token" in a for a in args)
    # Token NOT in clone URL part of args
    for a in args:
        if a.startswith("https://") or a.startswith("http://"):
            assert "BBDC" not in a, f"Token leaked in URL: {a}"
            assert "secret" not in a, f"Token leaked in URL: {a}"
    # GIT_CONFIG_COUNT not in env


# ── Test 3: No username required for bearer mode ────────────────────

def test_bb_bearer_no_username_required(client):
    """auth_mode=bitbucket_datacenter_bearer succeeds without username."""
    with _mock_git_success():
        r = client.post("/projects/clone", json={
            "git_url": BB_URL,
            "type": "bitbucket",
            "access_token": "BBDC-no-username-token",
            "auth_mode": "bitbucket_datacenter_bearer",
        })
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["status"] in ("ready", "building", "indexing")


# ── Test 4: SSL verify always false ───────────────────────────────────

def test_bb_ssl_verify_always_false_no_auth():
    """No token → -c http.sslVerify=false in git args."""
    from app import _git_auth_args, _git_env
    args = _git_auth_args()
    assert "-c" in args
    assert any(a == "http.sslVerify=false" for a in args)
    assert not any("http.extraHeader" in a for a in args)
    env = _git_env()
    assert env.get("GIT_TERMINAL_PROMPT") == "0"
    # No GIT_CONFIG_COUNT in env
    assert "GIT_CONFIG_COUNT" not in env


def test_bb_ssl_verify_always_false_with_auth():
    """Bearer token → both -c http.sslVerify=false and -c http.extraHeader=in args."""
    from app import _git_auth_args
    args = _git_auth_args(access_token="BBDC-test-token")
    assert "-c" in args
    assert any(a == "http.sslVerify=false" for a in args)
    assert any("http.extraHeader=Authorization: Bearer BBDC-test-token" in a for a in args)


def test_bb_ssl_verify_always_no_env_var_needed():
    """No env vars set → sslVerify=false still in args."""
    import os
    for k in ("INTELLIGRAPH_GIT_SSL_VERIFY", "INTELLIGRAPH_GIT_SSL_CAINFO"):
        os.environ.pop(k, None)
    from app import _git_auth_args
    args = _git_auth_args()
    assert any(a == "http.sslVerify=false" for a in args)

# ── Test 5: Public clone still works ────────────────────────────────

def test_bb_public_clone_still_works(client):
    """Non-Bitbucket repo without token still clones."""
    public_url = "https://github.com/user/public-repo.git"

    import unittest.mock as um

    def fake_run(cmd_args, **kwargs):
        if "ls-remote" in cmd_args:
            return um.MagicMock(returncode=0, stdout="abc123\tHEAD\n", stderr="")
        if "clone" in cmd_args:
            repo_dir = cmd_args[-1]
            os.makedirs(repo_dir, exist_ok=True)
            return um.MagicMock(returncode=0, stdout="", stderr="")
        if "get-url" in cmd_args:
            return um.MagicMock(returncode=0, stdout=f"{public_url}\n", stderr="")
        if "set-url" in cmd_args:
            return um.MagicMock(returncode=0, stdout="", stderr="")
        return um.MagicMock(returncode=0, stdout="", stderr="")

    with um.patch("subprocess.run", side_effect=fake_run):
        r = client.post("/projects/clone", json={
            "git_url": public_url,
            "type": "git",
        })

    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["status"] in ("ready", "building", "indexing")


# ── Test 6: Bad token returns auth error ───────────────────────────

def test_bb_bad_token_returns_auth_error(client):
    """Rejected Bearer token returns bitbucket_auth_failed."""
    import unittest.mock as um

    with um.patch("subprocess.run") as mock_run:
        # ls-remote fails with auth error
        mock_run.side_effect = [
            um.MagicMock(returncode=128, stdout="", stderr="fatal: Authentication failed for 'https://bitbucket.example.com/scm/PROJ/repo.git'"),
        ]
        r = client.post("/projects/clone", json={
            "git_url": BB_URL,
            "type": "bitbucket",
            "access_token": "BBDC-bad-token",
            "auth_mode": "bitbucket_datacenter_bearer",
        })

    assert r.status_code == 401
    data = json.loads(r.data)
    assert data["error"] == "bitbucket_auth_failed"
    assert "BBDC-bad-token" not in json.dumps(data)


# ── Test 7: Preflight runs before clone ────────────────────────────

def test_bb_preflight_before_clone(client):
    """git ls-remote runs before git clone."""
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
        if "get-url" in cmd_args:
            return um.MagicMock(returncode=0, stdout=f"{BB_URL}\n", stderr="")
        if "set-url" in cmd_args:
            return um.MagicMock(returncode=0, stdout="", stderr="")
        return um.MagicMock(returncode=0, stdout="", stderr="")

    with um.patch("subprocess.run", side_effect=track_calls):
        r = client.post("/projects/clone", json={
            "git_url": BB_URL,
            "type": "bitbucket",
            "access_token": "BBDC-test-token",
            "auth_mode": "bitbucket_datacenter_bearer",
        })

    assert "ls-remote" in call_order
    assert "clone" in call_order
    assert call_order.index("ls-remote") < call_order.index("clone"), \
        "ls-remote must run before clone"


# ── Test 8: GIT_TERMINAL_PROMPT=0 always set ───────────────────────

def test_bb_git_terminal_prompt_zero(client):
    """Every git subprocess call has GIT_TERMINAL_PROMPT=0."""
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
        if "get-url" in cmd_args:
            return um.MagicMock(returncode=0, stdout=f"{BB_URL}\n", stderr="")
        if "set-url" in cmd_args:
            return um.MagicMock(returncode=0, stdout="", stderr="")
        return um.MagicMock(returncode=0, stdout="", stderr="")

    with um.patch("subprocess.run", side_effect=capture_run):
        r = client.post("/projects/clone", json={
            "git_url": BB_URL,
            "type": "bitbucket",
            "access_token": "BBDC-test-token",
            "auth_mode": "bitbucket_datacenter_bearer",
        })

    assert "0" in captured_envs, "GIT_TERMINAL_PROMPT=0 not found in any git call"


# ── Test 9: Token not leaked in response or metadata ──────────────

def test_bb_token_not_leaked(client):
    """Token not in API response or project metadata."""
    import unittest.mock as um

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
            os.makedirs(os.path.join(repo_dir, ".git"), exist_ok=True)
            return um.MagicMock(returncode=0, stdout="", stderr="")
        if "set-url" in cmd_args:
            return um.MagicMock(returncode=0, stdout="", stderr="")
        return um.MagicMock(returncode=0, stdout="", stderr="")

    with um.patch("subprocess.run", side_effect=leaky_geturl):
        r = client.post("/projects/clone", json={
            "git_url": BB_URL,
            "type": "bitbucket",
            "access_token": "BBDC-leaky",
            "auth_mode": "bitbucket_datacenter_bearer",
        })

    data = json.loads(r.data)
    response_str = json.dumps(data)
    assert "BBDC-leaky" not in response_str, "Token leaked in API response"
    # Check project metadata too
    if data.get("id"):
        from app import _PROJECTS as projects_store
        for user_projects in projects_store.values():
            proj = user_projects.get(data["id"], {})
            proj_str = json.dumps(proj)
            assert "BBDC-leaky" not in proj_str, "Token leaked in project metadata"


# ── Test 10: Dry-run returns redacted command shape ──────────────────

def test_bb_dry_run_returns_cmd_shape(client):
    """Dry-run returns redacted command shape, no subprocess calls."""
    import unittest.mock as um
    subprocess_called = []

    def track(cmd_args, **kwargs):
        subprocess_called.append(cmd_args)
        return um.MagicMock(returncode=0, stdout="", stderr="")

    with um.patch("subprocess.run", side_effect=track):
        r = client.post("/projects/clone", json={
            "git_url": "https://bitbucket.app.iaf/scm/romach/repo.git",
            "access_token": "BBDC-abc++/==",
            "auth_mode": "bitbucket_datacenter_bearer",
            "dry_run": True,
        })

    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["dry_run"] is True
    assert data["ok"] is True
    # No subprocess calls
    assert len(subprocess_called) == 0, f"subprocess was called: {subprocess_called}"
    # Command shape
    pre = data["preflight_cmd_redacted"]
    assert "git" in pre
    assert "http.sslVerify=false" in pre
    assert "ls-remote" in pre
    assert "https://bitbucket.app.iaf/scm/romach/repo.git" in pre
    # Bearer header present but redacted
    assert any("http.extraHeader=Authorization: Bearer" in a for a in pre)
    assert not any("BBDC-abc++/==" in a for a in pre), "Token leaked in preflight cmd"
    # Clone shape
    cl = data["clone_cmd_redacted"]
    assert "clone" in cl
    assert "--depth" in cl
    assert "1" in cl
    assert "<repo_dir>" in cl
    # Token fields
    assert data["token_present"] is True
    assert data["token_prefix"] == "BBDC-..."
    assert data["token_length"] == 13


def test_bb_bearer_no_token_returns_missing(client):
    """auth_mode=bitbucket_datacenter_bearer without access_token returns 400."""
    r = client.post("/projects/clone", json={
        "git_url": "https://bitbucket.app.iaf/scm/romach/repo.git",
        "auth_mode": "bitbucket_datacenter_bearer",
    })
    assert r.status_code == 400
    data = json.loads(r.data)
    assert data["error"] == "missing_repo_credentials"


# ── Test 12: Bearer mode never activates URL fallback ──────────────

def test_bb_bearer_no_url_embedding(client):
    """Bearer mode: URL stays clean, token not embedded in URL."""
    captured, patcher = _clone_capture_helper()
    with patcher:
        client.post("/projects/clone", json={
            "git_url": "https://bitbucket.app.iaf/scm/romach/repo.git",
            "access_token": "BBDC-no-url-embed",
            "auth_mode": "bitbucket_datacenter_bearer",
        })

    # Check ls-remote arg for URL — should be original clean URL
    args = captured["ls_remote_args"]
    assert args is not None
    # The URL arg should be the last element
    url_arg = [a for a in args if a.startswith("https://bitbucket.app.iaf/")]
    assert len(url_arg) == 1
    assert "x-token-auth" not in url_arg[0]
    assert "BBDC-no-url-embed" not in url_arg[0]
    # Same for clone
    clone_args = captured["clone_args"]
    assert clone_args is not None
    clone_urls = [a for a in clone_args if a.startswith("https://bitbucket.app.iaf/")]
    assert len(clone_urls) == 1
    assert "x-token-auth" not in clone_urls[0]
    assert "BBDC-no-url-embed" not in clone_urls[0]