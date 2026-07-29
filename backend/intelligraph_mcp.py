#!/usr/bin/env python3
"""
intelligraph_mcp.py — Local MCP server with pod sync.

Downloads graph.db + graph.json from the Intelligraph pod on startup,
then serves all MCP tools locally (no HTTP to pod during normal operation).

Usage:
  python intelligraph_mcp.py --pod-url http://pod:5050 --project-id 1 --repo-dir . --mcp-token TOKEN
  python intelligraph_mcp.py --sync --pod-url http://pod:5050 --project-id 1 --mcp-token TOKEN  # re-sync only

On startup: prints "Updating MCP..." to stderr while syncing, then "MCP ready".
The harness (opencode/Claude Code) displays this to the user.

Re-syncs automatically when graph.db changes on disk (e.g. after /mcp-update).
"""

import argparse
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

# Add this script's directory to sys.path so crg_intelligence + semantic_planner
# (downloaded alongside this file) are importable.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import requests

# ── Cache location ───────────────────────────────────────────────
CACHE_DIR = Path.home() / ".intelligraph" / "cache"


def _sync_from_pod(pod_url: str, project_id: int, mcp_token: str, ssl_verify: bool = True) -> Path:
    """Download graph.db + graph.json from pod as zip, extract to cache dir.

    Returns the path to the cached graph.db.
    """
    global _ORIGINAL_REPO_DIR
    cache_dir = CACHE_DIR / str(project_id)
    cache_dir.mkdir(parents=True, exist_ok=True)

    url = f"{pod_url.rstrip('/')}/projects/{project_id}/sync"
    headers = {"X-MCP-Token": mcp_token}

    print(f"Updating MCP... (syncing from {pod_url})", file=sys.stderr, flush=True)

    r = requests.get(url, headers=headers, timeout=120, verify=ssl_verify)
    if r.status_code == 401:
        print(f"ERROR: MCP token rejected (401). Check your token.", file=sys.stderr)
        sys.exit(1)
    if r.status_code != 200:
        print(f"ERROR: sync failed (HTTP {r.status_code}): {r.text[:200]}", file=sys.stderr)
        sys.exit(1)

    # Extract zip to cache dir
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        zf.extractall(cache_dir)

    graph_db = cache_dir / "graph.db"
    if not graph_db.exists():
        print("ERROR: graph.db not found in sync data", file=sys.stderr)
        sys.exit(1)

    # Read metadata (includes original_repo_dir from Docker — used for path normalization)
    meta_path = cache_dir / "metadata.json"
    meta = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())

    _ORIGINAL_REPO_DIR = meta.get("original_repo_dir", "")

    nodes = meta.get("nodes", "?")
    edges = meta.get("edges", "?")
    print(f"MCP ready ({nodes} nodes, {edges} edges)", file=sys.stderr, flush=True)

    return graph_db


def _check_for_update(graph_db: Path, last_mtime: float) -> bool:
    """Check if graph.db has been modified since last check."""
    try:
        mtime = graph_db.stat().st_mtime
        return mtime > last_mtime
    except Exception:
        return False


def _get_provider(graph_db_path: str, graphify_data: dict = None):
    """Get a CRGProvider instance. Uses crg_intelligence (same code as the pod)."""
    proj = {
        "id": 0,
        "crg_db_path": graph_db_path,
        "graphify_data": graphify_data or {},
        "repo_dir": REPO_DIR,
        "original_repo_dir": _ORIGINAL_REPO_DIR,
    }
    from crg_intelligence import CRGProvider
    return CRGProvider(proj)


