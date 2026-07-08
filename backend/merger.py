"""
ContextMerger — Synthesizes results across multiple tasks.

Responsibilities:
- Deduplicate files across tasks
- Merge overlapping node explanations
- Compute global ranking across all tasks
- Allocate token budget per task (priority-weighted)
- Assemble final LLM context string

This is the coordination layer between execution branches.
Without it, multi-task queries produce duplicated/conflicting context.
"""

import os
import logging
import math

HARD_MAX_CONTEXT_CHARS = int(os.environ.get("INTELLIGRAPH_HARD_MAX_CONTEXT", "48000"))
DEFAULT_TOKEN_BUDGET = int(os.environ.get("INTELLIGRAPH_CONTEXT_BUDGET", "12000"))
ARCHITECTURE_BUDGET = int(os.environ.get("INTELLIGRAPH_ARCHITECTURE_BUDGET", "20000"))

# Task priority weights for budget allocation (higher = more budget)
_TASK_PRIORITY = {
    "security": 1.5,
    "impact": 1.5,
    "debug": 1.2,
    "refactor": 1.2,
    "how_works": 1.0,
    "architecture": 1.0,
    "explain": 1.0,
    "nx_architecture": 1.0,
    "callers": 1.0,
    "callees": 1.0,
    "tests": 0.8,
    "what_is": 0.7,
}

# Per-task-type chunk count caps
_TASK_CHUNK_CAPS = {
    "architecture": 15,
    "how_works": 10,
    "explain": 10,
    "debug": 10,
    "refactor": 10,
    "security": 12,
    "impact": 12,
    "callers": 8,
    "callees": 8,
    "tests": 8,
    "what_is": 5,
    "nx_architecture": 10,
}

# Per-section char budgets for architecture overhead
_SECTION_BUDGETS = {
    "A_summary": 1000,
    "B_hubs": 800,
    "C_relationships": 800,
    "D_communities": 1000,
    "E_data_assets": 600,
    "F_crg_domain": 600,
}


