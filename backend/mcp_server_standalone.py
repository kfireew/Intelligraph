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
DOCKER_REPO_PREFIX = ""
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


def _fetch_docker_prefix():
    """Fetch the Docker repo_dir prefix from the backend at startup.

    The CRG DB stores Docker-absolute paths (e.g.
    /app/backend/data/repos/suser-1-xxx/...). The MCP server rewrites
    these to local paths by stripping this prefix and joining with REPO_DIR.
    """
    global DOCKER_REPO_PREFIX
    if not INTELLIGRAPH_URL or not PROJECT_ID:
        return
    try:
        headers = {}
        if MCP_TOKEN:
            headers["X-MCP-Token"] = MCP_TOKEN
        r = _session.get(
            f"{INTELLIGRAPH_URL}/projects/{PROJECT_ID}/docker-prefix",
            headers=headers, timeout=10, verify=SSL_VERIFY,
        )
        if r.status_code == 200:
            DOCKER_REPO_PREFIX = r.json().get("docker_prefix", "")
            if DOCKER_REPO_PREFIX:
                print(f"[intelligraph-mcp] Docker repo prefix: {DOCKER_REPO_PREFIX}", file=sys.stderr)
    except Exception as e:
        print(f"[intelligraph-mcp] WARNING: could not fetch docker-prefix: {e}", file=sys.stderr)


def _rewrite_path(fp: str) -> str:
    """Rewrite a Docker-absolute path to a local path.

    Deterministic — applies to ALL tool outputs (search, node, impact, path).
    1. Strip the Docker repo prefix (e.g. /app/backend/data/repos/suser-1-xxx/)
    2. Join the remaining repo-relative path with the local REPO_DIR
    """
    if not fp:
        return fp
    p = fp.replace("\\", "/")

    # Strip Docker prefix if known
    if DOCKER_REPO_PREFIX:
        prefix = DOCKER_REPO_PREFIX.replace("\\", "/").rstrip("/") + "/"
        if p.lower().startswith(prefix.lower()):
            p = p[len(prefix):]
    elif "/app/backend/data/repos/" in p:
        # Fallback: strip the known Docker repos path pattern
        idx = p.find("/app/backend/data/repos/")
        if idx >= 0:
            rest = p[idx + len("/app/backend/data/repos/"):]
            # rest = suser-1-xxx/libs/shared/...
            slash = rest.find("/")
            if slash >= 0:
                p = rest[slash + 1:]

    # Join with local REPO_DIR if the path is now relative
    if REPO_DIR and not os.path.isabs(p):
        p = os.path.join(REPO_DIR, p)

    return p.replace("\\", "/")


