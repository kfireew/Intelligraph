"""
retrieval.py — Backend-owned graph retrieval pipeline.

Phase 5: Wires ExecutionPlanner → NodeResolver → TraversalPlanner
         → NeighborhoodRanker → ChunkRetriever → ContextMerger.
"""

import os
import logging

log = logging.getLogger(__name__)


# ── Task policy (per-task compression + depth) ──
#
# NOTE: The "compression" field is a PLANNED policy target.
# Headroom is NOT implemented. _apply_policy in retriever.py is a no-op.
# "full", "partial", "none" are future targets only.
# Exact-code tasks ("debug", "refactor", "security", "impact", "what_is")
# should never be compressed when Headroom is added.
def task_policy(task_type: str) -> dict:
    """Return compression + traversal policy for a task type.
    
    NOTE: "compression" is a PLANNED policy target only.
    Headroom is NOT implemented. _apply_policy is a no-op.
    """
    policies = {
        "explain":      {"compression": "full",    "depth": 2, "include_callgraph": False},
        "nx_architecture": {"compression": "full",    "depth": 1, "include_callgraph": False},
        "architecture": {"compression": "full",    "depth": 3, "include_callgraph": True},
        "how_works":    {"compression": "partial", "depth": 2, "include_callgraph": True},
        "what_is":      {"compression": "none",    "depth": 1, "include_callgraph": False},
        "impact":       {"compression": "none",    "depth": 3, "include_callgraph": True},
        "debug":        {"compression": "none",    "depth": 2, "include_callgraph": True},
        "refactor":     {"compression": "none",    "depth": 2, "include_callgraph": True},
        "security":     {"compression": "none",    "depth": 3, "include_callgraph": True},
    }
    return policies.get(task_type, policies["how_works"])


# Intent detection is in planner.py
from planner import detect_intent  # task_policy is defined above, not imported

# ── Main entry point ──