def merge_tasks(tasks: list, per_task_results: list, graphify_data: dict, nx_metadata: dict = None) -> tuple:
    """Merge results from multiple tasks into a single deduplicated context.
    
    Args:
        tasks:             [{ id, type, target, ... }] from ExecutionPlanner
        per_task_results:  [{ task_id, files, chunks, expanded_nodes }] per task
        graphify_data:     raw graph data for structure overview
        nx_metadata:       optional Nx workspace metadata dict
    
    Returns:
        (context_string, stats_dict)
    """
    if not tasks:
        return "", {}

    parts = []

    # 1. Deduplicate files across tasks
    all_files = _deduplicate_across_tasks(per_task_results)
    task_file_map = {r["task_id"]: r.get("files", []) for r in per_task_results}

    _all_source_files = sorted(set(
        n.get("source_file", "") for n in graphify_data.get("nodes", []) if n.get("source_file"))
    )

    # 2b. Nx workspace context (when relevant) — capped
    if nx_metadata and nx_metadata.get("available"):
        nx_projects = nx_metadata.get("projects", [])
        nx_deps = nx_metadata.get("dependencies", [])
        if nx_projects:
            nx_parts = ["## Nx Workspace Context"]
            has_nx_task = any(r.get("nx_matched") for r in per_task_results)
            if has_nx_task:
                for result in per_task_results:
                    for p in (result.get("nx_matched") or [])[:20]:  # cap at 20
                        nx_parts.append(f"- **Project:** `{p['name']}`")
                        nx_parts.append(f"  - Root: `{p.get('root', '')}`")
                        nx_parts.append(f"  - Type: `{p.get('type', 'lib')}`")
                        if p.get("tags"):
                            nx_parts.append(f"  - Tags: `{'`, `'.join(p['tags'][:5])}`")
                        if p.get("dependencies"):
                            nx_parts.append(f"  - Depends on: `{'`, `'.join(p['dependencies'][:5])}`")
            else:
                app_count = sum(1 for p in nx_projects if p.get("type") == "app")
                lib_count = sum(1 for p in nx_projects if p.get("type") == "lib")
                nx_parts.append(f"- **Projects:** {len(nx_projects)} total ({app_count} apps, {lib_count} libs)")
                nx_parts.append(f"- **Dependencies:** {len(nx_deps)} edges")
                for p in nx_projects[:30]:  # cap at 30
                    nx_parts.append(f"  - `{p['name']}` ({p.get('type', 'lib')}) — `{p.get('root', '')}`")
            parts.append("\n".join(nx_parts))

    # 2c. Determine query type for architecture-aware sections.
    # A "narrow" query targets a specific subsystem (e.g. "architecture of the parser").
    # A "broad" query asks about the whole codebase (e.g. "overview", "how is it organized").
    #
    # Signal: did the resolver find real node matches for the architecture target?
    # If yes AND the matched file count is small (≤8), it's a narrow query.
    # If the resolver found nothing (fell back to seeds), it's a broad query.
    is_architecture = any(t.get("type") in ("architecture", "overview") for t in tasks)
    _task_results_by_id = {r.get("task_id"): r for r in per_task_results}
    has_specific_target = False
    for t in tasks:
        if t.get("type") not in ("architecture", "overview"):
            continue
        tid = t.get("id")
        result = _task_results_by_id.get(tid, {})
        if not result.get("resolver_matched", False):
            continue
        files = result.get("files", [])
        matched_files = set(f.get("file_path", "") for f in files if f.get("score", 0) > 0)
        if 0 < len(matched_files) <= 8:
            has_specific_target = True
            break

    # 2d. Graphify architecture context (architecture/overview tasks only)
    arch_overhead_chars = 0
    _crg_domain_found = 0
    _domain_layers = {}

    if is_architecture:
        arch_text, arch_overhead_chars, _crg_domain_found, _domain_layers = _build_architecture_sections(
            graphify_data, per_task_results, _all_source_files, has_specific_target
        )
        if arch_text:
            parts.append(arch_text)

    # 3. Global file ranking across tasks
    ranked = _global_rank(all_files, task_file_map)

    # 2e. Codebase structure (after graphify architecture sections) — dedup against code blocks
    if _all_source_files:
        structure = "## Codebase Structure\n"
        for f in _all_source_files[:20]:
            structure += f"- `{f}`\n"
        parts.append(structure)

    # 4. Token budget allocation — priority-weighted
    total_budget = ARCHITECTURE_BUDGET if is_architecture else DEFAULT_TOKEN_BUDGET
    # Subtract architecture overhead from code budget
    code_budget = max(total_budget - arch_overhead_chars, 2000)
    budget_per_task = _allocate_budget(tasks, code_budget)

    # 5. Code chunks — deduplicated, globally ranked, budgeted per-task TOTAL
    code_blocks = _collect_code_blocks(
        ranked, per_task_results, tasks, budget_per_task, is_architecture
    )

    if code_blocks:
        code_blocks.sort(key=lambda x: -x[0])  # highest score first
        code_text = "## Source Code\n"
        for _, block in code_blocks:
            code_text += block
        parts.append(code_text)

    # Build context string and stats
    ctx = "\n\n".join(parts)
    total_edges = len(graphify_data.get("links", graphify_data.get("edges", [])))
    omitted = max(0, len(_all_source_files) - 20)
    raw_code_chars = sum(len(b[1]) for b in code_blocks) if code_blocks else 0
    data_ext = {".json", ".xml", ".csv", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".md", ".txt", ".log"}
    data_assets = [sf for sf in _all_source_files if os.path.splitext(sf)[1].lower() in data_ext]
    crg_found = _crg_domain_found if is_architecture else 0
    crg_domain_layers = _domain_layers if is_architecture else {}
    stats = {
        "budget_chars": total_budget,
        "hard_max_chars": HARD_MAX_CONTEXT_CHARS,
        "final_chars": len(ctx),
        "raw_chunks": len(code_blocks),
        "raw_code_chars": raw_code_chars,
        "arch_overhead_chars": arch_overhead_chars,
        "code_budget_chars": code_budget,
        "graphify_edges_used": total_edges,
        "data_assets_included": len(data_assets),
        "omitted_files": omitted,
        "crg_domain_files_found": crg_found,
        "crg_domain_files_included": min(crg_found, 10),
        "domain_layers_covered": crg_domain_layers,
    }

    # Enforce hard max with smart truncation (never cut mid-code-block)
    if len(ctx) > HARD_MAX_CONTEXT_CHARS:
        ctx = _smart_truncate(ctx, code_blocks, HARD_MAX_CONTEXT_CHARS)
        stats["final_chars"] = len(ctx)
        stats["truncated"] = True

    return ctx, stats


