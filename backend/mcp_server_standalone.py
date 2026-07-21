"""
Intelligraph MCP Server — stdio transport via official MCP SDK.

Proxies code-graph retrieval to a running Intelligraph container.
Also reads local repository files directly when --repo-dir is provided.

Works with Claude Code (.mcp.json) and opencode (opencode.json).

Prerequisites:
  pip install mcp requests

Usage:
  python mcp_server_standalone.py --intelligraph-url http://localhost:5050 --project-id 1

With local file access:
  python mcp_server_standalone.py --intelligraph-url http://localhost:5050 --project-id 1 --repo-dir /path/to/repo

Claude Code (.mcp.json, in project root):
  {
    "mcpServers": {
      "intelligraph": {
        "command": "python",
        "args": ["mcp_server_standalone.py", "--intelligraph-url", "http://localhost:5050", "--project-id", "1", "--repo-dir", "."],
        "cwd": "/path/to/your/project"
      }
    }
  }

opencode (opencode.json, in project root or ~/.config/opencode/opencode.json):
  {
    "$schema": "https://opencode.ai/config.json",
    "mcp": {
      "intelligraph": {
        "type": "local",
        "command": ["python", "mcp_server_standalone.py", "--intelligraph-url", "http://localhost:5050", "--project-id", "1", "--repo-dir", "."],
        "cwd": "/path/to/your/project"
      }
    }
  }
"""

import argparse
import json
import os
import sys

import requests
from mcp.server import Server
from mcp import types
from mcp.server.stdio import stdio_server

INTELLIGRAPH_URL = "http://localhost:5050"
PROJECT_ID = None
REPO_DIR = None
MCP_TOKEN = os.environ.get("INTELLIGRAPH_MCP_TOKEN", "")
SSL_VERIFY = os.environ.get("LLM_SSL_VERIFY", "false").lower() == "true"

# Module-level requests Session with trust_env disabled.
# Corporate transparent TLS interceptors (Zscaler, Cisco AnyConnect, etc.)
# mangle even localhost TCP. trust_env=False stops requests from honoring
# HTTP_PROXY/HTTPS_PROXY env vars so plain-HTTP localhost calls are not routed
# through a TLS-bumping proxy. (Does not help against WFP/TDI network-layer
# interceptors — for those use the `docker exec -i` launch form, which uses the
# Docker daemon named pipe and never touches host TCP.)
_session = requests.Session()
_session.trust_env = False


