"""
Intelligraph MCP server — standalone, uses same retrieval.py runtime as web UI.
Run locally with your graph files. Claude Code connects via .mcp.json.

Usage:
  python mcp_server_standalone.py --crg-db .code-review-graph/graph.db --graphify graphify-out/graph.json

Claude Code .mcp.json:
{
  "mcpServers": {
    "intelligraph": {
      "command": "python",
      "args": ["mcp_server_standalone.py", "--crg-db", ".code-review-graph/graph.db", "--graphify", "graphify-out/graph.json"],
      "cwd": "/path/to/your/project"
    }
  }
}
"""

import json
import os
import sys

# Ensure the runtime module is importable
RUNTIME_DIR = os.path.dirname(os.path.abspath(__file__))
if RUNTIME_DIR not in sys.path:
    sys.path.insert(0, RUNTIME_DIR)

from retrieval import retrieve_context

from flask import Flask, jsonify, request

app = Flask(__name__)

CRG_DB = None
GRAPHIFY_DATA = None  # parsed graph.json in memory


def load_data():
    global GRAPHIFY_DATA
    if args.graphify and os.path.exists(args.graphify):
        with open(args.graphify, encoding="utf-8") as f:
            GRAPHIFY_DATA = json.load(f)


# ── Build project dict for retrieve_context ──

def _build_project():
    """Build a project dict that retrieva_context can use."""
    return {
        "graphify_data": GRAPHIFY_DATA or {},
        "crg_db_path": CRG_DB or "",
        "repo_dir": os.path.dirname(args.graphify) if args.graphify else None,
        "_G": None,
    }


# ── MCP tools ──

def tool_search(query, limit=15):
    """Search codebase graph for symbols matching query."""
    proj = _build_project()
    result = retrieve_context(proj, query)
    files = result.get("files", [])[:limit]
    ctx = result.get("context", "")[:800]
    return {"matches": files, "context": ctx}


def tool_callers(name, limit=15):
    """Find callers of a symbol (incoming edges)."""
    query = f"who calls {name}"
    proj = _build_project()
    result = retrieve_context(proj, query)
    files = result.get("files", [])[:limit]
    ctx = result.get("context", "")[:800]
    return {"matches": files, "context": ctx}


def tool_callees(name, limit=15):
    """Find callees of a symbol (outgoing edges)."""
    query = f"what does {name} call"
    proj = _build_project()
    result = retrieve_context(proj, query)
    files = result.get("files", [])[:limit]
    ctx = result.get("context", "")[:800]
    return {"matches": files, "context": ctx}


def tool_impact(name, limit=15):
    """Find what would break if a symbol were changed."""
    query = f"impact of {name} what breaks"
    proj = _build_project()
    result = retrieve_context(proj, query)
    files = result.get("files", [])[:limit]
    ctx = result.get("context", "")[:800]
    return {"matches": files, "context": ctx}


def tool_architecture(prompt, limit=15):
    """Get architecture overview of a component or the codebase."""
    proj = _build_project()
    result = retrieve_context(proj, prompt or "architecture overview")
    files = result.get("files", [])[:limit]
    ctx = result.get("context", "")[:800]
    return {"matches": files, "context": ctx}


def tool_tests(name, limit=15):
    """Find test files related to a symbol."""
    query = f"test {name}"
    proj = _build_project()
    result = retrieve_context(proj, query)
    files = result.get("files", [])[:limit]
    ctx = result.get("context", "")[:800]
    return {"matches": files, "context": ctx}


# ── Tool registry ──

TOOLS = {
    "search": tool_search,
    "callers": tool_callers,
    "callees": tool_callees,
    "impact": tool_impact,
    "architecture": tool_architecture,
    "tests": tool_tests,
}


# ── JSON-RPC endpoint ──

@app.route("/mcp", methods=["POST"])
def mcp_handle():
    data = request.get_json(silent=True) or {}
    tool_name = data.get("tool", "")
    params = data.get("params", {})
    if tool_name not in TOOLS:
        return jsonify({"error": f"unknown tool: {tool_name}"}), 400
    try:
        result = TOOLS[tool_name](**params)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)[:500]}), 500


# ── CLI ──

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--crg-db", help="Path to .code-review-graph/graph.db")
    parser.add_argument("--graphify", help="Path to graphify-out/graph.json")
    parser.add_argument("--port", type=int, default=0)
    global args
    args = parser.parse_args()
    CRG_DB = args.crg_db
    load_data()
    port = args.port or int(os.environ.get("MCP_PORT", 8765))
    print(f"Intelligraph MCP running on port {port}", file=sys.stderr)
    app.run(host="0.0.0.0", port=port)