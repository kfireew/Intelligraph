"""
graphify-qa MCP server — standalone, no MongoDB.
Run locally with your graph files. Claude Code connects via .mcp.json.

Usage:
  python mcp_server_standalone.py --crg-db .code-review-graph/graph.db --graphify graphify-out/graph.json
  python mcp_server_standalone.py --graphify graphify-out/graph.json    # graphify only
  python mcp_server_standalone.py --crg-db .code-review-graph/graph.db  # CRG only

Claude Code .mcp.json:
{
  "mcpServers": {
    "graphify-qa": {
      "command": "python",
      "args": ["mcp_server_standalone.py", "--crg-db", ".code-review-graph/graph.db", "--graphify", "graphify-out/graph.json"],
      "cwd": "/path/to/your/project"
    }
  }
}
"""

import json
import os
import sqlite3
import sys

from flask import Flask, jsonify, request

app = Flask(__name__)

CRG_DB = None
GRAPHIFY_JSON = None
GRAPHIFY_DATA = None  # parsed JSON in memory


def load_data():
    global GRAPHIFY_DATA
    if GRAPHIFY_JSON and os.path.exists(GRAPHIFY_JSON):
        with open(GRAPHIFY_JSON) as f:
            GRAPHIFY_DATA = json.load(f)


def crg_query(sql, params=()):
    if not CRG_DB:
        return []
    conn = sqlite3.connect(f"file:{CRG_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


# ── Tool definitions ─────────────────────────────────────────────

TOOLS = [
    {"name": "graph_search", "description": "Search codebase knowledge graph for functions, classes, files.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "graph_callers", "description": "Find all callers/importers of a symbol.", "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "graph_callees", "description": "Find all callees/imports of a symbol.", "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "graph_impact", "description": "Blast radius: dependents, risk, affected flows.", "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "graph_architecture", "description": "Architecture overview: communities, flows, kind stats.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "graph_tests", "description": "Find test files/functions related to a symbol.", "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
]


# ── Graph queries ────────────────────────────────────────────────

def _search(query, limit=15):
    results = []
    if CRG_DB:
        results = crg_query("""
            SELECT n.kind, n.name, n.qualified_name, n.file_path, n.line_start, n.signature
            FROM nodes_fts f JOIN nodes n ON n.qualified_name = f.qualified_name
            WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?
        """, (query, limit))
    if GRAPHIFY_DATA and isinstance(GRAPHIFY_DATA, dict):
        gf_nodes = GRAPHIFY_DATA.get("nodes", [])
        q = query.lower()
        gf_results = []
        for n in gf_nodes:
            label = (n.get("label") or "").lower()
            fname = (n.get("source_file") or "").lower()
            if q in label or q in fname:
                gf_results.append({"kind": n.get("file_type","?"), "name": n.get("label",""),
                                   "file_path": n.get("source_file",""),
                                   "source": "graphify"})
            if len(gf_results) >= limit:
                break
        results = list(results) + gf_results
    return results[:limit]


def _callers(symbol, limit=30):
    edges = crg_query("""
        SELECT kind, source_qualified, file_path, line FROM edges
        WHERE target_qualified LIKE ? LIMIT ?
    """, (f"%{symbol}%", limit)) if CRG_DB else []
    return edges


def _callees(symbol, limit=30):
    edges = crg_query("""
        SELECT kind, target_qualified, file_path, line FROM edges
        WHERE source_qualified LIKE ? LIMIT ?
    """, (f"%{symbol}%", limit)) if CRG_DB else []
    return edges


def _impact(symbol):
    matched = _search(symbol, 5)
    qnames = [m["qualified_name"] for m in matched if m.get("qualified_name")]
    deps = []
    if qnames and CRG_DB:
        placeholders = ",".join("?" * len(qnames))
        deps = crg_query(f"""
            SELECT kind, source_qualified, file_path, line FROM edges
            WHERE target_qualified IN ({placeholders}) AND kind IN ('CALLS','IMPORTS')
            LIMIT 50
        """, qnames)
    flows = crg_query("""
        SELECT DISTINCT f.name, f.criticality FROM flows f
        JOIN flow_memberships fm ON fm.flow_id = f.id
        JOIN nodes n ON n.id = fm.node_id
        WHERE n.qualified_name LIKE ? ORDER BY f.criticality DESC LIMIT 10
    """, (f"%{symbol}%",)) if CRG_DB else []
    return {"matched": matched, "dependents": deps, "flows": flows}


def _architecture():
    result = {}
    if CRG_DB:
        result["communities"] = crg_query("SELECT community_id, name, purpose, risk, size, dominant_language FROM community_summaries ORDER BY size DESC")
        result["flows"] = crg_query("SELECT name, criticality, node_count, depth FROM flows ORDER BY criticality DESC LIMIT 15")
        result["kinds"] = crg_query("SELECT kind, COUNT(*) as count FROM nodes GROUP BY kind ORDER BY count DESC")
    return result


def _tests(symbol, limit=20):
    tests = crg_query("""
        SELECT name, file_path, line_start FROM nodes
        WHERE is_test=1 AND (name LIKE ? OR file_path LIKE ?) LIMIT ?
    """, (f"%{symbol}%", f"%{symbol}%", limit)) if CRG_DB else []
    return tests


# ── JSON-RPC handler ────────────────────────────────────────────

def handle(msg):
    method = msg.get("method", "")
    mid = msg.get("id")

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "graphify-qa-standalone", "version": "1.0.0"},
            "capabilities": {"tools": {}},
        }}

    if method == "notifications/initialized":
        return None

    if method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name", "")
        args = params.get("arguments", {})
        try:
            if name == "graph_search":
                r = {"matches": _search(args.get("query", ""))}
            elif name == "graph_callers":
                r = {"callers": _callers(args.get("symbol", ""))}
            elif name == "graph_callees":
                r = {"callees": _callees(args.get("symbol", ""))}
            elif name == "graph_impact":
                r = _impact(args.get("symbol", ""))
            elif name == "graph_architecture":
                r = _architecture()
            elif name == "graph_tests":
                r = {"tests": _tests(args.get("symbol", ""))}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown: {name}"}}
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(r, default=str)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(e)}}

    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method: {method}"}}