def _build_tools() -> list[types.Tool]:
    """Build tool list, including local_files only if repo_dir is set."""
    tools = [
        types.Tool(
            name="search",
            description=(
                "Search the codebase for symbols, files, or concepts using RRF hybrid search "
                "(keyword FTS5 + semantic embeddings). "
                "Returns relevant symbols WITH signatures, source snippets, and confidence levels "
                "(HIGH/MEDIUM/LOW). Use this FIRST. Usually sufficient for 'what/where is X' "
                "questions — no file read needed when confidence is HIGH. "
                "Example: 'add entity' finds 'upsertEntity'. Build artifacts are filtered out."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Symbol name or concept to search for"},
                    "semantic": {"type": "boolean", "description": "Use semantic-only search (default: false, uses hybrid)", "default": False},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="node",
            description=(
                "Get a symbol's details, multi-hop subgraph (2-hop), 500-char source snippets for "
                "top 5 neighbors, and rationale notes. Use AFTER search. Snippets are usually "
                "sufficient — only read full files if you need implementation details beyond the "
                "snippet. Each result includes role annotations (hub/leaf) to gauge importance."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Symbol name (exact or partial match)"},
                    "depth": {"type": "integer", "description": "Traversal depth (1-3, default 2 for multi-hop context)", "default": 2},
                    "include_snippets": {"type": "boolean", "description": "Include source code snippets (default true)", "default": True},
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="path",
            description=(
                "Trace the shortest path between two symbols in the codebase graph. "
                "Returns the chain of symbols and edge types connecting them. "
                "Use this to answer 'how does X connect to Y?' without reading source files."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "from": {"type": "string", "description": "Starting symbol name"},
                    "to": {"type": "string", "description": "Target symbol name"},
                },
                "required": ["from", "to"],
            },
        ),
        types.Tool(
            name="impact",
            description=(
                "Analyze blast-radius of changing a symbol. "
                "Traverses CALLS and IMPORTS_FROM edges to find all callers, dependents, "
                "and downstream code that would break if the target is modified. "
                "Returns a list of affected files with scores and reasons. Use to plan refactors or assess risk."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Symbol name to analyze impact for"},
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="retrieve",
            description=(
                "Full retrieval pipeline for complex multi-part questions. "
                "Decomposes the question into tasks, runs graph traversal + CRG intelligence + "
                "sparse code fetch, and returns assembled context with source code. "
                "Use this only when search/node/path are not enough."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language question about the codebase"},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="local_files",
            description=(
                "Read raw source files from disk. EXPENSIVE (~1000-4000 tokens per file). "
                "Use ONLY when search/node snippets are insufficient or search confidence is LOW. "
                "If a file was already covered by a prior search/node result, this tool will note "
                "what you already have before returning the content. Prefer 'node' for focused context."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Repo-relative file paths to read",
                    },
                    "max_bytes": {
                        "type": "integer",
                        "description": "Max bytes per file (default 15000)",
                        "default": 15000,
                    },
                },
                "required": ["paths"],
            },
        ),
    ]

    if REPO_DIR:
        tools.append(types.Tool(
            name="nx",
            description=(
                "Run Nx commands locally on the user's workstation. "
                "Available when --repo-dir is set and Nx is installed (node_modules/.bin/nx or package.json). "
                "Capabilities: 'affected' (what projects affected by changes), "
                "'generators' (list available generators), 'targets' (list targets for a project), "
                "'status' (Nx version + workspace status). "
                "This runs Nx directly on the host — not through the container."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "capability": {
                        "type": "string",
                        "enum": ["affected", "generators", "targets", "status"],
                        "description": "What Nx capability to run",
                    },
                    "target": {
                        "type": "string",
                        "description": "Project name (for targets capability)",
                    },
                },
                "required": ["capability"],
            },
        ))

    return tools


# ── Session tracking: which files search/node already described ──
# Maps file_path -> {tool, call_id, snippet_chars, had_signature, had_relationships}
_SESSION_SEEN = {}
_SESSION_STATS = {"search": 0, "node": 0, "path": 0, "impact": 0, "retrieve": 0, "local_files": 0, "est_tokens": 0}
_SESSION_CALL_COUNTER = [0]


def _track_seen(file_path, tool, call_id, snippet_chars=0, had_signature=False, had_relationships=False):
    if not file_path:
        return
    _SESSION_SEEN[file_path] = {
        "tool": tool, "call_id": call_id, "snippet_chars": snippet_chars,
        "had_signature": had_signature, "had_relationships": had_relationships,
    }


def _log_call(tool, result_count, est_tokens):
    _SESSION_STATS[tool] = _SESSION_STATS.get(tool, 0) + 1
    _SESSION_STATS["est_tokens"] += est_tokens
    _SESSION_CALL_COUNTER[0] += 1
    cid = _SESSION_CALL_COUNTER[0]
    stats_summary = ", ".join(f"{k}={v}" for k, v in _SESSION_STATS.items() if k != "est_tokens")
    print(f"[intelligraph-mcp] {tool}#{cid} -> {result_count} results, ~{est_tokens} tokens | session: {stats_summary}, total_tokens~{_SESSION_STATS['est_tokens']}", file=sys.stderr)


def _retrieve(query: str) -> dict:
    """Call the Intelligraph container's retrieval endpoint (full pipeline)."""
    url = f"{INTELLIGRAPH_URL}/graph/retrieve-context"
    headers = {"Content-Type": "application/json"}
    if MCP_TOKEN:
        headers["X-MCP-Token"] = MCP_TOKEN
    r = _session.post(
        url,
        json={"prompt": query, "project_id": PROJECT_ID},
        headers=headers,
        timeout=30,
        verify=SSL_VERIFY,
    )
    r.raise_for_status()
    return r.json()