def _rewrite_path(fp: str) -> str:
    """Rewrite a Docker-absolute or repo-relative path to a full local path.

    1. Strip the Docker repo prefix (from original_repo_dir in metadata)
    2. Join the remaining repo-relative path with the local REPO_DIR
    Returns forward-slashed full local path.
    """
    if not fp:
        return fp
    p = fp.replace("\\", "/")

    # Strip Docker prefix if known (from metadata.original_repo_dir)
    if _ORIGINAL_REPO_DIR:
        prefix = _ORIGINAL_REPO_DIR.replace("\\", "/").rstrip("/") + "/"
        if p.lower().startswith(prefix.lower()):
            p = p[len(prefix):]
    elif "/app/backend/data/repos/" in p:
        idx = p.find("/app/backend/data/repos/")
        if idx >= 0:
            rest = p[idx + len("/app/backend/data/repos/"):]
            slash = rest.find("/")
            if slash >= 0:
                p = rest[slash + 1:]

    # Join with local REPO_DIR if the path is now relative
    if REPO_DIR and not os.path.isabs(p):
        p = os.path.join(REPO_DIR, p)

    return p.replace("\\", "/")


# ── Session tracking (same as mini server) ───────────────────────
_SESSION_SEEN = {}
_SESSION_STATS = {"search": 0, "node": 0, "path": 0, "impact": 0, "local_files": 0, "est_tokens": 0}
_SESSION_CALL_COUNTER = [0]
_SESSION_SEARCHES = {}


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


# ── Tool dispatch (same logic as mini server) ────────────────────

def _detect_language(path: str) -> str:
    ext_map = {
        ".py": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript",
        ".tsx": "tsx", ".java": "java", ".go": "go", ".rs": "rust",
        ".rb": "ruby", ".php": "php", ".c": "c", ".cpp": "cpp", ".h": "c",
        ".cs": "csharp", ".scala": "scala", ".kt": "kotlin", ".swift": "swift",
    }
    ext = os.path.splitext(path)[1].lower()
    return ext_map.get(ext, "text")