def _build_architecture_sections(graphify_data, per_task_results, _all_source_files, narrow_query):
    """Build architecture sections A-F with per-section char budgets.
    
    For narrow queries (specific target), skip sections B/D/E to save tokens.
    Returns (text, total_chars, crg_found, domain_layers).
    """
    nodes = graphify_data.get("nodes", [])
    links_data = graphify_data.get("links", graphify_data.get("edges", []))
    graph_parts = []

    # File density
    file_counts = {}
    for n in nodes:
        sf = n.get("source_file")
        if sf:
            file_counts[sf] = file_counts.get(sf, 0) + 1
    top_files = sorted(file_counts, key=lambda f: -file_counts[f])[:15]

    # Degree scores
    degree = {}
    for l in links_data:
        for key in (l.get("source"), l.get("target"), l.get("from"), l.get("to")):
            if key:
                degree[key] = degree.get(key, 0) + 1

    node_by_id = {}
    for n in nodes:
        nid = n.get("id")
        if nid:
            node_by_id[nid] = n

    total_chars = 0

    # Section A: Architecture Summary (always)
    community_values = set()
    for n in nodes:
        c = n.get("community")
        if c is not None:
            community_values.add(c)
    gsummary = ["## Graphify Architecture Summary"]
    gsummary.append(f"- **Nodes:** {len(nodes)}")
    gsummary.append(f"- **Edges:** {len(links_data)}")
    gsummary.append(f"- **Communities detected:** {len(community_values)}")
    gsummary.append("")
    gsummary.append("Top files by node count:")
    for sf in top_files[:8]:
        gsummary.append(f"  - `{sf}` — {file_counts[sf]} nodes")
    section_a = "\n".join(gsummary)
    section_a = section_a[:_SECTION_BUDGETS["A_summary"]]
    graph_parts.append(section_a)
    total_chars += len(section_a)

    # Section B: Important Hubs (skip for narrow queries)
    if not narrow_query:
        top_hubs = sorted(degree, key=lambda k: -degree[k])[:15]
        if top_hubs:
            hubs = ["## Important Hubs"]
            for nid in top_hubs[:10]:
                n = node_by_id.get(nid)
                if n:
                    label = n.get("label", nid)
                    sf = n.get("source_file", "")
                    hubs.append(f"  - `{label}` — `{sf}` — degree {degree[nid]}")
                else:
                    hubs.append(f"  - `{nid}` — degree {degree[nid]}")
            section_b = "\n".join(hubs)[:_SECTION_BUDGETS["B_hubs"]]
            graph_parts.append(section_b)
            total_chars += len(section_b)

    # Section C: Key Relationships (skip for narrow queries)
    if not narrow_query and links_data:
        rel_samples = {}
        for l in links_data:
            rel = l.get("relation") or l.get("type") or "related"
            if rel not in rel_samples:
                rel_samples[rel] = []
            if len(rel_samples[rel]) < 15:
                rel_samples[rel].append(l)

        rels = ["## Key Relationships"]
        total_rels = 0
        for rel_type in sorted(rel_samples.keys()):
            samples = rel_samples[rel_type][:5]
            for l in samples:
                src = l.get("source", "?")
                tgt = l.get("target", "?")
                src_label = node_by_id.get(src, {}).get("label", src) if src else src
                tgt_label = node_by_id.get(tgt, {}).get("label", tgt) if tgt else tgt
                src_file = node_by_id.get(src, {}).get("source_file", "") if src else ""
                tgt_file = node_by_id.get(tgt, {}).get("source_file", "") if tgt else tgt
                rels.append(f"  - `{src_label}` ({src_file}) **{rel_type}** `{tgt_label}` ({tgt_file})")
                total_rels += 1
                if total_rels >= 15:  # reduced from 30
                    break
            if total_rels >= 15:
                break
        rels.append(f"  *({len(links_data)} total edges; showing {total_rels} samples)*")
        section_c = "\n".join(rels)[:_SECTION_BUDGETS["C_relationships"]]
        graph_parts.append(section_c)
        total_chars += len(section_c)

    # Section D: Community Structure (skip for narrow queries)
    if not narrow_query and community_values:
        comm_files = {}
        for n in nodes:
            c = n.get("community")
            sf = n.get("source_file")
            if c is not None and sf:
                comm_files.setdefault(c, set()).add(sf)
        comms = ["## Community Structure"]
        sorted_comms = sorted(comm_files.items(), key=lambda x: -len(x[1]))
        for cid, files in sorted_comms[:5]:  # reduced from 10 to 5
            file_list = sorted(files)[:5]  # reduced from 8 to 5
            count = len(files)
            file_str = ", ".join(f"`{f}`" for f in file_list)
            if count > 5:
                file_str += f", +{count - 5} more"
            comms.append(f"  - **Community {cid}:** {file_str}")
        section_d = "\n".join(comms)[:_SECTION_BUDGETS["D_communities"]]
        graph_parts.append(section_d)
        total_chars += len(section_d)

    # Section E: Data Assets (skip for narrow queries)
    if not narrow_query:
        _data_extensions = {".json", ".xml", ".csv", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".md", ".txt", ".log"}
        data_assets = []
        for sf in _all_source_files:
            ext = os.path.splitext(sf)[1].lower()
            if ext in _data_extensions:
                data_assets.append(sf)
        if data_assets:
            dassets = ["## Data Assets"]
            for sf in sorted(data_assets)[:10]:  # reduced from 15 to 10
                dassets.append(f"  - `{sf}`")
            section_e = "\n".join(dassets)[:_SECTION_BUDGETS["E_data_assets"]]
            graph_parts.append(section_e)
            total_chars += len(section_e)

    # Section F: Domain Workflow Files Found By CRG
    crg_domain_files = []
    for r in per_task_results:
        for cf in r.get("crg_domain_files", []):
            crg_domain_files.append(cf)
    _crg_domain_found = len(crg_domain_files)
    _domain_layers = {}
    if crg_domain_files:
        for cf in crg_domain_files:
            for g in cf.get("groups", []):
                _domain_layers[g] = _domain_layers.get(g, 0) + 1
        dwf = ["## Domain Workflow Files Found By CRG"]
        for cf in crg_domain_files[:10]:
            terms = ", ".join(cf.get("matched_terms", []))
            dwf.append(f"  - `{cf['file_path']}` — matched: {terms}")
        section_f = "\n".join(dwf)[:_SECTION_BUDGETS["F_crg_domain"]]
        graph_parts.append(section_f)
        total_chars += len(section_f)

    text = "\n\n".join(graph_parts) if graph_parts else ""
    return text, total_chars, _crg_domain_found, _domain_layers