def _crg(mode: str, query: str) -> dict:
    """Call the Intelligraph CRG endpoint for direct mode access."""
    url = f"{INTELLIGRAPH_URL}/graph/crg"
    headers = {"Content-Type": "application/json"}
    if MCP_TOKEN:
        headers["X-MCP-Token"] = MCP_TOKEN
    r = _session.post(
        url,
        json={"project_id": PROJECT_ID, "mode": mode, "query": query},
        headers=headers,
        timeout=30,
        verify=SSL_VERIFY,
    )
    r.raise_for_status()
    return r.json()


def _read_local_file(repo_relative_path: str, max_bytes: int = 15000) -> str:
    """Read a file from the local repository."""
    # Normalize path separators
    clean_path = repo_relative_path.replace("\\", "/").lstrip("/")

    # Resolve against repo_dir
    full_path = os.path.normpath(os.path.join(REPO_DIR, clean_path))

    # Security: ensure path is within repo_dir
    if not full_path.startswith(os.path.normpath(REPO_DIR)):
        return f"ERROR: path '{repo_relative_path}' is outside the repo directory"

    if not os.path.isfile(full_path):
        return f"ERROR: file not found: {repo_relative_path}"

    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_bytes + 1)
        if len(content) > max_bytes:
            content = content[:max_bytes] + f"\n... (truncated at {max_bytes} bytes)"
        return content
    except Exception as e:
        return f"ERROR reading {repo_relative_path}: {e}"


def _detect_language(path: str) -> str:
    ext_map = {
        ".py": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript",
        ".tsx": "tsx", ".java": "java", ".go": "go", ".rs": "rust",
        ".rb": "ruby", ".php": "php", ".c": "c", ".cpp": "cpp", ".h": "c",
        ".cs": "csharp", ".scala": "scala", ".kt": "kotlin", ".swift": "swift",
    }
    ext = os.path.splitext(path)[1].lower()
    return ext_map.get(ext, "text")


def _format_crg_search(results: list, query: str) -> str:
    if not results:
        _log_call("search", 0, 0)
        return f"No symbols found matching '{query}'."
    call_id = _SESSION_CALL_COUNTER[0] + 1
    lines = [f"## Symbol Search: '{query}' ({len(results)} results)", ""]
    for i, r in enumerate(results[:10], 1):
        name = r.get("name", "?")
        kind = r.get("kind", "?")
        fp = r.get("file_path", "?")
        sig = r.get("signature", "")
        score = r.get("score", 0)
        terms = r.get("matched_terms", [])
        conf = r.get("confidence", "MEDIUM")
        reason = r.get("confidence_reason", "")
        snippet = (r.get("snippet", "") or "").strip()[:200]
        lines.append(f"{i}. {name} ({kind}) — `{fp}`")
        lines.append(f"   confidence: {conf} ({reason})")
        if sig:
            lines.append(f"   signature: `{sig[:150]}`")
        if snippet:
            lines.append(f"   snippet: {snippet}")
        _track_seen(fp, "search", call_id,
                    snippet_chars=len(snippet),
                    had_signature=bool(sig),
                    had_relationships=False)
    est_tokens = len(lines) * 25
    _log_call("search", len(results), est_tokens)
    return "\n".join(lines)


def _format_crg_architecture(results: list) -> str:
    if not results:
        return "No architecture data available."
    lines = ["## Architecture: Community Structure", ""]
    for c in results:
        name = c.get("name", f"Community {c.get('id', '?')}")
        size = c.get("size", 0)
        lang = c.get("dominant_language", "?")
        purpose = c.get("purpose", "")
        risk = c.get("risk", "")
        symbols = c.get("key_symbols", [])
        files = c.get("files", [])
        lines.append(f"### {name} (size={size}, lang={lang})")
        if purpose:
            lines.append(f"  Purpose: {purpose}")
        if risk:
            lines.append(f"  Risk: {risk}")
        if symbols:
            lines.append(f"  Key symbols: {', '.join(symbols[:8])}")
        if files:
            lines.append(f"  Files: {', '.join(files[:5])}")
        lines.append("")
    return "\n".join(lines)