def retrieve_context(proj: dict, prompt: str) -> dict:
    """Main retrieval pipeline.
    
    Uses the full Phase 5 module architecture:
    ExecutionPlanner → NodeResolver → TraversalPlanner
    → NeighborhoodRanker → ChunkRetriever → ContextMerger
    
    Args:
        proj:   Project dict with graphify_data, repo_dir, etc.
        prompt: User's natural language query.
    
    Returns:
        { context: str, files: [str], strategy: str, plan: {} }
    """
    if not proj or not proj.get("graphify_data"):
        return {"context": "", "files": [], "strategy": "no_data", "plan": {}}

    graphify_data = proj["graphify_data"]
    links = graphify_data.get("links", [])
    node_map = _build_node_map(graphify_data)
    nx_metadata = proj.get("nx_metadata") or {}

    # ── Re-clone fallback: restore missing repo_dir ──
    repo_dir = proj.get("repo_dir")
    if repo_dir and not os.path.isdir(repo_dir):
        git_url = proj.get("git_url")
        if git_url:
            log.warning("repo_dir missing (%s) — attempting re-clone from %s", repo_dir, git_url)
            try:
                import subprocess
                os.makedirs(repo_dir, exist_ok=True)
                r = subprocess.run(
                    ["git", "clone", "--depth", "1", git_url, repo_dir],
                    capture_output=True, text=True, timeout=120,
                )
                if r.returncode != 0:
                    log.warning("re-clone failed: %s", r.stderr[:200])
                    proj["repo_dir"] = None
                    import shutil
                    shutil.rmtree(repo_dir, ignore_errors=True)
                else:
                    log.info("re-clone succeeded — repo_dir restored")
            except Exception as e:
                log.warning("re-clone exception: %s", str(e)[:200])
                proj["repo_dir"] = None
                import shutil
                shutil.rmtree(repo_dir, ignore_errors=True)

    # 1. ExecutionPlanner: decompose query into task plan
    from planner import plan_query
    plan = plan_query(prompt)
    tasks = plan["tasks"]

    # 2. Execute each task through the pipeline
    per_task_results = []
    all_files = set()

    for task in tasks:
        # ── Nx architecture task (skip normal graph pipeline) ──
        if task["type"] == "nx_architecture" and nx_metadata.get("available"):
            nx_projects = nx_metadata.get("projects", [])
            nx_deps = nx_metadata.get("dependencies", [])
            nx_matched = []
            target_lower = (task["target"] or "").lower()
            for p in nx_projects:
                name_lower = p["name"].lower()
                if target_lower in name_lower or name_lower in target_lower:
                    nx_matched.append(p)
                    continue
                for tag in p.get("tags", []):
                    if tag.lower() in target_lower:
                        nx_matched.append(p)
                        break
                else:
                    root_lower = p.get("root", "").lower()
                    if target_lower and target_lower in root_lower:
                        nx_matched.append(p)
            # Deduplicate by name
            seen_nx = set()
            nx_matched_dedup = []
            for p in nx_matched:
                if p["name"] not in seen_nx:
                    seen_nx.add(p["name"])
                    nx_matched_dedup.append(p)
            nx_files = [{"file_path": p["root"], "score": 10, "reason": ["nx_project_match"]} for p in nx_matched_dedup]
            per_task_results.append({
                "task_id": task["id"],
                "files": nx_files,
                "chunks": [],
                "expanded_nodes": [f"nx:project:{p['name']}" for p in nx_matched_dedup],
                "nx_matched": nx_matched_dedup,
                "nx_deps": nx_deps,
            })
            all_files.update(p["root"] for p in nx_matched_dedup)
            continue
        # ── Live Nx task (requires Nx MCP bridge) ──
        if task.get("requires_live_nx"):
            nx_mcp_result = {"note": "Nx MCP is disabled by default", "result": None}
            if os.environ.get("INTELLIGRAPH_ENABLE_NX_MCP", "false").lower() == "true":
                try:
                    from nx_mcp_bridge import query_offline_nx_mcp, get_nx_mcp_status
                    status = get_nx_mcp_status(proj.get("repo_dir", ""))
                    if status.get("available"):
                        nx_mcp_result = query_offline_nx_mcp(
                            proj.get("repo_dir", ""),
                            task.get("nx_capability", "status"),
                            {"target": task.get("target", ""), "prompt": prompt},
                        )
                    else:
                        nx_mcp_result = {"note": "Nx MCP not available locally", "result": None}
                except Exception as e:
                    nx_mcp_result = {"note": f"Nx MCP error: {str(e)[:100]}", "result": None}
            per_task_results.append({
                "task_id": task["id"],
                "files": [],
                "chunks": [],
                "expanded_nodes": [],
                "nx_mcp_result": nx_mcp_result,
                "is_live_nx": True,
            })
            continue
        # ── NodeResolver: text → graph nodes ──
        from resolver import resolve_nodes
        matched = resolve_nodes(task["target"], graphify_data)

        # Architecture tasks: always prefer graph-heuristic seeding over
        # document/label matches (e.g. "architecture" matches README.md labels)
        if task["type"] == "architecture":
            matched = _seed_architecture_fallback(graphify_data, links, node_map)
        elif not matched:
            # Fall back to BFS against community hubs for other unmatched queries
            matched = resolve_nodes(prompt[:80], graphify_data, max_nodes=5)

        # ── TraversalPlanner: expand nodes ──
        from traversal import plan_traversal
        traversal = plan_traversal(task, matched, links)

        # ── NeighborhoodRanker: rank expanded set ──
        from ranker import rank_neighborhood, build_degree_scores
        expanded_ids = traversal.get("expanded", [])
        ranked = rank_neighborhood(expanded_ids, graphify_data, node_map)

        # ── If no traversal matches, seed from matched/fallback files ──
        if not ranked:
            file_set = set()
            seed_nodes = matched or graphify_data.get("nodes", [])[:100]
            for n in seed_nodes:
                sf = n.get("source_file")
                if sf and sf not in file_set:
                    file_set.add(sf)
                    ranked.append({"file_path": sf, "score": 0 if not matched else 5, "reason": ["fallback"]})

        # ── ChunkRetriever: fetch code ──
        from retriever import retrieve_chunks
        policy = task_policy(task["type"])

        # ── CRG Domain Discovery (architecture/overview tasks only) ──
        crg_domain_files = []
        if task["type"] == "architecture":
            try:
                from crg_domain_finder import find_domain_files_with_crg, get_crg_db_path
                crg_path = get_crg_db_path(proj)
                if crg_path:
                    crg_domain_files = find_domain_files_with_crg(crg_path, prompt, repo_dir=proj.get("repo_dir"), max_files=12)
                    existing_paths = {r["file_path"] for r in ranked}
                    for cf in crg_domain_files:
                        if cf["file_path"] not in existing_paths:
                            ranked.append(cf)
                            existing_paths.add(cf["file_path"])
            except Exception as e:
                import logging as _l
                _l.getLogger(__name__).warning("CRG domain discovery failed: %s", e)

        chunks = retrieve_chunks(ranked, proj, policy)
        task_files = [r["file_path"] for r in ranked[:20]]
        all_files.update(task_files)

        per_task_results.append({
            "task_id": task["id"],
            "files": ranked[:20],
            "chunks": chunks,
            "expanded_nodes": expanded_ids,
            "crg_domain_files": crg_domain_files,
        })

    # 3. ContextMerger: deduplicate, rank, budget, assemble
    from merger import merge_tasks
    context, merger_stats = merge_tasks(tasks, per_task_results, graphify_data, nx_metadata)
    strategy = "planner"
    if len(tasks) == 1:
        strategy = tasks[0]["type"]

    # Collect matched nodes from planner tasks
    matched_nodes = []
    for result in per_task_results:
        expanded = result.get("expanded_nodes", [])
        for nid in expanded:
            node = node_map.get(nid)
            if node:
                matched_nodes.append({
                    "id": node.get("id", nid),
                    "label": node.get("label", nid),
                    "source_file": node.get("source_file", ""),
                })
    # Deduplicate by id
    seen = set()
    matched_nodes_dedup = []
    for n in matched_nodes:
        if n["id"] not in seen:
            seen.add(n["id"])
            matched_nodes_dedup.append(n)
    # Add Nx matched nodes
    for result in per_task_results:
        for nx_p in result.get("nx_matched", []):
            nid = f"nx:project:{nx_p['name']}"
            if nid not in seen:
                seen.add(nid)
                matched_nodes_dedup.append({
                    "id": nid,
                    "label": nx_p["name"],
                    "source_file": nx_p.get("root", ""),
                })

    # Build context_stats from merger stats + retrieval stats
    repo_dir = proj.get("repo_dir")
    source_available = bool(repo_dir and os.path.isdir(repo_dir))
    raw_chunks = merger_stats.get("raw_chunks", 0)
    raw_code_chars = merger_stats.get("raw_code_chars", 0)
    is_architecture = any(t.get("type") in ("architecture", "overview", "how_works") for t in tasks)
    degraded = not source_available or (is_architecture and raw_chunks < 3 and raw_code_chars < 6000)

    # If context has no real source code, prepend a degraded warning
    if degraded and is_architecture:
        warning = (
            "\n---\n"
            "SOURCE CONTEXT DEGRADED:\n"
            "Repo files were not readable. Answer may only use graph/file metadata.\n"
            "---\n"
        )
        context = warning + context

    context_stats = dict(merger_stats)
    context_stats.update({
        "source_available": source_available,
        "degraded": degraded,
        "task_count": len(tasks),
    })

    return {
        "context": context,
        "files": sorted(all_files)[:20],
        "strategy": strategy,
        "plan": {
            "task_count": len(tasks),
            "tasks": [{"type": t["type"], "target": t["target"],
                       "depth": t["depth"], "compression": t["compression"]} for t in tasks],
        },
        "matched_nodes": matched_nodes_dedup[:50],
        "context_stats": context_stats,
    }