def _collect_code_blocks(ranked, per_task_results, tasks, budget_per_task, is_architecture):
    """Collect code chunks with per-task TOTAL budget enforcement and chunk count caps."""
    code_blocks = []
    seen_chunks = set()
    task_chars_used = {}  # task_id → running char count
    task_chunk_count = {}  # task_id → chunk count

    # Build task type lookup
    task_type_map = {t["id"]: t.get("type", "how_works") for t in tasks}

    for rank_entry in ranked:
        fp = rank_entry["file_path"]
        for result in per_task_results:
            for chunk in result.get("chunks", []):
                if chunk.get("file_path") != fp:
                    continue
                chunk_key = f"{chunk['file_path']}:{chunk['name']}:{chunk['start_line']}"
                if chunk_key in seen_chunks:
                    continue
                seen_chunks.add(chunk_key)
                task_id = result["task_id"]
                task_type = task_type_map.get(task_id, "how_works")
                task_budget = budget_per_task.get(task_id, 2000)
                chunk_cap = _TASK_CHUNK_CAPS.get(task_type, 10)

                # Check chunk count cap per task
                if task_chunk_count.get(task_id, 0) >= chunk_cap:
                    continue

                # Check per-task total char budget
                used = task_chars_used.get(task_id, 0)
                if used >= task_budget:
                    continue

                snippet = chunk.get("content", "")
                remaining = task_budget - used
                if len(snippet) > remaining:
                    snippet = snippet[:max(remaining, 0)] + "\n// ... (budget truncated)"

                lang = (chunk.get("file_path") or "").split(".")[-1] or ""
                block = f"### {chunk['file_path']} -- `{chunk['name']}` (L{chunk['start_line']}-{chunk['end_line']})\n```{lang}\n{snippet}\n```\n"
                code_blocks.append((rank_entry["score"], block))
                task_chars_used[task_id] = used + len(block)
                task_chunk_count[task_id] = task_chunk_count.get(task_id, 0) + 1

    return code_blocks


