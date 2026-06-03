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
    app_module.mcp_tokens.clear()
    app_module._project_tokens.clear()
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


def test_mcp_wiring_in_wsgi(client):
    """mcp_server.mcp_tokens IS app.mcp_tokens (same object, not a copy)."""
    import mcp_server
    assert mcp_server.mcp_tokens is app_module.mcp_tokens