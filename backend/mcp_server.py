"""
graphify-qa MCP server — embedded Blueprint for the pod.
Reads from mcp_tokens (set by /mcp/upload) or _projects_ref (Bitbucket clones).
"""

import json
import sqlite3
import os

from flask import Blueprint, jsonify, request

mcp_bp = Blueprint("mcp", __name__, url_prefix="/mcp")
mcp_tokens = {}       # Set by app.py at startup
_projects_ref = None   # Set by app.py at startup (pointer to app._PROJECTS)


def _get_graphs():
    """Get graph data via ?token=, ?project_id=, or ?project_token= query param."""
    token = request.args.get("token")
    if token and token in mcp_tokens:
        return mcp_tokens[token]

    # Project token — no Flask session required (works for external MCP clients)
    project_token = request.args.get("project_token")
    if project_token and _projects_ref:
        try:
            from app import _project_tokens
        except ImportError:
            pass
        else:
            entry = _project_tokens.get(project_token)
            if entry:
                uk, pid = entry
                proj = _projects_ref.get(uk, {}).get(pid, {})
                if proj:
                    return {
                        "graphify": proj.get("graphify_data"),
                        "crg_db": proj.get("crg_db_path"),
                    }

    pid = request.args.get("project_id", type=int)
    if pid and _projects_ref:
        from flask import session as flask_session
        uk = flask_session.get("oidc_sub") or flask_session.get("_anon_key", "")
        user_projects = _projects_ref.get(uk, {})
        proj = user_projects.get(pid, {})
        if proj:
            return {
                "graphify": proj.get("graphify_data"),
                "crg_db": proj.get("crg_db_path"),
            }

    from flask import session as flask_session
    token = flask_session.get("mcp_token")
    if token and token in mcp_tokens:
        return mcp_tokens[token]
    return {}


TOOLS = [
    {"name": "graph_search", "description": "Search codebase knowledge graph for functions, classes, files.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "graph_callers", "description": "Find all callers/importers of a symbol.", "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "graph_callees", "description": "Find all callees/imports of a symbol.", "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "graph_impact", "description": "Blast radius: dependents, risk, affected flows.", "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "graph_architecture", "description": "Architecture overview.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "graph_tests", "description": "Find test files/functions.", "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
]


def _crg_query(sql, params=()):
    g = _get_graphs()
    db_path = g.get("crg_db")
    if not db_path or not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


def _search(query, limit=15):
    results = _crg_query("""
        SELECT n.kind, n.name, n.qualified_name, n.file_path, n.line_start, n.signature
        FROM nodes_fts f JOIN nodes n ON n.qualified_name = f.qualified_name
        WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?
    """, (query, limit))
    g = _get_graphs()
    if g.get("graphify"):
        gf = g["graphify"]
        if isinstance(gf, dict):
            q = query.lower()
            for n in gf.get("nodes", []):
                if q in (n.get("label","")+n.get("source_file","")).lower():
                    results.append({"kind": n.get("file_type","?"), "name": n.get("label",""), "file_path": n.get("source_file",""), "source": "graphify"})
                if len(results) >= limit:
                    break
    return results[:limit]


def _callers(symbol, limit=30):
    return _crg_query("SELECT kind, source_qualified, file_path, line FROM edges WHERE target_qualified LIKE ? LIMIT ?", (f"%{symbol}%", limit))


def _callees(symbol, limit=30):
    return _crg_query("SELECT kind, target_qualified, file_path, line FROM edges WHERE source_qualified LIKE ? LIMIT ?", (f"%{symbol}%", limit))


def _impact(symbol):
    matched = _search(symbol, 5)
    qnames = [m["qualified_name"] for m in matched if m.get("qualified_name")]
    deps = []
    if qnames:
        ph = ",".join("?" * len(qnames))
        deps = _crg_query(f"SELECT kind, source_qualified, file_path, line FROM edges WHERE target_qualified IN ({ph}) AND kind IN ('CALLS','IMPORTS') LIMIT 50", qnames)
    flows = _crg_query("SELECT DISTINCT f.name, f.criticality FROM flows f JOIN flow_memberships fm ON fm.flow_id=f.id JOIN nodes n ON n.id=fm.node_id WHERE n.qualified_name LIKE ? ORDER BY f.criticality DESC LIMIT 10", (f"%{symbol}%",))
    return {"matched": matched, "dependents": deps, "flows": flows}


def _architecture():
    return {
        "communities": _crg_query("SELECT community_id, name, purpose, risk, size, dominant_language FROM community_summaries ORDER BY size DESC"),
        "flows": _crg_query("SELECT name, criticality, node_count, depth FROM flows ORDER BY criticality DESC LIMIT 15"),
        "kinds": _crg_query("SELECT kind, COUNT(*) as count FROM nodes GROUP BY kind ORDER BY count DESC"),
    }


def _tests(symbol, limit=20):
    return _crg_query("SELECT name, file_path, line_start FROM nodes WHERE is_test=1 AND (name LIKE ? OR file_path LIKE ?) LIMIT ?", (f"%{symbol}%", f"%{symbol}%", limit))


def _handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "graphify-qa", "version": "1.0.0"}, "capabilities": {"tools": {}}}}
    if method == "notifications/initialized":
        return None
    if method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name", "")
        args = params.get("arguments", {})
        try:
            r = {"graph_search": lambda: {"matches": _search(args.get("query",""))},
                 "graph_callers": lambda: {"callers": _callers(args.get("symbol",""))},
                 "graph_callees": lambda: {"callees": _callees(args.get("symbol",""))},
                 "graph_impact": lambda: _impact(args.get("symbol","")),
                 "graph_architecture": lambda: _architecture(),
                 "graph_tests": lambda: {"tests": _tests(args.get("symbol",""))},
            }.get(name, lambda: None)()
            if r is None:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown: {name}"}}
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(r, default=str)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(e)}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method: {method}"}}


@mcp_bp.route("/message", methods=["POST"])
def message():
    msg = request.get_json(force=True)
    if isinstance(msg, list):
        return jsonify([r for r in (_handle(m) for m in msg) if r is not None])
    result = _handle(msg)
    return ("", 204) if result is None else jsonify(result)


@mcp_bp.route("/sse", methods=["GET"])
def sse():
    token = request.args.get("token", "")
    def stream():
        base = request.url_root.rstrip("/")
        t = f"?token={token}" if token else ""
        yield f"data: {json.dumps({'endpoint': f'{base}/mcp/message{t}'})}\n\n"
    from flask import Response
    return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})