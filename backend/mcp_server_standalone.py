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
SSL_VERIFY = os.environ.get("LLM_SSL_VERIFY", "false").lower() == "true"


def _build_tools() -> list[types.Tool]:
    """Build tool list, including local_files only if repo_dir is set."""
    tools = [
        types.Tool(
            name="search",
            description=(
                "Search the codebase graph for symbols, files, or concepts. "
                "Returns symbol names, signatures, file paths, and community IDs from the CRG database. "
                "Use this to find where a function, class, or variable is defined."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for (symbol name, concept, file pattern)"},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="architecture",
            description=(
                "Get architecture overview of the codebase. "
                "Returns community structure with purpose summaries, key symbols, risk levels, "
                "and dominant languages for each module/community."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Architecture question or component name (optional)"},
                },
            },
        ),
        types.Tool(
            name="impact",
            description=(
                "Analyze blast-radius of changing a symbol. "
                "Traverses CALLS and IMPORTS_FROM edges to find all callers, dependents, "
                "and downstream code that would break if the target is modified. "
                "Returns a call chain tree showing the full impact path."
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
            name="flows",
            description=(
                "Find execution flow paths containing a symbol. "
                "Returns ordered execution paths from entry points through the target symbol, "
                "with criticality scores. Use this to understand how data/control flows "
                "through the system and trace how a function fits into the larger flow."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Symbol name to find flows for"},
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="callers",
            description="Find callers of a symbol — who calls this function/method/class.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Symbol name to find callers for"},
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="callees",
            description="Find callees of a symbol — what does this function/method call.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Symbol name to find callees for"},
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="retrieve",
            description=(
                "Full retrieval pipeline — decomposes a natural language question into tasks, "
                "runs graph traversal + CRG intelligence + sparse code fetch, and returns "
                "assembled context with source code. Use this for complex questions that "
                "need both graph structure and actual code contents."
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
            name="tests",
            description="Find test files related to a symbol or component.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Symbol name to find tests for"},
                },
                "required": ["name"],
            },
        ),
    ]

    if REPO_DIR:
        tools.append(types.Tool(
            name="local_files",
            description=(
                "Read full source code from the local repository on disk. "
                "This is more reliable than the retrieve tool for getting exact code — "
                "no sparse fetch or clone needed. Use this after search/impact/flows "
                "to get the actual file contents for the files you found. "
                "Pass repo-relative paths (e.g. 'src/parser.py', 'backend/app.py')."
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
        ))

    return tools


def _retrieve(query: str) -> dict:
    """Call the Intelligraph container's retrieval endpoint (full pipeline)."""
    url = f"{INTELLIGRAPH_URL}/graph/retrieve-context"
    r = requests.post(
        url,
        json={"prompt": query, "project_id": PROJECT_ID},
        timeout=30,
        verify=SSL_VERIFY,
    )
    r.raise_for_status()
    return r.json()


def _crg(mode: str, query: str) -> dict:
    """Call the Intelligraph CRG endpoint for direct mode access."""
    url = f"{INTELLIGRAPH_URL}/graph/crg"
    r = requests.post(
        url,
        json={"project_id": PROJECT_ID, "mode": mode, "query": query},
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
        return f"No symbols found matching '{query}'."
    lines = [f"## Symbol Search: '{query}'", ""]
    for r in results:
        name = r.get("name", "?")
        kind = r.get("kind", "?")
        fp = r.get("file_path", "?")
        sig = r.get("signature", "")
        score = r.get("score", 0)
        terms = r.get("matched_terms", [])
        lines.append(f"### {name} ({kind}) — `{fp}`")
        if sig:
            lines.append(f"  Signature: `{sig}`")
        lines.append(f"  Score: {score}")
        if terms:
            lines.append(f"  Matched: {', '.join(terms)}")
        lines.append("")
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
    for path in paths:
        content = _read_local_file(path, max_bytes)
        if content.startswith("ERROR"):
            lines.append(f"## {path}\n{content}")
        else:
            lang = _detect_language(path)
            lines.append(f"## {path}")
            lines.append(f"```{lang}")
            lines.append(content)
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def _dispatch_tool(name: str, arguments: dict) -> str:
    """Map a tool call to the appropriate backend and return formatted text."""
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

    # CRG direct mode tools
    crg_modes = {
        "search": ("search", lambda a: a.get("query", "")),
        "architecture": ("architecture", lambda a: a.get("prompt", "")),
        "impact": ("impact", lambda a: a["name"]),
        "flows": ("flows", lambda a: a["name"]),
        "callers": ("impact", lambda a: a["name"]),
        "callees": ("impact", lambda a: a["name"]),
        "tests": ("search", lambda a: f"test {a['name']}"),
    }

    if name not in crg_modes:
        return f"Unknown tool: {name}"

    mode, get_query = crg_modes[name]
    query = get_query(arguments)

    # For architecture, use full retrieve (gets graph + CRG + communities)
    if name == "architecture":
        result = _retrieve(arguments.get("prompt", "architecture overview"))
        return _format_retrieve_result(result)

    # For callers/callees, use impact mode and filter
    if name == "callers":
        result = _crg("impact", query)
        results = result.get("results", [])
        # Filter to incoming (callers)
        filtered = [r for r in results if "incoming" in r.get("reason", []) or "caller" in str(r.get("reason", [])).lower()]
        return _format_crg_impact(filtered or results, f"callers of {query}")
    if name == "callees":
        result = _crg("impact", query)
        results = result.get("results", [])
        filtered = [r for r in results if "outgoing" in r.get("reason", []) or "callee" in str(r.get("reason", [])).lower()]
        return _format_crg_impact(filtered or results, f"callees of {query}")

    # Direct CRG mode
    result = _crg(mode, query)
    results = result.get("results", [])

    if mode == "search":
        return _format_crg_search(results, query)
    elif mode == "architecture":
        return _format_crg_architecture(results)
    elif mode == "impact":
        return _format_crg_impact(results, query)
    elif mode == "flows":
        return _format_crg_flows(results, query)

    return json.dumps(result, indent=2)[:5000]


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
    global INTELLIGRAPH_URL, PROJECT_ID, REPO_DIR
    parser = argparse.ArgumentParser(description="Intelligraph MCP Server (stdio)")
    parser.add_argument("--intelligraph-url", default="http://localhost:5050",
                        help="Intelligraph container URL (default: http://localhost:5050)")
    parser.add_argument("--project-id", type=int, required=True,
                        help="Project ID in the Intelligraph container")
    parser.add_argument("--project-name", default=None,
                        help="Project name to use (alternative to --project-id)")
    parser.add_argument("--repo-dir", default=None,
                        help="Local repository directory for direct file reads (enables local_files tool)")
    parser.add_argument("--ssl-verify", action="store_true", default=SSL_VERIFY,
                        help="Verify SSL certificates (default: from LLM_SSL_VERIFY env)")
    args = parser.parse_args()
    INTELLIGRAPH_URL = args.intelligraph_url.rstrip("/")
    PROJECT_ID = args.project_id
    REPO_DIR = os.path.abspath(args.repo_dir) if args.repo_dir else None
    if REPO_DIR and not os.path.isdir(REPO_DIR):
        print(f"WARNING: --repo-dir '{REPO_DIR}' does not exist, local_files tool disabled", file=sys.stderr)
        REPO_DIR = None

    print(f"Intelligraph MCP Server starting (url={INTELLIGRAPH_URL}, pid={PROJECT_ID}, repo={REPO_DIR})", file=sys.stderr)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
