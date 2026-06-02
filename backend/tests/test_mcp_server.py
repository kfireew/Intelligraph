"""Tests for mcp_server.py — embedded MCP Blueprint."""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask
from mcp_server import mcp_bp, mcp_tokens as mcp_tokens_module
from app import mcp_tokens as app_tokens


@pytest.fixture
def mcp_client():
    """Flask test client with MCP blueprint, no app.py routes."""
    test_app = Flask(__name__)
    test_app.secret_key = "test"
    test_app.register_blueprint(mcp_bp)
    # Wire the token dict so MCP can find graphs
    import mcp_server
    mcp_server.mcp_tokens = app_tokens
    test_app.config["TESTING"] = True
    with test_app.test_client() as c:
        yield c


# ── JSON-RPC protocol ────────────────────────────────────────────

def test_tools_list(mcp_client):
    r = mcp_client.post("/mcp/message", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    data = json.loads(r.data)
    assert len(data["result"]["tools"]) == 6


def test_unknown_method(mcp_client):
    r = mcp_client.post("/mcp/message", json={"jsonrpc": "2.0", "id": 1, "method": "unknown"})
    data = json.loads(r.data)
    assert data["error"]["code"] == -32601


def test_unknown_tool(mcp_client):
    r = mcp_client.post("/mcp/message", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "nonexistent", "arguments": {}}
    })
    data = json.loads(r.data)
    assert data["error"]["code"] == -32601


def test_parse_error(mcp_client):
    r = mcp_client.post("/mcp/message", data="not json", content_type="application/json")
    assert r.status_code == 400


# ── No graph → empty results ─────────────────────────────────────

def test_search_no_graph(mcp_client):
    r = mcp_client.post("/mcp/message", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "graph_search", "arguments": {"query": "test"}}
    })
    data = json.loads(r.data)
    content = json.loads(data["result"]["content"][0]["text"])
    assert content["matches"] == []


def test_architecture_no_graph(mcp_client):
    r = mcp_client.post("/mcp/message", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "graph_architecture", "arguments": {}}
    })
    data = json.loads(r.data)
    content = json.loads(data["result"]["content"][0]["text"])
    assert content["communities"] == []
    assert content["flows"] == []
    assert content["kinds"] == []


def test_callers_no_graph(mcp_client):
    r = mcp_client.post("/mcp/message", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "graph_callers", "arguments": {"symbol": "test"}}
    })
    data = json.loads(r.data)
    content = json.loads(data["result"]["content"][0]["text"])
    assert content["callers"] == []


# ── Token isolation ──────────────────────────────────────────────

def test_with_token_returns_results(mcp_client):
    """MCP query with a valid token should find data (after upload)."""
    # Simulate an uploaded graph by populating the token dict
    test_data = {"nodes": [{"id": "n1", "label": "test_func", "file_type": "code",
                             "source_file": "test.py", "source_location": "L10"}],
                  "links": []}
    app_tokens["test-token-123"] = {"graphify": test_data}
    try:
        r = mcp_client.post("/mcp/message?token=test-token-123", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "graph_search", "arguments": {"query": "test_func"}}
        })
        data = json.loads(r.data)
        content = json.loads(data["result"]["content"][0]["text"])
        assert len(content["matches"]) == 1
        assert content["matches"][0]["name"] == "test_func"
    finally:
        app_tokens.pop("test-token-123", None)


def test_wrong_token_returns_empty(mcp_client):
    """Query with non-existent token → empty results."""
    r = mcp_client.post("/mcp/message?token=nonexistent", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "graph_search", "arguments": {"query": "test"}}
    })
    data = json.loads(r.data)
    content = json.loads(data["result"]["content"][0]["text"])
    assert content["matches"] == []


# ── Tool argument validation ─────────────────────────────────────

def test_search_missing_query(mcp_client):
    """Calling graph_search without query arg should still work (graceful)."""
    r = mcp_client.post("/mcp/message", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "graph_search", "arguments": {}}
    })
    data = json.loads(r.data)
    # Should return empty, not crash
    content = json.loads(data["result"]["content"][0]["text"])
    assert "matches" in content


def test_callers_missing_symbol(mcp_client):
    r = mcp_client.post("/mcp/message", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "graph_callers", "arguments": {}}
    })
    data = json.loads(r.data)
    content = json.loads(data["result"]["content"][0]["text"])
    assert content["callers"] == []


# ── Project token support ────────────────────────────────────────

def test_get_graphs_with_project_token(mcp_client):
    """project_token param resolves to project data without Flask session."""
    import mcp_server
    # Set up _projects_ref with a mock project
    mcp_server._projects_ref = {
        "test-user": {
            42: {
                "graphify_data": {"nodes": [{"label": "bar", "source_file": "bar.py", "file_type": "py"}], "links": []},
                "crg_db_path": None,
            }
        }
    }
    # Inject _project_tokens into app module
    import app as app_module
    app_module._project_tokens["ptok-123"] = ("test-user", 42)

    r = mcp_client.post("/mcp/message?project_token=ptok-123", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "graph_search", "arguments": {"query": "bar"}}
    })
    data = json.loads(r.data)
    content = json.loads(data["result"]["content"][0]["text"])
    assert len(content["matches"]) == 1
    assert content["matches"][0]["name"] == "bar"

    # Clean up
    app_module._project_tokens.clear()
    mcp_server._projects_ref = None


def test_missing_tools(mcp_client):
    """Six tools should be listed."""
    r = mcp_client.post("/mcp/message", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    data = json.loads(r.data)
    names = [t["name"] for t in data["result"]["tools"]]
    assert names == ["graph_search", "graph_callers", "graph_callees", "graph_impact", "graph_architecture", "graph_tests"]