def _smart_truncate(ctx, code_blocks, hard_max):
    """Truncate context intelligently — never cut mid-code-block."""
    # If we're over budget, drop lowest-scored code blocks first
    if not code_blocks:
        return ctx[:hard_max] + "\n\n## Context Truncated\n"

    # The context is assembled as: preamble + arch_sections + structure + source_code
    # We need to trim from the end (source code) to preserve preamble + arch
    # Find the "## Source Code" marker
    marker = "## Source Code\n"
    idx = ctx.find(marker)
    if idx < 0:
        return ctx[:hard_max] + "\n\n## Context Truncated\n"

    preamble = ctx[:idx]
    code_section = ctx[idx:]

    # Sort code blocks by score ascending (drop lowest first)
    sorted_blocks = sorted(code_blocks, key=lambda x: x[0])
    blocks_to_drop = []

    while len(preamble) + len(marker) + sum(len(b[1]) for _, b in sorted_blocks) > hard_max and sorted_blocks:
        _, dropped = sorted_blocks.pop(0)
        blocks_to_drop.append(dropped)

    # Rebuild code section with remaining blocks (sorted by score desc)
    remaining_blocks = sorted(sorted_blocks, key=lambda x: -x[0])
    new_code = marker + "".join(b[1] for _, b in remaining_blocks)

    result = preamble + new_code
    if len(result) > hard_max:
        result = result[:hard_max] + "\n\n## Context Truncated\n"
    else:
        result += "\n\n## Context Truncated (low-relevance blocks dropped)\n"
    return result


def _deduplicate_across_tasks(per_task_results: list) -> dict:
    """Merge all files from all tasks, deduplicated, tracking source tasks."""
    all_files = {}  # file_path → { tasks: [task_id], score: total }
    for result in per_task_results:
        task_id = result["task_id"]
        for f in result.get("files", []):
            fp = f["file_path"] if isinstance(f, dict) else f
            score = f.get("score", 0) if isinstance(f, dict) else 0
            if fp not in all_files:
                all_files[fp] = {"tasks": [], "score": 0}
            all_files[fp]["tasks"].append(task_id)
            all_files[fp]["score"] += score
    return all_files


def _global_rank(all_files: dict, task_file_map: dict) -> list:
    """Rank files globally across all tasks."""
    ranked = []
    for fp, data in all_files.items():
        score = data["score"]
        if len(data["tasks"]) > 1:
            score += 20 * len(data["tasks"])
        ranked.append({"file_path": fp, "score": score, "tasks": data["tasks"]})
    ranked.sort(key=lambda x: -x["score"])
    return ranked


def _allocate_budget(tasks: list, total_budget: int) -> dict:
    """Allocate token budget per task based on priority weighting.
    
    Higher priority tasks (security, impact, debug) get more budget.
    Lower priority tasks (what_is, tests) get less.
    """
    if not tasks:
        return {}
    
    weights = []
    for t in tasks:
        task_type = t.get("type", "how_works")
        weights.append(_TASK_PRIORITY.get(task_type, 1.0))
    
    total_weight = sum(weights)
    if total_weight == 0:
        total_weight = len(tasks)
    
    return {t["id"]: int(total_budget * (w / total_weight)) for t, w in zip(tasks, weights)}