def _seed_architecture_fallback(graphify_data: dict, links: list, node_map: dict = None) -> list:
    """Seed architecture query from entrypoints, high-degree hubs, and dense files.
    
    Returns only CODE nodes. Data/config/doc nodes are handled separately
    by the merger's Data Assets section.
    """
    # Non-code file extensions to exclude from seeding
    _data_ext = {".json", ".xml", ".csv", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".md", ".txt", ".log", ".pdf", ".png", ".jpg"}
    entrypoint_names = {"main.py", "app.py", "index.ts", "main.ts", "server.ts",
                        "database.py", "bridge.py", "models.py", "__init__.py",
                        "routes.py", "cli.py", "manage.py", "middleware.ts"}
    nodes = graphify_data.get("nodes", [])
    if not nodes:
        return []
    links = links or graphify_data.get("links", [])

    if node_map is None:
        node_map = {}
        for n in nodes:
            for key in (n.get("id"), n.get("label"), n.get("qualified_name")):
                if key:
                    node_map[key] = n
                    node_map[key.lower()] = n

    # Build degree scores
    degree = {}
    for l in links:
        for key in (l.get("source"), l.get("target"), l.get("from"), l.get("to")):
            if key:
                degree[key] = degree.get(key, 0) + 1

    # Build file density: how many graph nodes per source_file
    file_density = {}
    for n in nodes:
        sf = n.get("source_file")
        if sf:
            file_density[sf] = file_density.get(sf, 0) + 1
    # Sort files by node count descending
    dense_files = sorted(file_density, key=lambda f: -file_density[f])

    avg_degree = max(len(degree) // max(len(nodes), 1), 1)
    seen_ids = set()
    result = []

    def _is_data(sf):
        return sf and os.path.splitext(sf)[1].lower() in _data_ext

    def _add_node(n):
        nid = n.get("id") or n.get("label")
        sf = n.get("source_file", "")
        if nid and nid not in seen_ids and not _is_data(sf):
            seen_ids.add(nid)
            result.append(n)

    # Tier 1: Entrypoint-filename nodes
    for n in nodes:
        sf = (n.get("source_file") or "").lower()
        fname = sf.split("/")[-1].split("\\")[-1]
        if fname in entrypoint_names:
            _add_node(n)

    # Tier 2: High-degree hubs (degree > 3× average)
    for nid in sorted(degree, key=lambda k: -degree[k]):
        deg = degree[nid]
        if deg > avg_degree * 3:
            n = node_map.get(nid) or node_map.get(nid.lower())
            if n:
                _add_node(n)

    # Tier 3: Dense file representatives — one high-degree node per dense file
    # that hasn't been picked yet.
    for sf in dense_files[:20]:
        if len(result) >= 15:
            break
        # Find the highest-degree node from this file not already in result
        candidates = []
        for n in nodes:
            if n.get("source_file") == sf:
                nid = n.get("id") or n.get("label")
                if nid and nid not in seen_ids:
                    candidates.append((degree.get(nid, 0), n))
        if candidates:
            candidates.sort(key=lambda x: -x[0])
            _add_node(candidates[0][1])

    # Tier 4: Remaining high-degree nodes if still under 15
    if len(result) < 15:
        for nid in sorted(degree, key=lambda k: -degree[k]):
            if len(result) >= 15:
                break
            n = node_map.get(nid) or node_map.get(nid.lower())
            if n and n.get("id") not in seen_ids:
                _add_node(n)

    return result[:15]


def _build_node_map(graphify_data: dict) -> dict:
    """Build id → node lookup map from graphify_data."""
    node_map = {}
    for n in graphify_data.get("nodes", []):
        for key in (n.get("id"), n.get("label"), n.get("qualified_name")):
            if key:
                node_map[key] = n
                node_map[key.lower()] = n
    return node_map