def _build_tools() -> list[types.Tool]:
    """Build tool list, including local_files only if repo_dir is set."""
    tools = [
        types.Tool(
            name="search",
            description=(
                "Search the codebase for symbols, files, or concepts. "
                "Returns name, kind, file path with line ranges (file:start-end), and confidence [H/M/L]. "
                "Use built-in Read with offset=line_start, limit=line_end-line_start to get source. "
                "Use this FIRST — replaces grep and glob."
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
                "Get a symbol's connections (callers, callees, imports) with file:line ranges. "
                "Use AFTER search. Then use built-in Read with those line ranges to get implementation details. "
                "Replaces reading whole files — read only the specific line ranges shown."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Symbol name (exact or partial match)"},
                    "depth": {"type": "integer", "description": "Traversal depth (1-3, default 2)", "default": 2},
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
                "Complete blast radius of changing a symbol. Exhaustive traversal of ALL edge types. "
                "Returns every affected file with symbols to check. Use before refactoring. "
                "Files not listed do not depend on the target."
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
                "Read full source files from disk. EXPENSIVE. "
                "Prefer built-in Read with line ranges from search/node results instead. "
                "Use this only when you need a whole file that search/node didn't cover."
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


def _resolve_path(repo_relative_path: str) -> str:
    """Resolve a path from search/node results against REPO_DIR.

    The backend (Docker) normalizes paths by stripping its guessed repo prefix,
    which may not match the local REPO_DIR. This function tries:
    1. Direct join (REPO_DIR + path) — works when prefix matches
    2. Basename search — find the file by its last 2-3 path components within REPO_DIR
    Returns the resolved full path, or the direct join if search fails.
    """
    clean = repo_relative_path.replace("\\", "/").lstrip("/")
    direct = os.path.normpath(os.path.join(REPO_DIR, clean))
    if os.path.isfile(direct):
        return direct
    # Basename search — find by last N path components
    parts = clean.split("/")
    for depth in range(min(len(parts), 4), 0, -1):
        suffix = "/".join(parts[-depth:])
        import subprocess
        try:
            result = subprocess.run(
                ["cmd", "/c", "dir", "/s", "/b", suffix],
                cwd=REPO_DIR, capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split("\n")
                if len(lines) == 1:
                    return os.path.normpath(lines[0].strip())
                if len(lines) > 1:
                    # Multiple matches — return first (prefer shortest)
                    best = min(lines, key=len).strip()
                    return os.path.normpath(best)
        except Exception:
            pass
    return direct


def _read_local_file(repo_relative_path: str, max_bytes: int = 15000) -> str:
    """Read a file from the local repository."""
    # Normalize path separators
    clean_path = repo_relative_path.replace("\\", "/").lstrip("/")

    # Resolve against repo_dir — try direct first, then basename search
    full_path = os.path.normpath(os.path.join(REPO_DIR, clean_path))
    if not os.path.isfile(full_path):
        full_path = _resolve_path(clean_path)

    # Security: ensure path is within repo_dir
    if not os.path.normpath(full_path).startswith(os.path.normpath(REPO_DIR)):
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


# ── Session search dedup cache ───────────────────────────────────
_SESSION_SEARCHES = {}


def _format_crg_search(results: list, query: str) -> str:
    """Compact search output — one line per result with file:line_start-end.

    The MCP is a NAVIGATOR, not a content provider. It tells the agent WHERE
    to look. The agent uses its built-in Read tool with offset/limit to get
    the actual source code. No DB snippets, no confidence paragraphs, no
    sufficiency text — just name, kind, file:lines, confidence letter.
    """
    if not results:
        _log_call("search", 0, 0)
        return f"No symbols found matching '{query}'."

    # Dedup: same query in same session → cached one-liner
    cache_key = query.lower().strip()
    if cache_key in _SESSION_SEARCHES:
        prev = _SESSION_SEARCHES[cache_key]
        return f"[CACHED] Same as search#{prev['call_id']}. Files: {', '.join(prev['files'])}"

    call_id = _SESSION_CALL_COUNTER[0] + 1
    top_conf = results[0].get("confidence", "MEDIUM")
    conf_tag = {"HIGH": "H", "MEDIUM": "M", "LOW": "L"}.get(top_conf, "M")

    lines = [f'## "{query}" — {len(results)} results [{conf_tag}]']
    files_list = []

    for i, r in enumerate(results[:10], 1):
        name = r.get("name", "?")
        kind = r.get("kind", "?")
        fp = _rewrite_path(r.get("file_path", "?"))
        ls = r.get("line_start", 0)
        le = r.get("line_end", 0)
        r_conf = r.get("confidence", "MEDIUM")
        r_tag = {"HIGH": "H", "MEDIUM": "M", "LOW": "L"}.get(r_conf, "M")

        # Format location: file:start-end (or just file if no line info)
        if ls and le and le > ls:
            loc = f"{fp}:{ls}-{le}"
        elif ls:
            loc = f"{fp}:{ls}"
        else:
            loc = fp
        files_list.append(loc)
        lines.append(f"{i}. {name} ({kind}) {loc} [{r_tag}]")
        _track_seen(fp, "search", call_id, had_signature=bool(r.get("signature")))

    _SESSION_SEARCHES[cache_key] = {"call_id": call_id, "files": files_list}
    est_tokens = sum(len(l) for l in lines) // 4
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
    _log_call("impact", len(results), len(results) * 30)
    lines = [f"## Impact: '{target}' ({len(results)} files — complete blast radius)", ""]
    lines.append("Exhaustive traversal of CRG + graphify links. Files not listed here do not depend on the target in the code graph.")
    lines.append("")
    for r in results:
        fp = _rewrite_path(r.get("file_path", "?"))
        depth = r.get("depth", 0)
        symbols = r.get("symbols", [])
        edge_types = r.get("edge_types", [])
        sources = r.get("sources", [])
        depth_label = "definition" if depth == 0 else f"depth {depth}"
        src_label = "/".join(sources) if sources else "crg"
        lines.append(f"- `{fp}` ({depth_label}, {src_label})")
        if symbols:
            lines.append(f"  symbols: {', '.join(symbols[:5])}")
        if edge_types:
            lines.append(f"  edges: {', '.join(edge_types[:5])}")
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
    """Compact node output — connections with file:line_start-end ranges.

    The MCP is a NAVIGATOR. It shows what connects to what and WHERE each
    symbol lives (file:start-end). The agent uses its built-in Read tool
    with offset/limit to get the actual source code. No DB snippets, no
    subgraph tree, no rationale, no reading plan prose — the file:line ranges
    in the connections list ARE the reading plan.
    """
    node = data.get("node")
    if not node:
        _log_call("node", 0, 0)
        return f"No node found matching '{name}'."

    call_id = _SESSION_CALL_COUNTER[0] + 1
    node_file = _rewrite_path(node.get("file", "unknown"))
    node_kind = node.get("kind", "unknown")
    degree = node.get("degree", 0)
    node_ls = node.get("line_start", 0)
    node_le = node.get("line_end", 0)

    # Header: one line with location
    loc = f"{node_file}:{node_ls}-{node_le}" if node_ls and node_le else node_file
    lines = [f"## {node.get('name', name)} ({node_kind}) {loc}", f"degree={degree}"]

    # Connections — ALL neighbors, one line each with file:line range
    neighbors = data.get("neighbors", [])
    if neighbors:
        incoming = [n for n in neighbors if n.get("direction") == "incoming"]
        outgoing = [n for n in neighbors if n.get("direction") == "outgoing"]
        lines.append("")
        lines.append(f"### Connections ({len(neighbors)})")
        if incoming:
            for n in incoming:
                nname = n.get("name", "?")
                nfile = _rewrite_path(n.get("file", ""))
                nls = n.get("line_start", 0)
                nle = n.get("line_end", 0)
                nloc = f"{nfile}:{nls}-{nle}" if nls and nle else (f"{nfile}:{nls}" if nls else nfile)
                edge = n.get("edge", "link")
                lines.append(f"  <- {nname} ({edge}) {nloc}")
                _track_seen(nfile, "node", call_id, had_relationships=True)
        if outgoing:
            for n in outgoing:
                nname = n.get("name", "?")
                nfile = _rewrite_path(n.get("file", ""))
                nls = n.get("line_start", 0)
                nle = n.get("line_end", 0)
                nloc = f"{nfile}:{nls}-{nle}" if nls and nle else (f"{nfile}:{nls}" if nls else nfile)
                edge = n.get("edge", "link")
                lines.append(f"  -> {nname} ({edge}) {nloc}")
                _track_seen(nfile, "node", call_id, had_relationships=True)

    est_tokens = sum(len(l) for l in lines) // 4
    _log_call("node", len(neighbors), est_tokens)
    return "\n".join(lines)


def _format_path_result(data: dict, src: str, dst: str) -> str:
    path = data.get("path", [])
    if not path:
        return f"No path found between '{src}' and '{dst}'."
    hops = data.get("hops", 0)
    lines = [f"## Path: {src} → {dst} ({hops} hops)", ""]
    for i, step in enumerate(path):
        name = step.get("name", "?")
        fp = _rewrite_path(step.get("file", ""))
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
        data = _graph_get("node", {
            "project_id": PROJECT_ID, "name": sym,
            "depth": depth,
            "include_rationale": "false",
            "include_snippets": "false",
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

    # Fetch Docker repo prefix for path rewriting
    _fetch_docker_prefix()

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
