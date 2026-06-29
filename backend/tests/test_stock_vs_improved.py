"""
A/B comparison: stock retrieval vs improved retrieval.

Creates a REAL on-disk test repo (actual .py files) so the full pipeline runs:
  code_chunker (AST parsing, MAX_CHUNK_CHARS=2000) -> _apply_policy (compression)
  -> _dedup_overlapping -> merger (budget enforcement, chunk caps, smart truncation)

Run: python backend/tests/test_stock_vs_improved.py
"""

import os
import sys
import json
import shutil
import tempfile
import textwrap

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BACKEND)


def create_real_repo():
    """Create a real on-disk Python repo with large files for testing.
    
    This ensures code_chunker runs (AST parsing with MAX_CHUNK_CHARS truncation),
    not just the graphify_data fallback path.
    """
    repo_dir = tempfile.mkdtemp(prefix="intelligraph_test_")
    
    # 15 files, each with 10 functions of ~3000 chars
    file_specs = {
        "auth.py": ["login", "logout", "authenticate", "check_token", "refresh_token",
                     "verify_session", "create_session", "destroy_session", "get_user_session", "revoke_token"],
        "models.py": ["User", "Session", "Token", "Role", "Permission",
                      "Profile", "Account", "Team", "Organization", "ApiKey"],
        "routes.py": ["auth_routes", "api_routes", "health_check", "user_routes", "admin_routes",
                      "webhook_routes", "export_routes", "import_routes", "metrics_routes", "debug_routes"],
        "utils.py": ["hash_password", "verify_password", "generate_token", "format_response",
                     "parse_input", "validate_email", "sanitize_input", "encode_base64", "decode_base64", "timestamp"],
        "main.py": ["create_app", "run_server", "init_db", "init_cache", "init_queue",
                    "register_blueprints", "configure_logging", "setup_middleware", "graceful_shutdown", "health"],
        "database.py": ["get_connection", "execute_query", "close_connection", "begin_transaction",
                        "commit", "rollback", "create_table", "drop_table", "seed_data", "run_migration"],
        "config.py": ["load_config", "validate_config", "get_env", "set_config",
                      "reload_config", "get_database_url", "get_redis_url", "get_secret_key"],
        "middleware.py": ["auth_middleware", "cors_middleware", "logging_middleware", "rate_limit_middleware",
                          "error_handler_middleware", "request_id_middleware", "compression_middleware"],
        "services/email.py": ["send_email", "send_template_email", "queue_email", "process_email_queue",
                              "validate_email_address", "parse_email_template"],
        "services/cache.py": ["get_cache", "set_cache", "delete_cache", "clear_cache",
                              "get_or_set", "invalidate_pattern", "cache_stats"],
        "services/queue.py": ["enqueue", "dequeue", "process_queue", "schedule_task",
                              "cancel_task", "get_task_status", "retry_failed"],
        "services/storage.py": ["upload_file", "download_file", "delete_file", "list_files",
                                "get_signed_url", "create_bucket"],
        "tests/test_auth.py": ["test_login", "test_logout", "test_authenticate", "test_token_refresh",
                               "test_session_management"],
        "tests/test_models.py": ["test_user_model", "test_session_model", "test_token_model"],
        "tests/test_routes.py": ["test_auth_routes", "test_api_routes", "test_health"],
    }
    
    nodes = []
    links = []
    
    for fname, func_names in file_specs.items():
        # Create the actual file on disk
        full_path = os.path.join(repo_dir, fname)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        
        file_content = []
        if fname.endswith(".py") and not fname.startswith("tests/"):
            file_content.append('"""Module: %s"""\n' % fname)
        elif fname.startswith("tests/"):
            file_content.append('"""Tests for %s"""\n' % fname)
            file_content.append("import pytest\n\n")
        
        for func_name in func_names:
            # Generate a large function body (~3000 chars)
            if func_name[0].isupper():
                # Class
                file_content.append(f"class {func_name}:")
                file_content.append(f'    """{func_name} model class."""')
                file_content.append(f"    def __init__(self):")
                file_content.append(f"        self.id = None")
                file_content.append(f"        self.created_at = None")
            else:
                file_content.append(f"def {func_name}(*args, **kwargs):")
                file_content.append(f'    """{func_name} implementation."""')
            
            for i in range(60):
                file_content.append(f"    result_{i} = process_data(arg_{i}, ctx={i})")
            file_content.append("    return result\n")
        
        with open(full_path, "w") as f:
            f.write("\n".join(file_content))
        
        # Create graph nodes for each function
        for func_name in func_names:
            nid = f"{fname}::{func_name}"
            content = "\n".join(file_content)  # simplified
            nodes.append({
                "id": nid,
                "label": func_name,
                "source_file": fname,
                "community": hash(fname) % 8,
                "content": content[:3000],
            })
    
    # Create dense call edges
    file_list = list(file_specs.keys())
    for fi, fname in enumerate(file_list):
        names = file_specs[fname]
        for j, name in enumerate(names[:5]):
            src = f"{fname}::{name}"
            for target_fi in range(1, 4):
                target_fname = file_list[(fi + target_fi) % len(file_list)]
                target_names = file_specs[target_fname]
                for k in range(min(3, len(target_names))):
                    tgt = f"{target_fname}::{target_names[k]}"
                    links.append({"source": src, "target": tgt, "relation": "calls"})
    
    # Create super-hubs
    for j in range(5):
        src = f"main.py::{file_specs['main.py'][j]}"
        for fi in range(2, 12):
            tgt = f"{file_list[fi]}::{file_specs[file_list[fi]][0]}"
            links.append({"source": src, "target": tgt, "relation": "calls"})
    
    # Auth-specific edges (so query relevance can be tested)
    auth_edges = [
        ("routes.py::auth_routes", "auth.py::login"),
        ("routes.py::auth_routes", "auth.py::logout"),
        ("auth.py::login", "auth.py::authenticate"),
        ("auth.py::authenticate", "utils.py::hash_password"),
        ("auth.py::authenticate", "utils.py::verify_password"),
        ("auth.py::login", "models.py::Session"),
        ("auth.py::login", "database.py::execute_query"),
        ("auth.py::refresh_token", "auth.py::check_token"),
        ("auth.py::check_token", "models.py::Token"),
        ("middleware.py::auth_middleware", "auth.py::check_token"),
        ("main.py::create_app", "routes.py::auth_routes"),
        ("main.py::register_blueprints", "routes.py::auth_routes"),
        ("main.py::register_blueprints", "routes.py::api_routes"),
        ("models.py::User", "database.py::get_connection"),
        ("models.py::Session", "database.py::get_connection"),
        ("services/cache.py::get_or_set", "database.py::execute_query"),
    ]
    for src, tgt in auth_edges:
        links.append({"source": src, "target": tgt, "relation": "calls"})
    
    return repo_dir, {"nodes": nodes, "links": links}


