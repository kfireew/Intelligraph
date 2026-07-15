#!/usr/bin/env python
"""
bench_all.py — Objective head-to-head benchmark: Intelligraph vs Graphify vs CRG.

Runs identical queries through all three systems on the same codebase,
collects token counts, F1, MRR, and (optionally) LLM answer quality.

Usage:
  python bench/bench_all.py --repo bench/competitors/code-review-graph --llm-url <URL> --llm-token <TOKEN>

Without --llm-url, only retrieval metrics are computed (no LLM calls).
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# ── Benchmark query suite with ground truth ─────────────────────

BENCHMARK_QUERIES = [
    {
        "id": "q01",
        "prompt": "how does the search system work",
        "expected_files": ["code_review_graph/search.py"],
        "expected_symbols": ["hybrid_search", "_fts_search"],
        "category": "how_works",
    },
    {
        "id": "q02",
        "prompt": "how does graph traversal work",
        "expected_files": ["code_review_graph/tools/query.py", "code_review_graph/graph.py"],
        "expected_symbols": ["traverse_graph_func", "query_graph"],
        "category": "how_works",
    },
    {
        "id": "q03",
        "prompt": "how are embeddings stored and used",
        "expected_files": ["code_review_graph/embeddings.py"],
        "expected_symbols": ["EmbeddingStore", "embed_all_nodes"],
        "category": "how_works",
    },
    {
        "id": "q04",
        "prompt": "how does impact analysis work",
        "expected_files": ["code_review_graph/tools/query.py", "code_review_graph/changes.py"],
        "expected_symbols": ["get_impact_radius", "analyze_changes"],
        "category": "how_works",
    },
    {
        "id": "q05",
        "prompt": "how does the build process work",
        "expected_files": ["code_review_graph/incremental.py", "code_review_graph/tools/build.py"],
        "expected_symbols": ["full_build", "build_or_update_graph"],
        "category": "how_works",
    },
    {
        "id": "q06",
        "prompt": "what is the main entry point",
        "expected_files": ["code_review_graph/main.py", "code_review_graph/__main__.py"],
        "expected_symbols": ["main"],
        "category": "what_is",
    },
    {
        "id": "q07",
        "prompt": "how does code review context generation work",
        "expected_files": ["code_review_graph/tools/review.py"],
        "expected_symbols": ["get_review_context"],
        "category": "how_works",
    },
    {
        "id": "q08",
        "prompt": "how does post-processing work after building the graph",
        "expected_files": ["code_review_graph/postprocessing.py"],
        "expected_symbols": ["run_post_processing"],
        "category": "how_works",
    },
    {
        "id": "q09",
        "prompt": "what languages are supported by the parser",
        "expected_files": ["code_review_graph/parser.py", "code_review_graph/custom_languages.py"],
        "expected_symbols": ["parse_file"],
        "category": "what_is",
    },
    {
        "id": "q10",
        "prompt": "how does community detection work",
        "expected_files": ["code_review_graph/communities.py"],
        "expected_symbols": ["detect_communities"],
        "category": "how_works",
    },
    {
        "id": "q11",
        "prompt": "insert a new node into the graph",
        "expected_files": ["code_review_graph/graph.py"],
        "expected_symbols": ["upsert_node", "store_file_nodes_edges"],
        "category": "semantic",
        "note": "Semantic test: 'insert' should match 'upsert' or 'store'",
    },
    {
        "id": "q12",
        "prompt": "remove a function from the database",
        "expected_files": ["code_review_graph/graph.py", "code_review_graph/incremental.py"],
        "expected_symbols": ["remove_node", "delete"],
        "category": "semantic",
        "note": "Semantic test: 'remove' should match delete/prune functions",
    },
    {
        "id": "q13",
        "prompt": "find similar code by meaning",
        "expected_files": ["code_review_graph/embeddings.py", "code_review_graph/search.py"],
        "expected_symbols": ["semantic_search", "_embedding_search"],
        "category": "semantic",
        "note": "Semantic test: 'similar by meaning' should match embedding search",
    },
    {
        "id": "q14",
        "prompt": "what calls the hybrid_search function and what do those callers call",
        "expected_files": ["code_review_graph/search.py", "code_review_graph/tools/query.py"],
        "expected_symbols": ["hybrid_search"],
        "category": "multihop",
        "note": "Multi-hop test: 2-hop from hybrid_search",
    },
    {
        "id": "q15",
        "prompt": "trace the path from build to graph storage",
        "expected_files": ["code_review_graph/incremental.py", "code_review_graph/graph.py"],
        "expected_symbols": ["full_build", "GraphStore"],
        "category": "multihop",
        "note": "Path traversal: build → store",
    },
    {
        "id": "q16",
        "prompt": "what is the blast radius of changing graph.py",
        "expected_files": ["code_review_graph/graph.py", "code_review_graph/changes.py"],
        "expected_symbols": ["get_impact_radius"],
        "category": "impact",
    },
    {
        "id": "q17",
        "prompt": "how does flow detection work",
        "expected_files": ["code_review_graph/flows.py"],
        "expected_symbols": ["trace_flows", "get_affected_flows"],
        "category": "how_works",
    },
    {
        "id": "q18",
        "prompt": "how does the MCP server register tools",
        "expected_files": ["code_review_graph/main.py"],
        "expected_symbols": ["main", "FastMCP"],
        "category": "how_works",
    },
    {
        "id": "q19",
        "prompt": "how does wiki generation work",
        "expected_files": ["code_review_graph/wiki.py", "code_review_graph/tools/docs.py"],
        "expected_symbols": ["generate_wiki_func"],
        "category": "how_works",
    },
    {
        "id": "q20",
        "prompt": "how does the refactoring tool work",
        "expected_files": ["code_review_graph/refactor.py", "code_review_graph/tools/refactor_tools.py"],
        "expected_symbols": ["refactor_func", "apply_refactor_func"],
        "category": "how_works",
    },
]


def estimate_tokens(text: str) -> int:
    return len(text) // 4 if text else 0


def compute_f1(retrieved_files: list[str], expected_files: list[str]) -> dict:
    expected_set = set()
    for ef in expected_files:
        expected_set.add(ef)
        expected_set.add(ef.split("/")[-1])
        expected_set.add(ef.replace("/", "\\"))
    retrieved_set = set()
    for rf in retrieved_files:
        retrieved_set.add(rf)
        retrieved_set.add(rf.replace("\\", "/").split("/")[-1])
    hits = retrieved_set & expected_set
    precision = len(hits) / max(len(retrieved_set), 1)
    recall = len(hits) / max(len(expected_set), 1)
    f1 = 2 * precision * recall / max(precision + recall, 0.001)
    return {"precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3), "hits": list(hits)}


def compute_mrr(retrieved_files: list[str], expected_files: list[str]) -> float:
    expected_set = set()
    for ef in expected_files:
        expected_set.add(ef)
        expected_set.add(ef.split("/")[-1])
    for i, rf in enumerate(retrieved_files):
        rf_norm = rf.replace("\\", "/")
        rf_basename = rf_norm.split("/")[-1]
        if rf_norm in expected_set or rf_basename in expected_set:
            return 1.0 / (i + 1)
    return 0.0


# ── System runners ───────────────────────────────────────────────

def run_graphify(repo_path: str, query: str) -> dict:
    """Run a query through Graphify's _query_graph_text API."""
    try:
        from graphify.serve import _load_graph, _query_graph_text, _score_nodes, _pick_seeds, _find_node
        graph_json = os.path.join(repo_path, "graphify-out", "graph.json")
        if not os.path.exists(graph_json):
            return {"error": "graph.json not found", "context": "", "files": [], "tokens": 0}
        G = _load_graph(graph_json)
        text = _query_graph_text(G, query, mode="bfs", depth=3, token_budget=2000)
        # Graphify NODE lines: "NODE label [src=path/to/file.py loc=Lxxx community=N]"
        # EDGE lines: "EDGE A --rel--> B [src=path ...]"
        files = set()
        for m in re.finditer(r'src=([^\]\s]+)', text):
            fp = m.group(1).strip()
            if fp and not fp.startswith("http"):
                files.add(fp)
        return {
            "context": text,
            "files": list(files),
            "tokens": estimate_tokens(text),
            "context_chars": len(text),
        }
    except Exception as e:
        return {"error": str(e)[:300], "context": "", "files": [], "tokens": 0}