def _read_local_file(repo_relative_path: str, max_bytes: int = 15000) -> str:
    clean_path = repo_relative_path.replace("\\", "/").lstrip("/")
    full_path = os.path.normpath(os.path.join(REPO_DIR, clean_path))
    if not os.path.isfile(full_path):
        # Try basename search
        parts = clean_path.split("/")
        for depth in range(min(len(parts), 4), 0, -1):
            suffix = "/".join(parts[-depth:])
            try:
                import subprocess
                result = subprocess.run(
                    ["cmd", "/c", "dir", "/s", "/b", suffix],
                    cwd=REPO_DIR, capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    lines = result.stdout.strip().split("\n")
                    best = min(lines, key=len).strip()
                    full_path = os.path.normpath(best)
                    break
            except Exception:
                pass
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


def _format_search(results, query, near=""):
    guidance = [r for r in results if "low_confidence_guidance" in r.get("reason", []) or "no_match" in r.get("reason", [])]
    real_results = [r for r in results if r not in guidance]

    if not real_results:
        _log_call("search", 0, 0)
        for g in guidance:
            return g.get("confidence_reason", f"No symbols found matching '{query}'.")
        return f"No symbols found matching '{query}'."

    cache_key = f"{query.lower().strip()}|{near.lower().strip()}"
    if cache_key in _SESSION_SEARCHES:
        prev = _SESSION_SEARCHES[cache_key]
        return f"[CACHED] Same as search#{prev['call_id']}. Files: {', '.join(prev['files'])}"

    call_id = _SESSION_CALL_COUNTER[0] + 1
    top_conf = real_results[0].get("confidence", "MEDIUM")
    conf_tag = {"HIGH": "H", "MEDIUM": "M", "LOW": "L"}.get(top_conf, "M")

    lines = [f'## "{query}" — {len(real_results)} results [{conf_tag}]']
    files_list = []

    for i, r in enumerate(real_results, 1):
        fp = _rewrite_path(r.get("file_path", "?"))
        r_conf = r.get("confidence", "MEDIUM")
        r_tag = {"HIGH": "H", "MEDIUM": "M", "LOW": "L"}.get(r_conf, "M")
        is_path_match = "path_match" in r.get("reason", [])

        if is_path_match:
            files_list.append(fp)
            connections = r.get("connections", [])
            if connections:
                lines.append(f"{i}. {fp} — connected to: {', '.join(connections)} [{r_tag}]")
            else:
                lines.append(f"{i}. {fp} [{r_tag}]")
            _track_seen(fp, "search", call_id)
        else:
            name = r.get("name", "?")
            kind = r.get("kind", "?")
            ls = r.get("line_start", 0)
            le = r.get("line_end", 0)
            if ls and le and le > ls:
                loc = f"{fp}:{ls}-{le}"
            elif ls:
                loc = f"{fp}:{ls}"
            else:
                loc = fp
            files_list.append(loc)
            lines.append(f"{i}. {name} ({kind}) {loc} [{r_tag}]")
            _track_seen(fp, "search", call_id, had_signature=bool(r.get("signature")))

    for g in guidance:
        lines.append(f"\n{g.get('confidence_reason', '')}")

    # Nudge: if no near= was used and results are broad (>5), suggest near= values
    if not near and len(real_results) > 5:
        suggest_names = []
        for r in real_results[:4]:
            n = r.get("name", "")
            if n and n not in suggest_names:
                suggest_names.append(n)
        if suggest_names:
            suggest_str = " or ".join(f'near="{n}"' for n in suggest_names[:2])
            lines.append(f"\n-> Pass {suggest_str} to filter to your subsystem.")

    _SESSION_SEARCHES[cache_key] = {"call_id": call_id, "files": files_list}
    est_tokens = sum(len(l) for l in lines) // 4
    _log_call("search", len(real_results), est_tokens)
    return "\n".join(lines)


def _dispatch(name, args, provider):
    if name == "search":
        query = args.get("query", "")
        near = args.get("near", "")
        results = provider.hybrid_search(query, max_results=10, embedding_weight=0.4, near=near)
        return _format_search(results, query, near=near)

    if name == "node":
        sym = args.get("name", "")
        depth = min(3, max(1, args.get("depth", 2)))
        results = provider.hybrid_search(sym, max_results=5, embedding_weight=0.4)
        target = results[0]["name"] if results else sym
        trav = provider.traverse(target, max_hops=depth, max_nodes=30, max_tokens=400)

        # Format node result
        nodes = trav.get("nodes", [])
        if not nodes:
            _log_call("node", 0, 0)
            return f"No node found matching '{sym}'."

        call_id = _SESSION_CALL_COUNTER[0] + 1
        node = nodes[0]
        node_file = _rewrite_path(node.get("file", "unknown"))
        node_kind = node.get("kind", "unknown")
        node_ls = node.get("line_start", 0)
        node_le = node.get("line_end", 0)
        loc = f"{node_file}:{node_ls}-{node_le}" if node_ls and node_le else node_file
        lines = [f"## {node.get('name', sym)} ({node_kind}) {loc}", f"degree={node.get('degree', 0)}"]

        edges = trav.get("edges", [])
        if edges:
            incoming = [e for e in edges if e.get("target") == node.get("name")]
            outgoing = [e for e in edges if e.get("source") == node.get("name")]
            lines.append("")
            lines.append(f"### Connections ({len(edges)})")
            for e in incoming:
                nname = e.get("source", "?")
                lines.append(f"  <- {nname} ({e.get('type', 'link')})")
                _track_seen(nname, "node", call_id, had_relationships=True)
            for e in outgoing:
                nname = e.get("target", "?")
                lines.append(f"  -> {nname} ({e.get('type', 'link')})")
                _track_seen(nname, "node", call_id, had_relationships=True)

        est_tokens = sum(len(l) for l in lines) // 4
        _log_call("node", len(edges), est_tokens)
        return "\n".join(lines)

    if name == "impact":
        target = args.get("name", "")
        results = provider.impact(target)
        if not results:
            _log_call("impact", 0, 0)
            return f"No impact data found for '{target}'."
        _log_call("impact", len(results), len(results) * 30)
        lines = [f"## Impact: '{target}' ({len(results)} files — complete blast radius)", ""]
        for r in results:
            fp = _rewrite_path(r.get("file_path", "?"))
            depth = r.get("depth", 0)
            symbols = r.get("symbols", [])
            edge_types = r.get("edge_types", [])
            depth_label = "definition" if depth == 0 else f"depth {depth}"
            lines.append(f"- `{fp}` ({depth_label})")
            if symbols:
                lines.append(f"  symbols: {', '.join(symbols[:5])}")
            if edge_types:
                lines.append(f"  edges: {', '.join(edge_types[:5])}")
        return "\n".join(lines)

    if name == "path":
        src = args.get("from", "")
        dst = args.get("to", "")
        # Use traverse to find path
        src_results = provider.hybrid_search(src, max_results=1, embedding_weight=0.4)
        dst_results = provider.hybrid_search(dst, max_results=1, embedding_weight=0.4)
        if not src_results or not dst_results:
            return f"Could not find both symbols ('{src}' and '{dst}')."
        _log_call("path", 0, 50)
        return f"## Path: {src} → {dst}\n(Use node() on each symbol to trace connections manually.)"

    if name == "local_files":
        paths = args.get("paths", [])
        max_bytes = args.get("max_bytes", 15000)
        lines = []
        total_bytes = 0
        for path in paths:
            content = _read_local_file(path, max_bytes)
            total_bytes += len(content)
            if content.startswith("ERROR"):
                lines.append(f"## {path}\n{content}")
            else:
                lang = _detect_language(path)
                lines.append(f"## {path}\n```{lang}\n{content}\n```")
        _log_call("local_files", len(paths), total_bytes // 4)
        return "\n".join(lines)

    if name == "package":
        return _resolve_package(args.get("name", ""))

    return f"Unknown tool: {name}"


def _resolve_package(pkg_name: str) -> str:
    """Resolve an npm package to its entry point files from node_modules."""
    if not REPO_DIR:
        return "ERROR: --repo-dir not set."
    if not pkg_name:
        return "ERROR: package name required."

    pkg_name = pkg_name.strip().strip('"').strip("'")
    pkg_json_path = os.path.join(REPO_DIR, "node_modules", pkg_name, "package.json")

    if not os.path.isfile(pkg_json_path):
        if pkg_name.startswith("@") and "/" in pkg_name:
            flat = pkg_name.split("/", 1)[1]
            flat_path = os.path.join(REPO_DIR, "node_modules", flat, "package.json")
            if os.path.isfile(flat_path):
                pkg_json_path = flat_path
            else:
                return f"Package '{pkg_name}' not found in node_modules."
        else:
            return f"Package '{pkg_name}' not found in node_modules."

    try:
        with open(pkg_json_path, "r", encoding="utf-8", errors="replace") as f:
            pkg = json.load(f)
    except Exception as e:
        return f"ERROR reading package.json for '{pkg_name}': {e}"

    lines = [f"## {pkg_name} (v{pkg.get('version', '?')})"]
    main = pkg.get("main")
    types = pkg.get("types") or pkg.get("typings")
    module_field = pkg.get("module")
    exports = pkg.get("exports")
    pkg_dir = os.path.dirname(pkg_json_path)
    rel_base = os.path.relpath(pkg_dir, REPO_DIR).replace("\\", "/")

    if main:
        lines.append(f"main: {rel_base}/{main}")
    if module_field:
        lines.append(f"module: {rel_base}/{module_field}")
    if types:
        lines.append(f"types: {rel_base}/{types}")
    if exports:
        if isinstance(exports, dict):
            for key in list(exports.keys())[:5]:
                val = exports[key]
                if isinstance(val, str):
                    lines.append(f"exports['{key}']: {rel_base}/{val}")
                elif isinstance(val, dict) and "." in val:
                    lines.append(f"exports['{key}']: {rel_base}/{val['.']}")
        elif isinstance(exports, str):
            lines.append(f"exports: {rel_base}/{exports}")

    if not any(line.startswith(("main:", "types:", "module:", "exports")) for line in lines[1:]):
        lines.append(f"(No entry point fields. Use Read to list: {rel_base}/)")

    _log_call("package", 1, 50)
    return "\n".join(lines)


# ── MCP server (stdio) ───────────────────────────────────────────

REPO_DIR = None
_GRAPH_DB_PATH = None
_GRAPHIFY_DATA = {}
_ORIGINAL_REPO_DIR = None
_LAST_MTIME = 0.0
_PROVIDER = None


def _get_or_reload_provider():
    """Get provider, re-syncing from pod if graph.db changed on disk."""
    global _PROVIDER, _LAST_MTIME, _GRAPH_DB_PATH, _GRAPHIFY_DATA

    # Only check for updates if we already have a provider (skip on first call
    # when _LAST_MTIME is 0.0 — that's the initial load, not an update)
    if _PROVIDER is not None and _GRAPH_DB_PATH and _check_for_update(Path(_GRAPH_DB_PATH), _LAST_MTIME):
        print("[intelligraph-mcp] Graph updated - re-syncing...", file=sys.stderr, flush=True)
        # Invalidate stale embedding cache (keyed by db_path, content changed)
        try:
            from crg_intelligence import _EMBEDDING_CACHE
            _EMBEDDING_CACHE.pop(_GRAPH_DB_PATH, None)
        except ImportError:
            pass
        _PROVIDER = None
        _LAST_MTIME = 0.0

    if _PROVIDER is None:
        _PROVIDER = _get_provider(str(_GRAPH_DB_PATH), _GRAPHIFY_DATA)
        if _PROVIDER:
            _PROVIDER.is_available()
            _LAST_MTIME = Path(_GRAPH_DB_PATH).stat().st_mtime
            # Pre-warm encoder + embedding index so first search after reload is fast
            try:
                from semantic_planner import _get_encoder
                enc = _get_encoder()
                if enc:
                    _PROVIDER._build_embedding_index()
                    print("[intelligraph-mcp] encoder + embedding index ready", file=sys.stderr, flush=True)
                else:
                    print("[intelligraph-mcp] encoder unavailable (semantic search disabled)", file=sys.stderr, flush=True)
            except Exception as e:
                print(f"[intelligraph-mcp] encoder pre-warm skipped: {e}", file=sys.stderr, flush=True)

    return _PROVIDER


def main():
    global REPO_DIR, _GRAPH_DB_PATH, _GRAPHIFY_DATA, _PROVIDER, _LAST_MTIME, _ORIGINAL_REPO_DIR

    parser = argparse.ArgumentParser(description="Intelligraph Local MCP Server (stdio)")
    parser.add_argument("--pod-url", required=True, help="Intelligraph pod URL")
    parser.add_argument("--project-id", type=int, required=True, help="Project ID")
    parser.add_argument("--repo-dir", default=None, help="Local repository directory")
    parser.add_argument("--mcp-token", required=True, help="MCP API token")
    parser.add_argument("--ssl-verify", action="store_true", default=False, help="Verify SSL certs")
    parser.add_argument("--sync", action="store_true", help="Re-sync from pod and exit (no stdio server)")
    args = parser.parse_args()

    REPO_DIR = os.path.abspath(args.repo_dir) if args.repo_dir else None
    if REPO_DIR and not os.path.isdir(REPO_DIR):
        print(f"WARNING: --repo-dir '{REPO_DIR}' does not exist", file=sys.stderr)

    # Sync from pod
    _GRAPH_DB_PATH = _sync_from_pod(args.pod_url, args.project_id, args.mcp_token, args.ssl_verify)

    # Load graphify data if available
    gf_path = CACHE_DIR / str(args.project_id) / "graph.json"
    if gf_path.exists():
        try:
            _GRAPHIFY_DATA = json.loads(gf_path.read_text())
        except Exception:
            _GRAPHIFY_DATA = {}

    if args.sync:
        # Sync-only mode: just download and exit
        return

    # Pre-load provider NOW (not lazily in async handler).
    # Importing crg_intelligence inside the anyio event loop can
    # deadlock - loading it here during startup avoids that.
    # _get_or_reload_provider() also pre-warms the encoder + embedding index.
    print("[intelligraph-mcp] loading provider...", file=sys.stderr, flush=True)
    _PROVIDER = _get_or_reload_provider()
    if not _PROVIDER:
        print("[intelligraph-mcp] ERROR: provider not available", file=sys.stderr, flush=True)
        sys.exit(1)
    print(f"[intelligraph-mcp] provider ready", file=sys.stderr, flush=True)

    # Start MCP stdio server
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        import mcp.types as types
    except ImportError:
        print("ERROR: mcp package not installed. Run: pip install mcp", file=sys.stderr)
        sys.exit(1)

    server = Server("intelligraph")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name="search",
                description=(
                    "Search the codebase graph. Pass a specific symbol name ('UserStatus', 'zik'), "
                    "a file path ('src/types/enums'), or ONE concept word ('authentication'). "
                    "Do NOT pass multi-word descriptions - "
                    "search('zik') not search('plane type enum plane types'). "
                    "Returns name, kind, file path with line ranges (file:start-end), and confidence [H/M/L]. "
                    "Use built-in Read with offset=line_start, limit=line_end-line_start to get source. "
                    "Use this FIRST - replaces grep and glob. "
                    "ALWAYS pass near= - without it you get 16+ broad results wasting ~2000 tokens. "
                    "With near= you get 2-3 targeted results (~300 tokens)."
                ),
                inputSchema={"type": "object", "properties": {
                    "query": {"type": "string"},
                    "near": {"type": "string", "description": "REQUIRED on every search after the first. Symbol or file path to filter results to your subsystem (3 graph hops). Without near=, search returns up to 16 broad results (~2000 tokens wasted). With near=, you get 2-3 targeted results (~300 tokens)."},
                }, "required": ["query"]},
            ),
            types.Tool(
                name="node",
                description=(
                    "Get a symbol's connections (callers, callees) with file:line ranges. "
                    "Use AFTER search. Then use built-in Read with those line ranges to get implementation details."
                ),
                inputSchema={"type": "object", "properties": {
                    "name": {"type": "string"}, "depth": {"type": "integer", "default": 2}
                }, "required": ["name"]},
            ),
            types.Tool(
                name="impact",
                description=(
                    "Complete blast radius of changing a symbol. Exhaustive traversal of ALL edge types. "
                    "Returns every affected file with symbols to check. Use before refactoring. "
                    "Files not listed do not depend on the target."
                ),
                inputSchema={"type": "object", "properties": {
                    "name": {"type": "string"}
                }, "required": ["name"]},
            ),
            types.Tool(
                name="path",
                description="Trace the shortest path between two symbols in the codebase graph.",
                inputSchema={"type": "object", "properties": {
                    "from": {"type": "string"}, "to": {"type": "string"}
                }, "required": ["from", "to"]},
            ),
            types.Tool(
                name="local_files",
                description=(
                    "Read full source files from disk. EXPENSIVE. "
                    "Prefer built-in Read with line ranges from search/node results instead."
                ),
                inputSchema={"type": "object", "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "max_bytes": {"type": "integer", "default": 15000}
                }, "required": ["paths"]},
            ),
            types.Tool(
                name="package",
                description=(
                    "Resolve an npm package to its entry point files (main, types, exports). "
                    "Reads node_modules/{name}/package.json. "
                    "Use this when you need to find symbols in external npm packages "
                    "that aren't in the codebase graph. Then use built-in Read on the returned types/main file path."
                ),
                inputSchema={"type": "object", "properties": {
                    "name": {"type": "string", "description": "npm package name (e.g. @romach/enums, lodash)"}
                }, "required": ["name"]},
            ),
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        provider = _get_or_reload_provider()
        if not provider:
            return [types.TextContent(type="text", text="No graph database available. Run with --sync to re-download.")]
        try:
            text = _dispatch(name, arguments, provider)
        except Exception as e:
            import traceback
            traceback.print_exc(file=sys.stderr)
            text = f"Error: {str(e)[:500]}"
        return [types.TextContent(type="text", text=text)]

    import asyncio
    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