# ── STOCK MERGER (simulates pre-improvement behavior) ──

STOCK_HARD_MAX = 48000
STOCK_BUDGET = 12000
STOCK_ARCH_BUDGET = 20000


def stock_merge_tasks(tasks, per_task_results, graphify_data, nx_metadata=None):
    """OLD merger: per-snippet budget only, no chunk cap for non-arch,
    blunt truncation, no smart section budgeting, no compression, how_works=architecture."""
    if not tasks:
        return "", {}
    
    parts = [
        "You are an expert code analyst. Use ONLY the code graph data provided below. "
        "Cite only actual source file paths. Be precise."
    ]
    
    all_files = {}
    for result in per_task_results:
        task_id = result["task_id"]
        for f in result.get("files", []):
            fp = f["file_path"] if isinstance(f, dict) else f
            score = f.get("score", 0) if isinstance(f, dict) else 0
            if fp not in all_files:
                all_files[fp] = {"tasks": [], "score": 0}
            all_files[fp]["tasks"].append(task_id)
            all_files[fp]["score"] += score
    
    _all_source_files = sorted(set(
        n.get("source_file", "") for n in graphify_data.get("nodes", []) if n.get("source_file"))
    )
    
    # OLD: how_works IS architecture (causes huge overhead)
    is_architecture = any(t.get("type") in ("architecture", "overview", "how_works") for t in tasks)
    
    if is_architecture:
        nodes = graphify_data.get("nodes", [])
        links_data = graphify_data.get("links", graphify_data.get("edges", []))
        graph_parts = []
        
        file_counts = {}
        for n in nodes:
            sf = n.get("source_file")
            if sf:
                file_counts[sf] = file_counts.get(sf, 0) + 1
        top_files = sorted(file_counts, key=lambda f: -file_counts[f])[:15]
        
        degree = {}
        for l in links_data:
            for key in (l.get("source"), l.get("target"), l.get("from"), l.get("to")):
                if key:
                    degree[key] = degree.get(key, 0) + 1
        
        node_by_id = {n.get("id"): n for n in nodes if n.get("id")}
        community_values = set(n.get("community") for n in nodes if n.get("community") is not None)
        
        # Section A (always)
        gsummary = ["## Graphify Architecture Summary"]
        gsummary.append(f"- **Nodes:** {len(nodes)}")
        gsummary.append(f"- **Edges:** {len(links_data)}")
        gsummary.append(f"- **Communities detected:** {len(community_values)}")
        gsummary.append("Top files by node count:")
        for sf in top_files[:8]:
            gsummary.append(f"  - `{sf}` -- {file_counts[sf]} nodes")
        graph_parts.append("\n".join(gsummary))
        
        # Section B (always)
        top_hubs = sorted(degree, key=lambda k: -degree[k])[:15]
        if top_hubs:
            hubs = ["## Important Hubs"]
            for nid in top_hubs[:10]:
                n = node_by_id.get(nid)
                if n:
                    hubs.append(f"  - `{n.get('label', nid)}` -- `{n.get('source_file', '')}` -- degree {degree[nid]}")
            graph_parts.append("\n".join(hubs))
        
        # Section C (always, 30 samples)
        if links_data:
            rel_samples = {}
            for l in links_data:
                rel = l.get("relation") or "related"
                if rel not in rel_samples:
                    rel_samples[rel] = []
                if len(rel_samples[rel]) < 15:
                    rel_samples[rel].append(l)
            rels = ["## Key Relationships"]
            total_rels = 0
            for rel_type in sorted(rel_samples.keys()):
                for l in rel_samples[rel_type][:5]:
                    src = l.get("source", "?")
                    tgt = l.get("target", "?")
                    src_label = node_by_id.get(src, {}).get("label", src)
                    tgt_label = node_by_id.get(tgt, {}).get("label", tgt)
                    src_file = node_by_id.get(src, {}).get("source_file", "")
                    tgt_file = node_by_id.get(tgt, {}).get("source_file", "")
                    rels.append(f"  - `{src_label}` ({src_file}) **{rel_type}** `{tgt_label}` ({tgt_file})")
                    total_rels += 1
                    if total_rels >= 30:
                        break
                if total_rels >= 30:
                    break
            rels.append(f"  *({len(links_data)} total edges; showing {total_rels} samples)*")
            graph_parts.append("\n".join(rels))
        
        # Section D (always, 10 communities × 8 files)
        if community_values:
            comm_files = {}
            for n in nodes:
                c = n.get("community")
                sf = n.get("source_file")
                if c is not None and sf:
                    comm_files.setdefault(c, set()).add(sf)
            comms = ["## Community Structure"]
            for cid, files_c in sorted(comm_files.items(), key=lambda x: -len(x[1]))[:10]:
                file_list_c = sorted(files_c)[:8]
                count = len(files_c)
                file_str = ", ".join(f"`{f}`" for f in file_list_c)
                if count > 8:
                    file_str += f", +{count - 8} more files"
                comms.append(f"  - **Community {cid}:** {file_str}")
            graph_parts.append("\n".join(comms))
        
        # Section E (always)
        _data_ext = {".json", ".xml", ".csv", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".md", ".txt", ".log"}
        data_assets = [sf for sf in _all_source_files if os.path.splitext(sf)[1].lower() in _data_ext]
        if data_assets:
            dassets = ["## Data Assets"]
            for sf in sorted(data_assets)[:15]:
                dassets.append(f"  - `{sf}`")
            graph_parts.append("\n".join(dassets))
        
        if graph_parts:
            parts.append("\n\n".join(graph_parts))
    
    if _all_source_files:
        structure = "## Codebase Structure\n"
        for f in _all_source_files[:20]:
            structure += f"- `{f}`\n"
        parts.append(structure)
    
    total_budget = STOCK_ARCH_BUDGET if is_architecture else STOCK_BUDGET
    budget_per_task = total_budget // len(tasks) if tasks else total_budget
    
    # OLD: no chunk cap for non-arch, 15 for arch only, no dedup, no compression
    code_blocks = []
    seen_chunks = set()
    ranked = sorted(all_files.items(), key=lambda x: -x[1]["score"])
    
    for fp, data in ranked:
        for result in per_task_results:
            for chunk in result.get("chunks", []):
                if chunk.get("file_path") != fp:
                    continue
                chunk_key = f"{chunk['file_path']}:{chunk['name']}:{chunk['start_line']}"
                if chunk_key in seen_chunks:
                    continue
                seen_chunks.add(chunk_key)
                
                # OLD: per-snippet truncation only (each snippet can be up to budget)
                snippet = chunk.get("content", "")
                if len(snippet) > budget_per_task:
                    snippet = snippet[:budget_per_task] + "\n// ... (truncated)"
                
                lang = (chunk.get("file_path") or "").split(".")[-1] or ""
                block = f"### {chunk['file_path']} -- `{chunk['name']}` (L{chunk['start_line']}-{chunk['end_line']})\n```{lang}\n{snippet}\n```\n"
                
                if is_architecture and len(code_blocks) >= 15:
                    continue
                code_blocks.append((data["score"], block))
    
    if code_blocks:
        code_blocks.sort(key=lambda x: -x[0])
        code_text = "## Source Code\n"
        for _, block in code_blocks:
            code_text += block
        parts.append(code_text)
    
    ctx = "\n\n".join(parts)
    
    # OLD: blunt truncation
    if len(ctx) > STOCK_HARD_MAX:
        ctx = ctx[:STOCK_HARD_MAX] + "\n\n## Context Truncated\nHard max reached."
    
    raw_code_chars = sum(len(b[1]) for _, b in code_blocks)
    return ctx, {
        "final_chars": len(ctx),
        "raw_chunks": len(code_blocks),
        "raw_code_chars": raw_code_chars,
        "truncated": len(ctx) >= STOCK_HARD_MAX,
    }