def run_crg(repo_path: str, query: str) -> dict:
    """Run a query through CRG's search_nodes API (with keyword extraction for FTS5)."""
    try:
        from pathlib import Path
        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import get_db_path
        repo_path_obj = Path(repo_path)
        db_path = get_db_path(repo_path_obj)
        if not os.path.exists(str(db_path)):
            return {"error": f"graph.db not found at {db_path}", "context": "", "files": [], "tokens": 0}
        store = GraphStore(str(db_path))
        # Extract meaningful keywords from the NL query (FTS5 needs keyword matching)
        words = re.split(r'[\s\-_./]+', query.lower())
        stopwords = {"how", "does", "the", "what", "is", "are", "a", "an", "to", "in",
                     "for", "of", "with", "and", "or", "by", "from", "work", "works",
                     "system", "process", "function", "method", "class", "module",
                     "file", "code", "point", "entry", "main", "new", "into", "insert",
                     "remove", "delete", "find", "show", "list", "all", "places",
                     "occurrences", "supported", "after", "building", "changing",
                     "those", "call", "calls", "used", "stored", "generation",
                     "detection", "registration", "register", "tools", "server"}
        keywords = [w for w in words if len(w) > 2 and w not in stopwords]
        # Also try compound terms
        compound = "_".join(keywords[:2]) if len(keywords) >= 2 else ""
        search_terms = [compound] + keywords if compound else keywords
        if not search_terms:
            search_terms = [query.lower()]

        search_results = []
        for term in search_terms:
            raw_results = store.search_nodes(term, limit=20)
            if raw_results:
                for r in raw_results:
                    search_results.append({
                        "name": r.name, "kind": r.kind, "file_path": r.file_path,
                        "signature": getattr(r, "signature", "") or "",
                        "score": 1.0,
                        "qualified_name": r.qualified_name,
                    })
                break  # Use first successful term

        # Deduplicate by qualified_name
        seen_qn = set()
        deduped = []
        for r in search_results:
            qn = r.get("qualified_name", "")
            if qn not in seen_qn:
                seen_qn.add(qn)
                deduped.append(r)
        search_results = deduped[:20]

        files = [r.get("file_path", "") for r in search_results if r.get("file_path")]
        files = [f.replace("\\", "/") for f in files if f]
        # Normalize to repo-relative
        repo_prefix = repo_path.replace("\\", "/") + "/"
        files = [f.replace(repo_prefix, "") if f.startswith(repo_prefix) else f for f in files]

        context_parts = []
        for r in search_results[:10]:
            name = r.get("name", "?")
            kind = r.get("kind", "?")
            fp = r.get("file_path", "?").replace("\\", "/").replace(repo_prefix, "")
            sig = r.get("signature", "")
            context_parts.append(f"- {name} ({kind}) — {fp}")
            if sig:
                context_parts.append(f"  Signature: {sig}")
        context = "\n".join(context_parts) if context_parts else "(no results)"
        store.close()
        return {
            "context": context,
            "files": list(set(files)),
            "tokens": estimate_tokens(context),
            "context_chars": len(context),
        }
    except Exception as e:
        return {"error": str(e)[:300], "context": "", "files": [], "tokens": 0}