def _format_crg_impact(results: list, target: str) -> str:
    if not results:
        return f"No impact data found for '{target}'."
    lines = [f"## Impact Analysis: '{target}'", ""]
    for r in results:
        fp = r.get("file_path", "?")
        score = r.get("score", 0)
        reason = r.get("reason", [])
        names = r.get("names", [])
        depth = r.get("depth", 0)
        lines.append(f"- `{fp}` (score={score}, depth={depth})")
        if names:
            lines.append(f"  Symbols: {', '.join(names[:5])}")
        if reason:
            lines.append(f"  Reason: {', '.join(reason[:3])}")
    return "\n".join(lines)


def _format_crg_flows(results: list, target: str) -> str:
    if not results:
        return f"No execution flows found for '{target}'."
    lines = [f"## Execution Flows: '{target}'", ""]
    for f in results:
        name = f.get("name", "?")
        criticality = f.get("criticality", 0)
        symbols = f.get("symbols", [])
        files = f.get("files", [])
        lines.append(f"### Flow: {name} (criticality={criticality})")
        if symbols:
            lines.append(f"  Path: {' → '.join(symbols[:10])}")
        if files:
            lines.append(f"  Files: {', '.join(files[:5])}")
        lines.append("")
    return "\n".join(lines)


def _format_retrieve_result(result: dict) -> str:
    context = result.get("context", "")
    files = result.get("files", [])
    strategy = result.get("strategy", "")
    stats = result.get("context_stats", {})

    lines = []
    if context:
        lines.append(context)
    if files:
        lines.append("\n## Relevant Files")
        for f in files[:20]:
            if isinstance(f, dict):
                lines.append(f"- {f.get('path', f.get('name', 'unknown'))}")
            else:
                lines.append(f"- {f}")
    if strategy and strategy != "default":
        lines.append(f"\n*Strategy: {strategy}*")
    if stats:
        lines.append(f"*Stats: {json.dumps(stats)}*")
    if not lines:
        lines.append("No results found. The project may still be indexing or the query didn't match any symbols.")
    return "\n".join(lines)


