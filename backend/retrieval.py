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

def retrieve_context(proj: dict, prompt: str, overrides: dict = None) -> dict:
    """Main retrieval pipeline.
    
    Uses the full Phase 5 module architecture:
    ExecutionPlanner → NodeResolver → TraversalPlanner
    → NeighborhoodRanker → ChunkRetriever → ContextMerger
    
    Args:
        proj:       Project dict with graphify_data, repo_dir, etc.
        prompt:     User's natural language query.
        overrides:  Optional tuning overrides {file_count, crg_ratio, depth}.
    
    Returns:
        { context: str, files: [str], strategy: str, plan: {}, token_status: str }
    """
    if not proj or not proj.get("graphify_data"):
        return {"context": "", "files": [], "strategy": "no_data", "plan": {}}

    graphify_data = proj["graphify_data"]
    links = graphify_data.get("links", [])
    node_map = _build_node_map(graphify_data)
    nx_metadata = proj.get("nx_metadata") or {}

    # ── Re-clone fallback DISABLED ──
    # repo_dir is now deleted after build to save disk/RAM. On-demand sparse
    # fetch in retriever.py handles file access during chat. The old full
    # re-clone on every chat request was too expensive.
    repo_dir = proj.get("repo_dir")

    # 0. Initialize intelligence providers (CRG, future: Nx, Semgrep, etc.)
    # Do this BEFORE planning so providers can contribute to target extraction.
    intel_providers = []
    intel_context_text = ""
    try:
        from crg_intelligence import get_providers, merge_intelligence_results, render_intelligence_context
        intel_providers = get_providers(proj)
    except Exception as e:
        log.warning("Intelligence provider init failed: %s", e)

    # Inject providers into semantic planner for FTS-based target extraction
    try:
        from semantic_planner import set_providers
        set_providers(intel_providers)
    except Exception:
        pass

    # 1. ExecutionPlanner: decompose query into task plan
    from planner import plan_query
    plan = plan_query(prompt, graphify_data=graphify_data, proj_id=proj.get("id"))
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
        resolver_matched = len(matched) > 0

        # Architecture tasks: merge resolver matches with graph-heuristic seeding
        # (was: replace resolver output entirely — now we keep both for better targeting)
        if task["type"] == "architecture" and matched:
            arch_seeds = _seed_architecture_fallback(graphify_data, links, node_map)
            # Merge: keep resolver matches first, add arch seeds not already present
            seen_ids = {n.get("id") or n.get("label") for n in matched}
            for n in arch_seeds:
                nid = n.get("id") or n.get("label")
                if nid and nid not in seen_ids:
                    matched.append(n)
                    seen_ids.add(nid)
        elif task["type"] == "architecture" and not matched:
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
        ranked = rank_neighborhood(expanded_ids, graphify_data, node_map, query=prompt)

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
        # Apply depth override
        if overrides and "depth" in overrides:
            policy = {**policy, "depth": overrides["depth"]}

        # ── Intelligence providers (CRG + future) per task type ──
        crg_domain_files = []
        crg_rescue_info = {"applied": False, "rescued_files": []}
        intel_files = []
        intel_metadata = []
        intel_mode = None

        if intel_providers:
            for provider in intel_providers:
                try:
                    ttype = task["type"]
                    target = task.get("target") or ""
                    if ttype == "architecture":
                        # Architecture: get community structure
                        arch_data = provider.architecture()
                        if arch_data:
                            intel_metadata.extend(arch_data)
                            # Extract files from communities
                            for c in arch_data:
                                for fp in c.get("files", []):
                                    intel_files.append({"file_path": fp, "score": 6.0, "reason": [f"{provider.name}:community"], "source": provider.name})
                            intel_mode = "architecture"
                            # Also do FTS search for the query
                            search_results = provider.search(prompt, max_results=10)
                            intel_files.extend(search_results)
                    elif ttype in ("impact", "debug", "refactor", "security"):
                        # Impact: blast-radius over CALLS edges
                        impact_results = provider.impact(target, max_depth=2)
                        intel_files.extend(impact_results)
                        intel_mode = "impact"
                    elif ttype == "how_works":
                        # How works: execution flows + FTS search
                        flow_results = provider.flows(target)
                        if flow_results:
                            intel_metadata.extend(flow_results)
                            for f in flow_results:
                                for fp in f.get("files", []):
                                    intel_files.append({"file_path": fp, "score": 8.0, "reason": [f"{provider.name}:flow"], "source": provider.name})
                            intel_mode = "flows"
                        # Also search for the target
                        search_results = provider.search(target or prompt, max_results=10)
                        intel_files.extend(search_results)
                        if not intel_mode and search_results:
                            intel_mode = "search"
                    elif ttype in ("what_is", "search", "callers", "callees"):
                        # Search: FTS symbol search
                        search_results = provider.search(target or prompt, max_results=15)
                        intel_files.extend(search_results)
                        intel_mode = "search"

                except Exception as e:
                    log.warning("Intelligence provider %s failed: %s", provider.name, e)

            # Merge intelligence file results into ranked list
            if intel_files:
                ranked = merge_intelligence_results(ranked, intel_files, provider_name=intel_providers[0].name)

            # Architecture rescue: ensure intelligence files have room in top N
            if task["type"] == "architecture" and intel_files:
                min_slots, max_slots = 2, 5
                if overrides and "crg_ratio" in overrides and "file_count" in overrides:
                    fc = overrides["file_count"]
                    min_slots = round(overrides["crg_ratio"] * fc * 0.7)
                    max_slots = round(overrides["crg_ratio"] * fc)
                ranked, crg_rescue_info = apply_architecture_layer_rescue(
                    ranked, intel_files, min_crg_slots=min_slots, max_crg_slots=max_slots)

            # Render intelligence metadata as context text
            if intel_metadata and intel_mode:
                intel_context_text += render_intelligence_context(intel_metadata, intel_mode)

            # If no metadata was rendered but search results exist, render those
            if not intel_metadata and intel_files and intel_mode:
                intel_context_text += render_intelligence_context(intel_files, intel_mode, max_chars=800)

        file_cap = 30
        if overrides and "file_count" in overrides:
            file_cap = overrides["file_count"]
        chunks = retrieve_chunks(ranked, proj, policy, max_files=file_cap)
        task_files = [r["file_path"] for r in ranked[:file_cap]]
        all_files.update(task_files)

        per_task_results.append({
            "task_id": task["id"],
            "files": ranked[:file_cap],
            "chunks": chunks,
            "expanded_nodes": expanded_ids,
            "resolver_matched": resolver_matched,
            "crg_domain_files": crg_domain_files,
            "crg_rescue_info": crg_rescue_info,
            "intel_metadata": intel_metadata,
            "intel_mode": intel_mode,
            "intel_files": [r["file_path"] for r in intel_files],
        })

    # Propagate token status from proj to result (set by retriever on auth failure)
    token_status = proj.get("_token_status")

    # 3. ContextMerger: deduplicate, rank, budget, assemble
    from merger import merge_tasks
    context, merger_stats = merge_tasks(tasks, per_task_results, graphify_data, nx_metadata)
    # Prepend intelligence context (community summaries, flow paths, etc.)
    if intel_context_text:
        context = intel_context_text + "\n" + context
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
    strategy = "planner"
    if len(tasks) == 1:
        strategy = tasks[0]["type"]

    # Build context_stats from merger stats + retrieval stats
    repo_dir = proj.get("repo_dir")
    raw_chunks = merger_stats.get("raw_chunks", 0)
    raw_code_chars = merger_stats.get("raw_code_chars", 0)
    source_available = bool(repo_dir and os.path.isdir(repo_dir)) or proj.get("_sparse_fetch_ok", False)
    is_architecture = any(t.get("type") in ("architecture", "overview") for t in tasks)
    degraded = not source_available or raw_chunks == 0 or (is_architecture and raw_chunks < 3 and raw_code_chars < 6000)

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

    file_cap_return = 30
    if overrides and "file_count" in overrides:
        file_cap_return = overrides["file_count"]

    return {
        "context": context,
        "files": sorted(all_files)[:file_cap_return],
        "strategy": strategy,
        "plan": {
            "task_count": len(tasks),
            "tasks": [{"type": t["type"], "target": t["target"],
                       "depth": t["depth"], "compression": t["compression"]} for t in tasks],
        },
        "matched_nodes": matched_nodes_dedup[:50],
        "context_stats": context_stats,
        "token_status": token_status,
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

def merge_graphify_and_crg_candidates(
    graphify_ranked: list[dict],
    crg_domain_files: list[dict],
    *,
    max_results: int = 30,
) -> list[dict]:
    """Merge CRG domain files into Graphify-ranked list with combined scores.

    Same file in both → sum scores, merge metadata (graph_score + crg_score).
    New CRG file → insert with graph_score=0, crg_score=crg score.
    Graphify-only file → crg_score=0, graph_score=existing score.

    Returns merged list sorted by score descending, capped at max_results.
    """
    if not crg_domain_files:
        result = []
        for gr in graphify_ranked:
            entry = dict(gr)
            entry.setdefault("graph_score", entry.get("score", 0))
            entry.setdefault("crg_score", 0)
            entry.setdefault("matched_terms", [])
            result.append(entry)
        return result[:max_results]

    # Build lookup by file_path
    merged_map = {}

    # Index graphify entries
    for gr in graphify_ranked:
        fp = gr.get("file_path", "")
        if not fp:
            continue
        base_score = gr.get("score", 0)
        reasons = gr.get("reason", [])
        if isinstance(reasons, str):
            reasons = [reasons]
        merged_map[fp] = {
            "file_path": fp,
            "score": base_score,
            "graph_score": base_score,
            "crg_score": 0,
            "reason": list(reasons),
            "source": "graphify",
            "matched_terms": [],
        }

    # Index CRG entries and merge
    for cf in crg_domain_files:
        fp = cf.get("file_path", "")
        if not fp:
            continue
        crg_score = cf.get("score", 0)
        matched_terms = cf.get("matched_terms", [])
        cf_reason = cf.get("reason", "domain_workflow_match")
        if isinstance(cf_reason, str):
            cf_reason = [cf_reason]

        if fp in merged_map:
            entry = merged_map[fp]
            entry["crg_score"] = crg_score
            entry["score"] = entry["graph_score"] + crg_score
            existing_terms = set(entry.get("matched_terms", []))
            for t in matched_terms:
                existing_terms.add(t)
            entry["matched_terms"] = sorted(existing_terms)
            entry["source"] = "graphify+crg"
            for r in cf_reason:
                if r not in entry["reason"]:
                    entry["reason"].append(r)
        else:
            merged_map[fp] = {
                "file_path": fp,
                "score": crg_score,
                "graph_score": 0,
                "crg_score": crg_score,
                "reason": list(cf_reason),
                "source": "crg_fts",
                "matched_terms": list(matched_terms),
            }

    # Sort by score descending, cap at max_results
    merged = sorted(merged_map.values(), key=lambda x: -x["score"])
    return merged[:max_results]


def apply_architecture_layer_rescue(
    merged_ranked: list[dict],
    crg_domain_files: list[dict],
    *,
    min_crg_slots: int = 2,
    max_crg_slots: int = 5
) -> tuple:
    """Ensure CRG domain files have room in top 15 for architecture tasks.

    If CRG found domain files but fewer than min_crg_slots are in the top 15,
    rescue by replacing lowest-scoring non-critical files.

    Protected files (entrypoints, critical structural files) are never removed.

    Returns:
        (ranked list, rescue_info dict)
        rescue_info = {"applied": bool, "rescued_files": [str]}
    """
    _protected_names = {
        "main.py", "app.py", "bridge.py", "database.py",
        "__init__.py", "server.py", "router.py", "routes.py",
    }
    _protected_reason_substrings = ["entrypoint", "startup", "bridge", "database"]

    if not crg_domain_files:
        return merged_ranked, {"applied": False, "rescued_files": []}

    crg_paths = {cf["file_path"] for cf in crg_domain_files if cf.get("file_path")}

    top15 = merged_ranked[:15]

    # Count CRG files already in top 15
    crg_in_top15 = [e for e in top15 if e["file_path"] in crg_paths]

    if len(crg_in_top15) >= min_crg_slots:
        return merged_ranked, {"applied": False, "rescued_files": []}

    # CRG files beyond current top 15, sorted by score
    rest = merged_ranked[15:]
    crg_rest = [e for e in rest if e["file_path"] in crg_paths]
    crg_rest.sort(key=lambda x: -x["score"])

    if not crg_rest:
        return merged_ranked, {"applied": False, "rescued_files": []}

    def _is_protected(entry):
        fname = entry.get("file_path", "").split("/")[-1].split("\\")[-1]
        if fname in _protected_names:
            return True
        reasons = entry.get("reason", [])
        if isinstance(reasons, str):
            reasons = [reasons]
        for r in reasons:
            rl = r.lower()
            for sub in _protected_reason_substrings:
                if sub in rl:
                    return True
        return False

    # Replaceable files in top 15: not CRG, not protected
    replaceable = [e for e in top15 if e["file_path"] not in crg_paths and not _is_protected(e)]
    replaceable.sort(key=lambda x: x["score"])

    slots_needed = min(min_crg_slots - len(crg_in_top15), len(crg_rest))
    slots_needed = min(slots_needed, max_crg_slots - len(crg_in_top15))

    if slots_needed <= 0 or not replaceable:
        return merged_ranked, {"applied": False, "rescued_files": []}

    replace_count = min(slots_needed, len(replaceable), len(crg_rest))
    replacement_map = {}
    for i in range(replace_count):
        replacement_map[replaceable[i]["file_path"]] = crg_rest[i]

    rescued = []
    new_top15 = []
    for entry in top15:
        if entry["file_path"] in replacement_map:
            repl = replacement_map[entry["file_path"]]
            rescued.append(repl["file_path"])
            new_top15.append(repl)
        else:
            new_top15.append(entry)

    return new_top15 + merged_ranked[15:], {"applied": True, "rescued_files": rescued}


def _build_node_map(graphify_data: dict) -> dict:
    """Build id → node lookup map from graphify_data."""
    node_map = {}
    for n in graphify_data.get("nodes", []):
        for key in (n.get("id"), n.get("label"), n.get("qualified_name")):
            if key:
                node_map[key] = n
                node_map[key.lower()] = n
    return node_map