def run_intelligraph(repo_path: str, query: str) -> dict:
    """Run a query through Intelligraph's CRGProvider (hybrid_search + traverse + snippets + rationale)."""
    try:
        backend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
        sys.path.insert(0, backend_dir)
        from crg_intelligence import CRGProvider
        crg_db = os.path.join(repo_path, ".code-review-graph", "graph.db")
        graphify_json = os.path.join(repo_path, "graphify-out", "graph.json")
        graphify_data = {}
        if os.path.exists(graphify_json):
            with open(graphify_json) as f:
                graphify_data = json.load(f)
        proj = {"crg_db_path": crg_db, "graphify_data": graphify_data, "id": 999}
        provider = CRGProvider(proj)
        if not provider.is_available():
            return {"error": "CRGProvider not available", "context": "", "files": [], "tokens": 0}
        results = provider.hybrid_search(query, max_results=10, embedding_weight=0.4)
        files = [r.get("file_path", "") for r in results if r.get("file_path")]
        context_parts = []
        for r in results[:10]:
            name = r.get("name", "?")
            kind = r.get("kind", "?")
            fp = r.get("file_path", "?")
            score = r.get("score", 0)
            mode = r.get("mode", "?")
            context_parts.append(f"- {name} ({kind}) — {fp} [score={score}, {mode}]")
        target = results[0].get("name", query) if results else query
        trav = provider.traverse(target, max_hops=2, max_nodes=20, max_tokens=300)
        if trav.get("nodes"):
            context_parts.append("\n--- Subgraph (2-hop) ---")
            for sn in trav["nodes"][:10]:
                context_parts.append(f"  {'  ' * sn.get('depth', 0)}{sn.get('name', '?')} ({sn.get('kind', '')}) — {sn.get('file', '')}")
        snippets = provider.get_snippets([target] + [n["name"] for n in trav.get("nodes", [])[:3]], max_chars=500)
        if snippets:
            context_parts.append("\n--- Source Code ---")
            for sname, sdata in list(snippets.items())[:3]:
                snip = sdata.get("snippet", "")
                if snip:
                    context_parts.append(f"  {sname}:")
                    context_parts.append(f"    {snip[:300]}")
        rationale = provider.get_rationale(target)
        if rationale:
            context_parts.append("\n--- Notes ---")
            for rn in rationale[:3]:
                context_parts.append(f"  {rn.get('text', '')[:200]}")
        context = "\n".join(context_parts) if context_parts else "(no results)"
        provider.close()
        return {
            "context": context,
            "files": list(set(files)),
            "tokens": estimate_tokens(context),
            "context_chars": len(context),
        }
    except Exception as e:
        return {"error": str(e)[:300], "context": "", "files": [], "tokens": 0}


