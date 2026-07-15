#!/usr/bin/env python
"""
bench_llm.py — Minimal-cost LLM answer quality evaluation.

Strategy: run 10 representative queries (not all 20) through all 3 systems,
feed context to Qwen3.6-27B, score answers with a single LLM judge call
per query (not per system). 10 queries × 3 systems = 30 answer calls +
10 judge calls = 40 total LLM calls.

Actually even cheaper: generate all 3 answers in ONE call (multi-turn),
then judge all 3 in ONE call. 10 queries × 2 calls = 20 total LLM calls.
"""

import json
import os
import re
import sys
import time
import warnings
warnings.filterwarnings("ignore")

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

LLM_URL = "https://openrouter.ai/api/v1/chat/completions"
LLM_TOKEN = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = "qwen/qwen3.6-27b"

# 10 representative queries (cover all categories)
QUERIES = [
    {"id": "q01", "prompt": "how does the search system work", "expected": ["code_review_graph/search.py"], "cat": "how_works"},
    {"id": "q03", "prompt": "how are embeddings stored and used", "expected": ["code_review_graph/embeddings.py"], "cat": "how_works"},
    {"id": "q05", "prompt": "how does the build process work", "expected": ["code_review_graph/incremental.py", "code_review_graph/tools/build.py"], "cat": "how_works"},
    {"id": "q09", "prompt": "what languages are supported by the parser", "expected": ["code_review_graph/parser.py", "code_review_graph/custom_languages.py"], "cat": "what_is"},
    {"id": "q10", "prompt": "how does community detection work", "expected": ["code_review_graph/communities.py"], "cat": "how_works"},
    {"id": "q11", "prompt": "insert a new node into the graph", "expected": ["code_review_graph/graph.py"], "cat": "semantic"},
    {"id": "q13", "prompt": "find similar code by meaning", "expected": ["code_review_graph/embeddings.py", "code_review_graph/search.py"], "cat": "semantic"},
    {"id": "q15", "prompt": "trace the path from build to graph storage", "expected": ["code_review_graph/incremental.py", "code_review_graph/graph.py"], "cat": "multihop"},
    {"id": "q16", "prompt": "what is the blast radius of changing graph.py", "expected": ["code_review_graph/graph.py", "code_review_graph/changes.py"], "cat": "impact"},
    {"id": "q19", "prompt": "how does wiki generation work", "expected": ["code_review_graph/wiki.py", "code_review_graph/tools/docs.py"], "cat": "how_works"},
]

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "competitors", "code-review-graph")

def est_tokens(text):
    return len(text) // 4 if text else 0

def run_graphify(query):
    from graphify.serve import _load_graph, _query_graph_text
    G = _load_graph(os.path.join(REPO, "graphify-out", "graph.json"))
    return _query_graph_text(G, query, mode="bfs", depth=3, token_budget=2000)

def run_crg(query):
    from pathlib import Path
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import get_db_path
    store = GraphStore(str(get_db_path(Path(REPO))))
    words = re.split(r'[\s\-_./]+', query.lower())
    stopwords = {"how","does","the","what","is","are","a","an","to","in","for","of","with","and","or","by","from","work","works","system","process","function","method","class","module","file","code","point","entry","main","new","into","insert","remove","delete","find","show","list","all","places","occurrences","supported","after","building","changing","those","call","calls","used","stored","generation","detection","registration","register","tools","server","similar","meaning","trace","path","blast","radius","changing"}
    keywords = [w for w in words if len(w) > 2 and w not in stopwords]
    search_terms = ["_".join(keywords[:2])] + keywords if len(keywords) >= 2 else keywords
    search_terms = search_terms or [query.lower()]
    parts = []
    for term in search_terms:
        raw = store.search_nodes(term, limit=10)
        if raw:
            for r in raw:
                fp = r.file_path.replace("\\","/").replace(REPO.replace("\\","/")+"/","")
                parts.append(f"- {r.name} ({r.kind}) — {fp}")
            break
    store.close()
    return "\n".join(parts) if parts else "(no results)"

def run_intelligraph(query):
    from crg_intelligence import CRGProvider
    proj = {
        "crg_db_path": os.path.join(REPO, ".code-review-graph", "graph.db"),
        "graphify_data": {},
        "id": 999,
    }
    provider = CRGProvider(proj)
    provider.is_available()
    results = provider.hybrid_search(query, max_results=10, embedding_weight=0.4)
    target = results[0]["name"] if results else query
    trav = provider.traverse(target, max_hops=2, max_nodes=20, max_tokens=300)
    snippets = provider.get_snippets([target] + [n["name"] for n in trav.get("nodes",[])[:3]], max_chars=500)
    parts = []
    for r in results[:10]:
        parts.append(f"- {r['name']} ({r.get('kind','')}) — {r['file_path']} [score={r['score']}]")
    if trav.get("nodes"):
        parts.append("\n--- Subgraph ---")
        for sn in trav["nodes"][:8]:
            parts.append(f"  {'  '*sn.get('depth',0)}{sn['name']} — {sn.get('file','')}")
    if snippets:
        parts.append("\n--- Source ---")
        for sn, sd in list(snippets.items())[:2]:
            if sd.get("snippet"):
                parts.append(f"  {sn}: {sd['snippet'][:300]}")
    provider.close()
    return "\n".join(parts)