@app.route("/mcp/message", methods=["POST"])
def message():
    msg = request.get_json(force=True)
    if isinstance(msg, list):
        return jsonify([r for r in (handle(m) for m in msg) if r is not None])
    result = handle(msg)
    return ("", 204) if result is None else jsonify(result)


@app.route("/")
def home():
    return jsonify({"server": "graphify-qa MCP (standalone)", "endpoint": "/mcp/message", "tools": len(TOOLS)})


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--crg-db")
    p.add_argument("--graphify")
    p.add_argument("--port", type=int, default=5051)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()

    if args.crg_db:
        CRG_DB = os.path.abspath(args.crg_db)
        if not os.path.exists(CRG_DB):
            print(f"ERROR: CRG DB not found: {CRG_DB}", file=sys.stderr)
            sys.exit(1)
        print(f"CRG DB:    {CRG_DB}")

    if args.graphify:
        GRAPHIFY_JSON = os.path.abspath(args.graphify)
        load_data()
        if not GRAPHIFY_DATA:
            print(f"ERROR: graphify file not found or invalid: {GRAPHIFY_JSON}", file=sys.stderr)
            sys.exit(1)
        print(f"Graphify:  {GRAPHIFY_JSON}")

    if not CRG_DB and not GRAPHIFY_DATA:
        print("ERROR: need --crg-db and/or --graphify", file=sys.stderr)
        sys.exit(1)

    print(f"MCP:       http://{args.host}:{args.port}/mcp/message")
    print(f"Tools:     {len(TOOLS)}")
    app.run(host=args.host, port=args.port, debug=False)