# ── LLM answer quality evaluation ───────────────────────────────

SYSTEM_PROMPT = """You are an expert software architect helping a developer understand a codebase.
Answer in this structure:
## Summary
2-3 sentences answering the question directly.

## Explanation
Detailed walk-through of the logic. Why, not just what.

## References
- `path/to/file.py` -- what it does

Do not invent files, functions, imports, or APIs.
Answer based on what you have -- do not declare context insufficient."""

def run_llm(llm_url: str, llm_token: str, model: str, prompt: str, context: str) -> dict:
    """Call the LLM with context and return answer + token usage."""
    import requests
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if context:
        messages.append({"role": "user", "content": f"Project context:\n{context}"})
    messages.append({"role": "user", "content": prompt})
    headers = {"Content-Type": "application/json"}
    if llm_token:
        headers["Authorization"] = f"Bearer {llm_token}"
    if "openrouter.ai" in llm_url:
        headers["HTTP-Referer"] = "https://localhost"
        headers["X-Title"] = "Intelligraph-Benchmark"
    payload = {"model": model, "messages": messages, "max_tokens": 2048, "temperature": 0.2}
    try:
        resp = requests.post(llm_url, json=payload, headers=headers, timeout=120, verify=False)
        resp.encoding = "utf-8"
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "answer": "", "tokens": 0}
        body = resp.json()
        answer = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = body.get("usage", {})
        return {
            "answer": answer,
            "tokens": usage.get("completion_tokens", len(answer) // 4),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
    except Exception as e:
        return {"error": str(e)[:300], "answer": "", "tokens": 0}


# ── Main benchmark runner ────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Head-to-head benchmark")
    parser.add_argument("--repo", required=True, help="Path to the benchmark codebase")
    parser.add_argument("--llm-url", default="", help="LLM API URL (e.g. https://openrouter.ai/api/v1/chat/completions)")
    parser.add_argument("--llm-token", default="", help="LLM API token")
    parser.add_argument("--model", default="qwen/qwen3.6-27b", help="LLM model ID")
    parser.add_argument("--output", default="bench/results.json", help="Output JSON path")
    parser.add_argument("--skip-llm", action="store_true", help="Skip LLM evaluation (retrieval metrics only)")
    args = parser.parse_args()

    repo_path = os.path.abspath(args.repo)
    print(f"=== Benchmark: {repo_path} ===")
    print(f"Queries: {len(BENCHMARK_QUERIES)}")
    print(f"LLM: {args.llm_url or '(skipped)'} model={args.model}")
    print()

    systems = [
        ("graphify", run_graphify),
        ("crg", run_crg),
        ("intelligraph", run_intelligraph),
    ]

    all_results = []
    for q in BENCHMARK_QUERIES:
        print(f"\n{'='*60}")
        print(f"Query {q['id']}: {q['prompt']}  [{q['category']}]")
        print(f"Expected files: {q['expected_files']}")
        print(f"{'='*60}")

        q_result = {"query_id": q["id"], "prompt": q["prompt"], "category": q["category"], "expected_files": q["expected_files"], "systems": {}}

        for sys_name, sys_runner in systems:
            t0 = time.time()
            result = sys_runner(repo_path, q["prompt"])
            elapsed = time.time() - t0

            f1_data = compute_f1(result.get("files", []), q["expected_files"])
            mrr = compute_mrr(result.get("files", []), q["expected_files"])

            sys_result = {
                "context_tokens": result.get("tokens", 0),
                "context_chars": result.get("context_chars", 0),
                "files_returned": result.get("files", []),
                "f1": f1_data["f1"],
                "precision": f1_data["precision"],
                "recall": f1_data["recall"],
                "mrr": round(mrr, 3),
                "elapsed_s": round(elapsed, 2),
                "error": result.get("error"),
            }

            if not args.skip_llm and args.llm_url:
                llm_result = run_llm(args.llm_url, args.llm_token, args.model, q["prompt"], result.get("context", ""))
                sys_result["llm_answer"] = llm_result.get("answer", "")[:500]
                sys_result["llm_answer_tokens"] = llm_result.get("tokens", 0)
                sys_result["llm_prompt_tokens"] = llm_result.get("prompt_tokens", 0)
                sys_result["llm_total_tokens"] = llm_result.get("total_tokens", 0)
                sys_result["llm_error"] = llm_result.get("error")

            q_result["systems"][sys_name] = sys_result

            status = "OK" if not sys_result["error"] else f"ERROR: {sys_result['error'][:50]}"
            print(f"  {sys_name:15s} | tokens={sys_result['context_tokens']:5d} | F1={sys_result['f1']:.3f} | MRR={sys_result['mrr']:.3f} | files={len(sys_result['files_returned'])} | {elapsed:.1f}s | {status}")

        all_results.append(q_result)

    # ── Aggregate ────────────────────────────────────────────────
    print(f"\n\n{'='*60}")
    print("=== AGGREGATE RESULTS ===")
    print(f"{'='*60}\n")

    for sys_name, _ in systems:
        results = [r["systems"][sys_name] for r in all_results if sys_name in r["systems"]]
        avg_tokens = sum(r["context_tokens"] for r in results) / len(results)
        avg_f1 = sum(r["f1"] for r in results) / len(results)
        avg_mrr = sum(r["mrr"] for r in results) / len(results)
        avg_prec = sum(r["precision"] for r in results) / len(results)
        avg_recall = sum(r["recall"] for r in results) / len(results)
        avg_time = sum(r["elapsed_s"] for r in results) / len(results)
        errors = sum(1 for r in results if r.get("error"))

        # Category breakdown
        cat_f1 = {}
        for q, r in zip(all_results, results):
            cat = q["category"]
            cat_f1.setdefault(cat, []).append(r["f1"])

        print(f"--- {sys_name.upper()} ---")
        print(f"  Avg context tokens:  {avg_tokens:.0f}")
        print(f"  Avg F1:              {avg_f1:.3f}")
        print(f"  Avg precision:       {avg_prec:.3f}")
        print(f"  Avg recall:          {avg_recall:.3f}")
        print(f"  Avg MRR:             {avg_mrr:.3f}")
        print(f"  Avg latency:         {avg_time:.2f}s")
        print(f"  Errors:              {errors}/{len(results)}")
        if cat_f1:
            print(f"  F1 by category:")
            for cat, f1s in sorted(cat_f1.items()):
                print(f"    {cat:15s}: {sum(f1s)/len(f1s):.3f} (n={len(f1s)})")
        print()

    # ── Save JSON ────────────────────────────────────────────────
    output_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({"queries": all_results, "system": "bench_all.py", "repo": repo_path}, f, indent=2)
    print(f"Results saved to {output_path}")


if __name__ == "__main__":
    main()
