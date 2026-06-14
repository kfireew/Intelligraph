"""
ContextMerger — Synthesizes results across multiple tasks.

Responsibilities:
- Deduplicate files across tasks
- Merge overlapping node explanations
- Compute global ranking across all tasks
- Allocate token budget per task
- Assemble final LLM context string

This is the coordination layer between execution branches.
Without it, multi-task queries produce duplicated/conflicting context.
"""

import os
import logging

HARD_MAX_CONTEXT_CHARS = int(os.environ.get("INTELLIGRAPH_HARD_MAX_CONTEXT", "48000"))
DEFAULT_TOKEN_BUDGET = int(os.environ.get("INTELLIGRAPH_CONTEXT_BUDGET", "12000"))
ARCHITECTURE_BUDGET = int(os.environ.get("INTELLIGRAPH_ARCHITECTURE_BUDGET", "20000"))

def merge_tasks(tasks: list, per_task_results: list, graphify_data: dict, nx_metadata: dict = None) -> tuple:
    """Merge results from multiple tasks into a single deduplicated context.
    
    Args:
        tasks:             [{ id, type, target, ... }] from ExecutionPlanner
        per_task_results:  [{ task_id, files, chunks, expanded_nodes }] per task
        graphify_data:     raw graph data for structure overview
        nx_metadata:       optional Nx workspace metadata dict
    
    Returns:
        assembled context string
    """
    if not tasks:
        return ""

    parts = [
        "You are an expert code analyst. Use ONLY the code graph data provided below. "
        "Cite only actual source file paths. Be precise."
    ]

    # 1. Deduplicate files across tasks
    all_files = _deduplicate_across_tasks(per_task_results)
    task_file_map = {r["task_id"]: r.get("files", []) for r in per_task_results}

    # 2. Codebase structure is placed AFTER graphify architecture sections
    # (see section 2e below) so we defer it for architecture tasks
    _all_source_files = sorted(set(
        n.get("source_file", "") for n in graphify_data.get("nodes", []) if n.get("source_file"))
    )

    # 2b. Nx workspace context (when relevant)

    if nx_metadata and nx_metadata.get("available"):
        nx_projects = nx_metadata.get("projects", [])
        nx_deps = nx_metadata.get("dependencies", [])
        if nx_projects:
            nx_parts = ["## Nx Workspace Context"]
            # Check if any Nx-matched tasks exist
            has_nx_task = any(r.get("nx_matched") for r in per_task_results)
            if has_nx_task:
                for result in per_task_results:
                    for p in (result.get("nx_matched") or []):
                        nx_parts.append(f"- **Project:** `{p['name']}`")
                        nx_parts.append(f"  - Root: `{p.get('root', '')}`")
                        nx_parts.append(f"  - Type: `{p.get('type', 'lib')}`")
                        if p.get("tags"):
                            nx_parts.append(f"  - Tags: `{'`, `'.join(p['tags'])}`")
                        if p.get("targets"):
                            nx_parts.append(f"  - Targets: `{'`, `'.join(p['targets'])}`")
                        if p.get("dependencies"):
                            nx_parts.append(f"  - Depends on: `{'`, `'.join(p['dependencies'])}`")
            else:
                # General Nx workspace overview (architecture queries)
                app_count = sum(1 for p in nx_projects if p.get("type") == "app")
                lib_count = sum(1 for p in nx_projects if p.get("type") == "lib")
                other_count = len(nx_projects) - app_count - lib_count
                nx_parts.append(f"- **Projects:** {len(nx_projects)} total ({app_count} apps, {lib_count} libs, {other_count} other)")
                nx_parts.append(f"- **Dependencies:** {len(nx_deps)} edges")
                # List all projects
                for p in nx_projects:
                    nx_parts.append(f"  - `{p['name']}` ({p.get('type', 'lib')}) — `{p.get('root', '')}`")
            parts.append("\n".join(nx_parts))

    # 2c. Determine query type for architecture-aware sections
    is_architecture = any(t.get("type") in ("architecture", "overview", "how_works") for t in tasks)

    # 2d. Graphify architecture context (architecture/overview tasks only)
    if is_architecture:
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

        # Section A: Graphify Architecture Summary
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
        graph_parts.append("\n".join(gsummary))

        # Section B: Important Hubs (degree-based)
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
            graph_parts.append("\n".join(hubs))

        # Section C: Key Relationships (edge samples)
        if links_data:
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
                    tgt_file = node_by_id.get(tgt, {}).get("source_file", "") if tgt else ""
                    rels.append(f"  - `{src_label}` ({src_file}) **{rel_type}** `{tgt_label}` ({tgt_file})")
                    total_rels += 1
                    if total_rels >= 30:
                        break
                if total_rels >= 30:
                    break
            rels.append(f"  *({len(links_data)} total edges; showing {total_rels} samples)*")
            graph_parts.append("\n".join(rels))

        # Section D: Community summary (per-community file groups)
        if community_values:
            comm_files = {}
            for n in nodes:
                c = n.get("community")
                sf = n.get("source_file")
                if c is not None and sf:
                    comm_files.setdefault(c, set()).add(sf)
            comms = ["## Community Structure"]
            sorted_comms = sorted(comm_files.items(), key=lambda x: -len(x[1]))
            for cid, files in sorted_comms[:10]:
                file_list = sorted(files)[:8]
                count = len(files)
                file_str = ", ".join(f"`{f}`" for f in file_list)
                if count > 8:
                    file_str += f", +{count - 8} more files"
                comms.append(f"  - **Community {cid}:** {file_str}")
            graph_parts.append("\n".join(comms))

        # Section E: Data Assets (separate from code hubs)
        _data_extensions = {".json", ".xml", ".csv", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".md", ".txt", ".log"}
        data_assets = []
        for sf in _all_source_files:
            ext = os.path.splitext(sf)[1].lower()
            if ext in _data_extensions:
                data_assets.append(sf)
        if data_assets:
            dassets = ["## Data Assets"]
            dassets.append("Non-code files detected in graph (excluded from raw chunk selection):")
            for sf in sorted(data_assets)[:15]:
                dassets.append(f"  - `{sf}`")
            graph_parts.append("\n".join(dassets))

        # Section F: Domain Workflow Files Found By CRG
        crg_domain_files = []
        for r in per_task_results:
            for cf in r.get("crg_domain_files", []):
                crg_domain_files.append(cf)
        if crg_domain_files:
            domain_layers = {}
            for cf in crg_domain_files:
                for g in cf.get("groups", []):
                    domain_layers[g] = domain_layers.get(g, 0) + 1
            dwf = ["## Domain Workflow Files Found By CRG"]
            for cf in crg_domain_files[:10]:
                terms = ", ".join(cf.get("matched_terms", []))
                dwf.append(f"  - `{cf['file_path']}` — matched: {terms}")
            graph_parts.append("\n".join(dwf))
            # Store for context_stats
            _crg_domain_found = len(crg_domain_files)
            _domain_layers = domain_layers
        else:
            _crg_domain_found = 0
            _domain_layers = {}

        if graph_parts:
            parts.append("\n\n".join(graph_parts))
    # 3. Global file ranking across tasks
    ranked = _global_rank(all_files, task_file_map)

    # 2e. Codebase structure (after graphify architecture sections)
    if _all_source_files:
        structure = "## Codebase Structure\n"
        for f in _all_source_files[:20]:
            structure += f"- `{f}`\n"
        parts.append(structure)

    # 4. Token budget allocation — larger for architecture/overview
    total_budget = ARCHITECTURE_BUDGET if is_architecture else DEFAULT_TOKEN_BUDGET
    budget_per_task = _allocate_budget(tasks, total_budget)

    # 5. Code chunks — deduplicated, globally ranked, budgeted
    code_blocks = []
    seen_chunks = set()
    # Collect CRG file paths and rescue info from all tasks
    crg_paths = set()
    crg_rescue_applied = False
    crg_rescued_files = []
    for result in per_task_results:
        for cf in result.get("crg_domain_files", []):
            if cf.get("file_path"):
                crg_paths.add(cf["file_path"])
        ri = result.get("crg_rescue_info", {})
        if ri.get("applied"):
            crg_rescue_applied = True
            crg_rescued_files.extend(ri.get("rescued_files", []))
    # Track which CRG file paths actually appear in raw code blocks
    crg_file_paths_in_raw_chunks = set()
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
                task_budget = budget_per_task.get(task_id, 2000)
                snippet = chunk.get("content", "")
                if len(snippet) > task_budget:
                    snippet = snippet[:task_budget] + "\n// ... (truncated)"
                lang = (chunk.get("file_path") or "").split(".")[-1] or ""
                block = f"### {chunk['file_path']} -- `{chunk['name']}` (L{chunk['start_line']}-{chunk['end_line']})\n```{lang}\n{snippet}\n```\n"
                if is_architecture and len(code_blocks) >= 15:
                    continue  # skip remaining chunks — capped for architecture
                code_blocks.append((rank_entry["score"], block))
                # Track CRG files that actually entered raw chunks
                if chunk["file_path"] in crg_paths:
                    crg_file_paths_in_raw_chunks.add(chunk["file_path"])

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
        "graphify_edges_used": total_edges,
        "data_assets_included": len(data_assets),
        "omitted_files": omitted,
        "crg_domain_files_found": crg_found,
        "crg_domain_files_included": min(crg_found, 10),
        "crg_domain_files_in_raw_chunks": len(crg_file_paths_in_raw_chunks),
        "crg_rescue_applied": crg_rescue_applied,
        "crg_rescued_files": crg_rescued_files,
        "domain_layers_covered": crg_domain_layers,
    }

    # Enforce hard max
    if len(ctx) > HARD_MAX_CONTEXT_CHARS:
        ctx = ctx[:HARD_MAX_CONTEXT_CHARS] + "\n\n## Context Truncated\nHard max reached. Some sections may be incomplete."
        stats["final_chars"] = len(ctx)
        stats["truncated"] = True

    return ctx, stats


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
    """Rank files globally across all tasks.
    
    Priority:
    - File appears in multiple tasks → boost
    - File has high accumulated score → boost
    - File appears in critical task (impact, security) → boost
    """
    ranked = []
    for fp, data in all_files.items():
        score = data["score"]
        # Multi-task bonus
        if len(data["tasks"]) > 1:
            score += 20 * len(data["tasks"])
        ranked.append({"file_path": fp, "score": score, "tasks": data["tasks"]})

    ranked.sort(key=lambda x: -x["score"])
    return ranked


def _allocate_budget(tasks: list, total_budget: int) -> dict:
    """Allocate token budget per task based on priority.
    
    Higher priority tasks get more budget.
    """
    if not tasks:
        return {}
    # All tasks equal weight by default
    per_task = total_budget // len(tasks)
    return {t["id"]: per_task for t in tasks}