def llm_call(messages, max_tokens=2048):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LLM_TOKEN}", "HTTP-Referer": "https://localhost", "X-Title": "Intelligraph-Bench"}
    payload = {"model": MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
    r = requests.post(LLM_URL, json=payload, headers=headers, timeout=120, verify=False)
    if r.status_code != 200:
        return f"ERROR {r.status_code}: {r.text[:200]}"
    return r.json().get("choices",[{}])[0].get("message",{}).get("content","")

def main():
    if not LLM_TOKEN:
        print("ERROR: set OPENROUTER_API_KEY env var")
        return
    print(f"=== LLM Answer Quality Benchmark ===")
    print(f"Queries: {len(QUERIES)} | Model: {MODEL} | Total LLM calls: {len(QUERIES) * 2}")
    print()

    all_results = []
    for q in QUERIES:
        print(f"\n{'='*60}")
        print(f"Query {q['id']}: {q['prompt']}  [{q['cat']}]")
        print(f"Expected: {q['expected']}")
        print(f"{'='*60}")

        ctxs = {}
        for name, runner in [("graphify", run_graphify), ("crg", run_crg), ("intelligraph", run_intelligraph)]:
            t0 = time.time()
            ctx = runner(q["prompt"])
            ctxs[name] = ctx
            print(f"  {name:15s} | {est_tokens(ctx):4d}t | {time.time()-t0:.1f}s")

        # ONE call: ask the model to answer the question 3 times, each with different context
        ans_prompt = (
            f"Answer this question about a codebase: \"{q['prompt']}\"\n\n"
            f"You will answer THREE times, each time using DIFFERENT project context.\n"
            f"Rate each answer 1-5 on CORRECTNESS (mentions correct files/functions) and COMPLETENESS.\n\n"
            f"=== CONTEXT A (Graphify) ===\n{ctxs['graphify'][:3000]}\n\n"
            f"=== CONTEXT B (CRG) ===\n{ctxs['crg'][:3000]}\n\n"
            f"=== CONTEXT C (Intelligraph) ===\n{ctxs['intelligraph'][:3000]}\n\n"
            f"Expected files for reference: {q['expected']}\n\n"
            f"Respond in this exact format:\n"
            f"ANSWER_A: <your answer using context A>\n"
            f"ANSWER_B: <your answer using context B>\n"
            f"ANSWER_C: <your answer using context C>\n"
            f"SCORE_A: <1-5> (correctness), <1-5> (completeness)\n"
            f"SCORE_B: <1-5> (correctness), <1-5> (completeness)\n"
            f"SCORE_C: <1-5> (correctness), <1-5> (completeness)\n"
        )
        print("  Calling LLM (1 call for all 3 answers + scores)...")
        t0 = time.time()
        response = llm_call([{"role": "user", "content": ans_prompt}], max_tokens=4096)
        elapsed = time.time() - t0
        print(f"  LLM responded in {elapsed:.1f}s")

        # Parse scores
        scores = {}
        for label, key in [("A","graphify"),("B","crg"),("C","intelligraph")]:
            m = re.search(f"SCORE_{label}.*?(\\d).*?(\\d)", response)
            if m:
                scores[key] = {"correctness": int(m.group(1)), "completeness": int(m.group(2))}
            else:
                scores[key] = {"correctness": 0, "completeness": 0}

        result = {
            "query_id": q["id"], "prompt": q["prompt"], "category": q["cat"],
            "expected": q["expected"],
            "context_tokens": {k: est_tokens(v) for k, v in ctxs.items()},
            "scores": scores,
            "llm_response": response[:2000],
            "llm_elapsed": round(elapsed, 1),
        }
        all_results.append(result)

        for name in ["graphify", "crg", "intelligraph"]:
            s = scores[name]
            print(f"  {name:15s} | score: {s['correctness']}/5 correct, {s['completeness']}/5 complete")

    # Aggregate
    print(f"\n\n{'='*60}")
    print("=== AGGREGATE LLM SCORES ===")
    print(f"{'='*60}\n")
    for name in ["graphify", "crg", "intelligraph"]:
        results = [r["scores"][name] for r in all_results]
        avg_c = sum(r["correctness"] for r in results) / len(results)
        avg_co = sum(r["completeness"] for r in results) / len(results)
        avg_tok = sum(r["context_tokens"][name] for r in all_results) / len(all_results)
        print(f"--- {name.upper()} ---")
        print(f"  Avg correctness:   {avg_c:.1f}/5")
        print(f"  Avg completeness:  {avg_co:.1f}/5")
        print(f"  Avg context tokens: {avg_tok:.0f}")
        print(f"  Combined score:   {(avg_c + avg_co) / 2:.1f}/5")
        print()

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results_llm.json")
    with open(out_path, "w") as f:
        json.dump({"queries": all_results, "model": MODEL}, f, indent=2)
    print(f"Results saved to {out_path}")

if __name__ == "__main__":
    main()