def stock_rank_neighborhood(expanded_node_ids, graphify_data, node_map=None):
    """OLD ranker: pure degree centrality, no query relevance, linear degree cap at 20."""
    if not expanded_node_ids or not graphify_data:
        return []
    nodes = graphify_data.get("nodes", [])
    links = graphify_data.get("links", [])
    
    if node_map is None:
        node_map = {}
        for n in nodes:
            for key in (n.get("id"), n.get("label"), n.get("qualified_name")):
                if key:
                    node_map[key] = n
                    node_map[key.lower()] = n
    
    degree = {}
    for l in links:
        for key in (l.get("source"), l.get("target"), l.get("from"), l.get("to")):
            if key:
                degree[key] = degree.get(key, 0) + 1
    
    scored_files = {}
    for nid in expanded_node_ids:
        node = node_map.get(nid) or node_map.get(nid.lower())
        if not node:
            continue
        sf = node.get("source_file")
        if not sf:
            continue
        if sf not in scored_files:
            scored_files[sf] = {"score": 0, "reasons": [], "count": 0}
        entry = scored_files[sf]
        entry["count"] += 1
        deg = degree.get(nid, 0)
        if deg > 0:
            entry["score"] += min(deg * 2, 20)
        avg_deg = max(len(degree) // max(len(nodes), 1), 1)
        if deg > avg_deg * 3:
            entry["score"] += 10
            entry["reasons"].append("hub_node")
        if node.get("community") is not None:
            entry["score"] += 5
        if entry["count"] > 2:
            entry["score"] += entry["count"] * 2
    
    ranked = [
        {"file_path": fp, "score": data["score"], "reason": data["reasons"][:3] if data["reasons"] else ["matched"]}
        for fp, data in sorted(scored_files.items(), key=lambda x: -x[1]["score"])
    ]
    return ranked


def stock_retrieve_chunks(ranked_files, proj, task_policy_dict):
    """OLD retriever: no dedup, no compression, no MAX_CHUNK_CHARS on AST chunks."""
    if not ranked_files:
        return []
    
    file_paths = [rf["file_path"] for rf in ranked_files[:15]]
    chunks = []
    
    repo_dir = proj.get("repo_dir")
    if repo_dir and os.path.isdir(repo_dir):
        try:
            from code_chunker import chunk_files
            # OLD: no MAX_CHUNK_CHARS truncation on AST chunks
            chunks = chunk_files(file_paths, repo_dir=repo_dir, max_chunks=50)
            if chunks:
                # OLD: no dedup, no _apply_policy (was no-op)
                return chunks
        except Exception:
            pass
    
    # Fallback
    gf = proj.get("graphify_data") or {}
    path_set = set(file_paths)
    for n in gf.get("nodes", []):
        sf = n.get("source_file") or n.get("file_path") or ""
        if sf not in path_set:
            continue
        source = n.get("source") or n.get("content") or ""
        if not source:
            continue
        chunks.append({
            "file_path": sf,
            "name": n.get("name") or n.get("label", ""),
            "start_line": n.get("line_start", 1),
            "end_line": n.get("line_end", min(n.get("line_start", 1) + 50, 9999)),
            "content": source[:3000],
        })
    return chunks[:50]


def stock_retrieve_context(proj, prompt):
    """Simulate stock retrieval: no BFS cap, no query relevance, no compression,
    no dedup, old merger, how_works=architecture."""
    from planner import plan_query
    from resolver import resolve_nodes
    from traversal import plan_traversal
    from retrieval import _seed_architecture_fallback, _build_node_map, task_policy
    
    graphify_data = proj["graphify_data"]
    links = graphify_data.get("links", [])
    node_map = _build_node_map(graphify_data)
    
    plan = plan_query(prompt)
    tasks = plan["tasks"]
    
    per_task_results = []
    all_files = set()
    
    for task in tasks:
        matched = resolve_nodes(task["target"], graphify_data)
        if task["type"] == "architecture":
            matched = _seed_architecture_fallback(graphify_data, links, node_map)
        elif not matched:
            matched = resolve_nodes(prompt[:80], graphify_data, max_nodes=5)
        
        traversal = plan_traversal(task, matched, links, max_expanded=999999)
        expanded_ids = traversal.get("expanded", [])
        ranked = stock_rank_neighborhood(expanded_ids, graphify_data, node_map)
        
        if not ranked:
            seed_nodes = matched or graphify_data.get("nodes", [])[:100]
            file_set = set()
            for n in seed_nodes:
                sf = n.get("source_file")
                if sf and sf not in file_set:
                    file_set.add(sf)
                    ranked.append({"file_path": sf, "score": 5, "reason": ["fallback"]})
        
        chunks = stock_retrieve_chunks(ranked, proj, task_policy(task["type"]))
        task_files = [r["file_path"] for r in ranked[:20]]
        all_files.update(task_files)
        
        per_task_results.append({
            "task_id": task["id"],
            "files": ranked[:20],
            "chunks": chunks,
            "expanded_nodes": expanded_ids,
        })
    
    ctx, stats = stock_merge_tasks(tasks, per_task_results, graphify_data, proj.get("nx_metadata", {}))
    return {"context": ctx, "files": sorted(all_files)[:20], "context_stats": stats}


def improved_retrieve_context(proj, prompt):
    from retrieval import retrieve_context
    return retrieve_context(proj, prompt)


BENCHMARK_QUERIES = [
    {"query": "What is the login function?", "expected_files": ["auth.py"]},
    {"query": "How does authentication work?", "expected_files": ["auth.py", "utils.py"]},
    {"query": "Who calls the login function?", "expected_files": ["routes.py"]},
    {"query": "What is the architecture of this project?", "expected_files": ["main.py", "auth.py", "routes.py"]},
    {"query": "What breaks if I change the User model?", "expected_files": ["models.py", "auth.py", "database.py"]},
    {"query": "How does the cache service work?", "expected_files": ["services/cache.py"]},
    {"query": "What does the email service do?", "expected_files": ["services/email.py"]},
]


def estimate_tokens(text):
    return len(text) // 4


def run_comparison():
    repo_dir, graphify_data = create_real_repo()
    node_count = len(graphify_data["nodes"])
    edge_count = len(graphify_data["links"])
    
    proj = {
        "graphify_data": graphify_data,
        "repo_dir": repo_dir,
        "git_url": "",
        "nx_metadata": {},
    }
    
    print("=" * 120)
    print("STOCK vs IMPROVED -- A/B COMPARISON")
    print(f"Test repo: {node_count} nodes, {edge_count} edges, REAL .py files on disk")
    print(f"Repo dir: {repo_dir}")
    print("=" * 120)
    print()
    
    stock_results = []
    improved_results = []
    
    for bq in BENCHMARK_QUERIES:
        try:
            s = stock_retrieve_context(proj, bq["query"])
        except Exception as e:
            s = {"context": f"ERROR: {e}", "files": [], "context_stats": {}}
        s_ctx = s.get("context", "")
        s_files = set(s.get("files", []))
        s_stats = s.get("context_stats", {})
        
        try:
            i = improved_retrieve_context(proj, bq["query"])
        except Exception as e:
            i = {"context": f"ERROR: {e}", "files": [], "context_stats": {}}
        i_ctx = i.get("context", "")
        i_files = set(i.get("files", []))
        i_stats = i.get("context_stats", {})
        
        expected = set(bq["expected_files"])
        s_prec = len(expected & s_files) / len(s_files) if s_files else 0
        s_rec = len(expected & s_files) / len(expected) if expected else 1
        i_prec = len(expected & i_files) / len(i_files) if i_files else 0
        i_rec = len(expected & i_files) / len(expected) if expected else 1
        
        stock_results.append({
            "query": bq["query"], "chars": len(s_ctx), "tokens": estimate_tokens(s_ctx),
            "chunks": s_stats.get("raw_chunks", 0), "code_chars": s_stats.get("raw_code_chars", 0),
            "truncated": s_stats.get("truncated", False), "files": len(s_files),
            "precision": round(s_prec, 2), "recall": round(s_rec, 2),
        })
        improved_results.append({
            "query": bq["query"], "chars": len(i_ctx), "tokens": estimate_tokens(i_ctx),
            "chunks": i_stats.get("raw_chunks", 0), "code_chars": i_stats.get("raw_code_chars", 0),
            "truncated": i_stats.get("truncated", False), "files": len(i_files),
            "precision": round(i_prec, 2), "recall": round(i_rec, 2),
        })
    
    # Token comparison
    print(f"{'Query':<42} {'Stock Chars':>11} {'Impr Chars':>10} {'Reduction':>10} {'Stock Tok':>9} {'Impr Tok':>8} {'Red%':>6}")
    print("-" * 120)
    for s, i in zip(stock_results, improved_results):
        reduction = ((s["chars"] - i["chars"]) / s["chars"] * 100) if s["chars"] > 0 else 0
        tok_reduction = ((s["tokens"] - i["tokens"]) / s["tokens"] * 100) if s["tokens"] > 0 else 0
        print(f"{s['query'][:41]:<42} {s['chars']:>11,} {i['chars']:>10,} {reduction:>9.1f}% {s['tokens']:>9,} {i['tokens']:>8,} {tok_reduction:>5.1f}%")
    
    # Chunk + quality
    print()
    print(f"{'Query':<42} {'Stk Chunks':>10} {'Imp Chunks':>10} {'Stk Trunc':>9} {'Imp Trunc':>9} {'Stk Prec':>9} {'Imp Prec':>8} {'Stk Rec':>7} {'Imp Rec':>7}")
    print("-" * 130)
    for s, i in zip(stock_results, improved_results):
        s_trunc = "YES" if s["truncated"] else "no"
        i_trunc = "YES" if i["truncated"] else "no"
        print(f"{s['query'][:41]:<42} {s['chunks']:>10} {i['chunks']:>10} {s_trunc:>9} {i_trunc:>9} {s['precision']:>9.2f} {i['precision']:>8.2f} {s['recall']:>7.2f} {i['recall']:>7.2f}")
    
    # Totals
    print()
    print("=" * 120)
    s_total_chars = sum(r["chars"] for r in stock_results)
    i_total_chars = sum(r["chars"] for r in improved_results)
    s_total_tokens = sum(r["tokens"] for r in stock_results)
    i_total_tokens = sum(r["tokens"] for r in improved_results)
    s_total_chunks = sum(r["chunks"] for r in stock_results)
    i_total_chunks = sum(r["chunks"] for r in improved_results)
    s_avg_prec = sum(r["precision"] for r in stock_results) / len(stock_results) if stock_results else 0
    i_avg_prec = sum(r["precision"] for r in improved_results) / len(improved_results) if improved_results else 0
    s_avg_rec = sum(r["recall"] for r in stock_results) / len(stock_results) if stock_results else 0
    i_avg_rec = sum(r["recall"] for r in improved_results) / len(improved_results) if improved_results else 0
    s_trunc_count = sum(1 for r in stock_results if r["truncated"])
    i_trunc_count = sum(1 for r in improved_results if r["truncated"])
    
    char_reduction = ((s_total_chars - i_total_chars) / s_total_chars * 100) if s_total_chars > 0 else 0
    tok_reduction = ((s_total_tokens - i_total_tokens) / s_total_tokens * 100) if s_total_tokens > 0 else 0
    chunk_reduction = ((s_total_chunks - i_total_chunks) / s_total_chunks * 100) if s_total_chunks > 0 else 0
    
    print(f"TOTAL CHARS:        stock={s_total_chars:>8,}  improved={i_total_chars:>8,}  reduction={char_reduction:+.1f}%")
    print(f"TOTAL TOKENS:       stock={s_total_tokens:>8,}  improved={i_total_tokens:>8,}  reduction={tok_reduction:+.1f}%")
    print(f"TOTAL CHUNKS:       stock={s_total_chunks:>8}  improved={i_total_chunks:>8}  reduction={chunk_reduction:+.1f}%")
    print(f"TRUNCATED QUERIES:  stock={s_trunc_count}/{len(stock_results)}  improved={i_trunc_count}/{len(improved_results)}")
    print(f"AVG PRECISION:      stock={s_avg_prec:.2%}  improved={i_avg_prec:.2%}  delta={i_avg_prec - s_avg_prec:+.2%}")
    print(f"AVG RECALL:         stock={s_avg_rec:.2%}  improved={i_avg_rec:.2%}  delta={i_avg_rec - s_avg_rec:+.2%}")
    print()
    
    print("VERDICT:")
    if i_total_tokens < s_total_tokens:
        print(f"  [PASS] Token reduction: {tok_reduction:.1f}% fewer tokens ({s_total_tokens:,} -> {i_total_tokens:,})")
    else:
        print(f"  [FAIL] Token INCREASED by {-tok_reduction:.1f}% ({s_total_tokens:,} -> {i_total_tokens:,})")
    if i_avg_prec > s_avg_prec:
        print(f"  [PASS] Precision improved: {s_avg_prec:.2%} -> {i_avg_prec:.2%}")
    elif i_avg_prec == s_avg_prec:
        print(f"  [HOLD] Precision maintained: {s_avg_prec:.2%}")
    else:
        print(f"  [WARN] Precision decreased: {s_avg_prec:.2%} -> {i_avg_prec:.2%}")
    if i_avg_rec >= s_avg_rec:
        print(f"  [PASS] Recall maintained: {s_avg_rec:.2%} -> {i_avg_rec:.2%}")
    else:
        print(f"  [WARN] Recall decreased: {s_avg_rec:.2%} -> {i_avg_rec:.2%}")
    if i_trunc_count < s_trunc_count:
        print(f"  [PASS] Fewer truncations: {s_trunc_count} -> {i_trunc_count}")
    if i_total_chunks < s_total_chunks:
        print(f"  [PASS] Fewer chunks: {s_total_chunks} -> {i_total_chunks}")
    
    # Cleanup
    shutil.rmtree(repo_dir, ignore_errors=True)
    
    out = {
        "test_project": {"nodes": node_count, "edges": edge_count, "real_files": True},
        "stock": stock_results, "improved": improved_results,
        "summary": {
            "stock_total_tokens": s_total_tokens, "improved_total_tokens": i_total_tokens,
            "token_reduction_pct": round(tok_reduction, 1),
            "stock_avg_precision": round(s_avg_prec, 2), "improved_avg_precision": round(i_avg_prec, 2),
            "stock_avg_recall": round(s_avg_rec, 2), "improved_avg_recall": round(i_avg_rec, 2),
        }
    }
    out_path = os.path.join(os.path.dirname(__file__), "stock_vs_improved.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    run_comparison()