def _format_local_files(paths: list[str], max_bytes: int) -> str:
    lines = []
    total_bytes = 0
    for path in paths:
        # Source-aware: inform LLM what it already has from prior search/node calls
        seen = _SESSION_SEEN.get(path)
        info_prefix = ""
        if seen:
            already = []
            if seen.get("had_signature"):
                already.append("function signature")
            if seen.get("snippet_chars", 0) > 0:
                already.append(f"{seen['snippet_chars']}-char snippet")
            if seen.get("had_relationships"):
                already.append("caller/callee relationships")
            if already:
                info_prefix = (
                    f"[INFO] `{path}` was previously returned by {seen['tool']} result #{seen['call_id']}.\n"
                    f"Already provided: {', '.join(already)}.\n"
                    f"Reading the raw file will retrieve the complete implementation.\n\n"
                )
        content = _read_local_file(path, max_bytes)
        total_bytes += len(content)
        if content.startswith("ERROR"):
            lines.append(f"{info_prefix}## {path}\n{content}")
        else:
            lang = _detect_language(path)
            lines.append(f"{info_prefix}## {path}")
            lines.append(f"```{lang}")
            lines.append(content)
            lines.append("```")
            lines.append("")
    _log_call("local_files", len(paths), total_bytes // 4)
    return "\n".join(lines)


def _run_nx_local(capability: str, target: str = "") -> str:
    """Run Nx commands locally on the user's workstation via nx_mcp_bridge."""
    if not REPO_DIR:
        return "ERROR: --repo-dir not set. The nx tool requires the MCP server to run with --repo-dir pointing to your project."
    try:
        from nx_mcp_bridge import detect_offline_nx_mcp, query_offline_nx_mcp
        detection = detect_offline_nx_mcp(REPO_DIR)
        if not detection.get("available"):
            return f"Nx not available: {detection.get('error', 'unknown')}. Install Nx in your project (npm install nx)."
        result = query_offline_nx_mcp(REPO_DIR, capability, {"target": target})
        if result.get("error"):
            return f"Nx error: {result['error']}"
        r = result.get("result")
        if isinstance(r, (dict, list)):
            import json
            return f"## Nx: {capability}\n```json\n{json.dumps(r, indent=2)[:5000]}\n```"
        return f"## Nx: {capability}\n```\n{str(r)[:5000]}\n```"
    except ImportError:
        return "ERROR: nx_mcp_bridge not available. Ensure the MCP server script is up to date."
    except Exception as e:
        return f"ERROR running Nx: {e}"


def _graph_get(endpoint: str, params: dict) -> dict:
    """GET request to a graph traversal endpoint."""
    url = f"{INTELLIGRAPH_URL}/graph/{endpoint}"
    headers = {}
    if MCP_TOKEN:
        headers["X-MCP-Token"] = MCP_TOKEN
    r = _session.get(url, params=params, headers=headers, timeout=15, verify=SSL_VERIFY)
    r.raise_for_status()
    return r.json()


def _format_node_result(data: dict, name: str) -> str:
    node = data.get("node")
    if not node:
        _log_call("node", 0, 0)
        return f"No node found matching '{name}'."
    call_id = _SESSION_CALL_COUNTER[0] + 1
    lines = [f"## {node.get('name', name)} ({node.get('kind', 'unknown')})", ""]
    lines.append(f"File: `{node.get('file', 'unknown')}`")
    if node.get("signature"):
        lines.append(f"Signature: `{node['signature']}`")
    degree = node.get("degree", 0)
    role = "hub" if degree >= 8 else ("connector" if degree >= 3 else "leaf")
    lines.append(f"Role: {role} (degree {degree})")
    lines.append(f"Community: {node.get('community', 'unknown')}")
    if node.get("is_test"):
        lines.append("Type: Test")
    neighbors = data.get("neighbors", [])
    if neighbors:
        lines.append("")
        lines.append(f"### Connections ({len(neighbors)})")
        incoming = [n for n in neighbors if n.get("direction") == "incoming"]
        outgoing = [n for n in neighbors if n.get("direction") == "outgoing"]
        if incoming:
            lines.append("**Called by / imported by:**")
            for n in incoming[:10]:
                lines.append(f"  ← `{n['name']}` ({n.get('edge', 'link')}) — `{n.get('file', '')}`")
        if outgoing:
            lines.append("**Calls / imports:**")
            for n in outgoing[:10]:
                lines.append(f"  → `{n['name']}` ({n.get('edge', 'link')}) — `{n.get('file', '')}`")

    # Multi-hop subgraph
    subgraph = data.get("subgraph")
    if subgraph and subgraph.get("nodes"):
        sg_nodes = subgraph["nodes"]
        sg_edges = subgraph.get("edges", [])
        sg_stats = subgraph.get("stats", {})
        lines.append("")
        lines.append(f"### Subgraph ({sg_stats.get('hops', 0)} hops, {len(sg_nodes)} nodes)")
        for sn in sg_nodes[:15]:
            depth = sn.get("depth", 0)
            indent = "  " * depth
            sn_degree = sn.get("degree", 0)
            sn_role = "hub" if sn_degree >= 8 else ("connector" if sn_degree >= 3 else "leaf")
            lines.append(f"{indent}{'→ ' if depth > 0 else ''}{sn.get('name', '?')} ({sn.get('kind', '')}) — `{sn.get('file', '')}` [{sn_role}: degree {sn_degree}]")
            _track_seen(sn.get("file", ""), "node", call_id,
                        snippet_chars=500,
                        had_signature=False,
                        had_relationships=True)

    # Source code snippets — top 5 (was 3)
    snippets = data.get("snippets")
    if snippets:
        lines.append("")
        lines.append("### Source Code")
        for sname, sdata in list(snippets.items())[:5]:
            snip = sdata.get("snippet", "")
            if snip:
                fp = sdata.get("file_path", "")
                ls = sdata.get("line_start", 0)
                lang = _detect_language(fp)
                lines.append(f"```{lang}")
                lines.append(f"// {fp}:{ls}")
                lines.append(snip[:500])
                lines.append("```")
                lines.append("")

    # Rationale notes
    rationale = data.get("rationale")
    if rationale:
        lines.append("### Notes")
        for rn in rationale[:5]:
            text = rn.get("text", "")
            conf = rn.get("confidence", "")
            conf_tag = f" [{conf}]" if conf else ""
            lines.append(f"- {text}{conf_tag}")

    est_tokens = len(lines) * 25
    _log_call("node", len(subgraph.get("nodes", [])) if subgraph else 0, est_tokens)
    return "\n".join(lines)


def _format_path_result(data: dict, src: str, dst: str) -> str:
    path = data.get("path", [])
    if not path:
        return f"No path found between '{src}' and '{dst}'."
    hops = data.get("hops", 0)
    lines = [f"## Path: {src} → {dst} ({hops} hops)", ""]
    for i, step in enumerate(path):
        name = step.get("name", "?")
        fp = step.get("file", "")
        edge = step.get("edge", "")
        prefix = "  " if i > 0 else ""
        edge_str = f" ({edge})" if edge else ""
        lines.append(f"{prefix}{'→ ' if i > 0 else ''}{name}{edge_str} — `{fp}`")
    return "\n".join(lines)


def _dispatch_tool(name: str, arguments: dict) -> str:
    """Map a tool call to the appropriate backend and return formatted text."""
    # Local Nx commands — runs on host, no HTTP needed
    if name == "nx":
        capability = arguments.get("capability", "status")
        target = arguments.get("target", "")
        return _run_nx_local(capability, target)

    # Local file reads — no HTTP needed
    if name == "local_files":
        paths = arguments.get("paths", [])
        max_bytes = arguments.get("max_bytes", 15000)
        return _format_local_files(paths, max_bytes)

    # Full retrieval pipeline
    if name == "retrieve":
        query = arguments["query"]
        result = _retrieve(query)
        return _format_retrieve_result(result)

    # Graph traversal tools
    if name == "node":
        sym = arguments.get("name", "")
        depth = arguments.get("depth", 2)
        include_snippets = arguments.get("include_snippets", True)
        data = _graph_get("node", {
            "project_id": PROJECT_ID, "name": sym,
            "depth": depth,
            "include_rationale": "true",
            "include_snippets": "true" if include_snippets else "false",
        })
        return _format_node_result(data, sym)

    if name == "path":
        src = arguments.get("from", "")
        dst = arguments.get("to", "")
        data = _graph_get("path", {"project_id": PROJECT_ID, "from": src, "to": dst})
        return _format_path_result(data, src, dst)

    # CRG direct mode tools
    if name == "search":
        query = arguments.get("query", "")
        semantic_only = arguments.get("semantic", False)
        mode = "semantic" if semantic_only else "hybrid"
        result = _crg(mode, query)
        results = result.get("results", [])
        return _format_crg_search(results, query)

    if name == "impact":
        query = arguments.get("name", "")
        result = _crg("impact", query)
        results = result.get("results", [])
        return _format_crg_impact(results, query)

    return f"Unknown tool: {name}"


server = Server("intelligraph")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return _build_tools()


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        text = _dispatch_tool(name, arguments)
    except requests.exceptions.ConnectionError:
        text = f"Cannot reach Intelligraph at {INTELLIGRAPH_URL}. Is the container running?"
    except requests.exceptions.Timeout:
        text = "Intelligraph request timed out (30s). The project may be large or still indexing."
    except Exception as e:
        text = f"Error: {str(e)[:500]}"
    return [types.TextContent(type="text", text=text)]


async def main():
    global INTELLIGRAPH_URL, PROJECT_ID, REPO_DIR, MCP_TOKEN
    parser = argparse.ArgumentParser(description="Intelligraph MCP Server (stdio)")
    parser.add_argument("--intelligraph-url", default="http://localhost:5050",
                        help="Intelligraph container URL (default: http://localhost:5050)")
    parser.add_argument("--project-id", type=int, required=True,
                        help="Project ID in the Intelligraph container")
    parser.add_argument("--project-name", default=None,
                        help="Project name to use (alternative to --project-id)")
    parser.add_argument("--repo-dir", default=None,
                        help="Local repository directory for direct file reads (enables local_files tool)")
    parser.add_argument("--mcp-token", default=MCP_TOKEN,
                        help="MCP API token (from Intelligraph UI). Required when SSO is enabled.")
    parser.add_argument("--ssl-verify", action="store_true", default=SSL_VERIFY,
                        help="Verify SSL certificates (default: from LLM_SSL_VERIFY env)")
    parser.add_argument("--self-test", action="store_true",
                        help="Run connectivity self-test (hits /status and /graph/), print verdict, and exit. Does not start the stdio server.")
    args = parser.parse_args()
    INTELLIGRAPH_URL = args.intelligraph_url.rstrip("/")
    PROJECT_ID = args.project_id
    REPO_DIR = os.path.abspath(args.repo_dir) if args.repo_dir else None
    MCP_TOKEN = args.mcp_token.strip()
    if REPO_DIR and not os.path.isdir(REPO_DIR):
        print(f"WARNING: --repo-dir '{REPO_DIR}' does not exist, local_files tool disabled", file=sys.stderr)
        REPO_DIR = None

    if not MCP_TOKEN:
        print("WARNING: no --mcp-token provided. /graph/ endpoints will return 401 if SSO is enabled.", file=sys.stderr)

    print(f"Intelligraph MCP Server starting (url={INTELLIGRAPH_URL}, pid={PROJECT_ID}, repo={REPO_DIR}, token={'yes' if MCP_TOKEN else 'no'})", file=sys.stderr)

    if args.self_test:
        _run_self_test()
        return

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _run_self_test():
    """Connectivity self-test: verify the Intelligraph backend is reachable and
    the MCP token authenticates against /graph/ endpoints. Prints a clear
    verdict for each check and exits. Does not start the stdio server.

    Usage:
        docker exec -i intelligraph python /app/backend/mcp_server_standalone.py \
            --intelligraph-url http://localhost:5050 --project-id 1 \
            --mcp-token <TOKEN> --self-test
    """
    print("=" * 60, file=sys.stderr)
    print("Intelligraph MCP self-test", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    ok = True

    # 1. /status — no auth required, plain HTTP health check
    try:
        r = _session.get(f"{INTELLIGRAPH_URL}/status", timeout=8, verify=SSL_VERIFY)
        if r.status_code == 200:
            print(f"  [PASS] /status reachable (200)", file=sys.stderr)
        else:
            print(f"  [FAIL] /status returned HTTP {r.status_code}", file=sys.stderr)
            ok = False
    except Exception as e:
        print(f"  [FAIL] /status unreachable: {e}", file=sys.stderr)
        ok = False

    # 2. /graph/retrieve-context — requires MCP token when SSO is on
    if not MCP_TOKEN:
        print("  [SKIP] /graph/retrieve-context — no --mcp-token provided", file=sys.stderr)
    else:
        try:
            headers = {"Content-Type": "application/json", "X-MCP-Token": MCP_TOKEN}
            r = _session.post(
                f"{INTELLIGRAPH_URL}/graph/retrieve-context",
                json={"prompt": "self-test", "project_id": PROJECT_ID},
                headers=headers,
                timeout=15,
                verify=SSL_VERIFY,
            )
            if r.status_code == 200:
                body = r.json()
                print(f"  [PASS] /graph/retrieve-context authenticated (200, strategy={body.get('strategy', '?')})", file=sys.stderr)
            elif r.status_code == 401:
                print(f"  [FAIL] /graph/retrieve-context returned 401 — token invalid or SSO blocking at gateway", file=sys.stderr)
                ok = False
            else:
                print(f"  [FAIL] /graph/retrieve-context returned HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
                ok = False
        except Exception as e:
            print(f"  [FAIL] /graph/retrieve-context error: {e}", file=sys.stderr)
            ok = False

    print("=" * 60, file=sys.stderr)
    if ok:
        print("RESULT: PASS — MCP server can reach Intelligraph and authenticate.", file=sys.stderr)
    else:
        print("RESULT: FAIL — see failures above.", file=sys.stderr)
    print("=" * 60, file=sys.stderr)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
