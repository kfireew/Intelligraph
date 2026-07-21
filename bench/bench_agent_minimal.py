#!/usr/bin/env python
"""
bench_agent_minimal.py — 3 queries, 3 LLM calls.
Compares WITH agent (Intelligraph pipeline) vs WITHOUT agent (raw CRG FTS).
Proves the agent helps or hurts.
"""
import json, os, re, sys, time, warnings
warnings.filterwarnings("ignore")
import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

LLM_URL = "https://openrouter.ai/api/v1/chat/completions"
LLM_TOKEN = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = "qwen/qwen3.6-27b"

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "competitors", "code-review-graph")

QUERIES = [
    {"id": "q11", "prompt": "insert a new node into the graph", "expected": ["code_review_graph/graph.py"], "cat": "semantic"},
    {"id": "q15", "prompt": "trace the path from build to graph storage", "expected": ["code_review_graph/incremental.py", "code_review_graph/graph.py"], "cat": "multihop"},
    {"id": "q16", "prompt": "what is the blast radius of changing graph.py", "expected": ["code_review_graph/graph.py", "code_review_graph/changes.py"], "cat": "impact"},
]

def est_tokens(text):
    return len(text) // 4 if text else 0

def run_without_agent(query):
    """Intelligraph WITHOUT agent: hybrid search only (RRF semantic+FTS). No traversal, no snippets, no rationale."""
    from crg_intelligence import CRGProvider
    proj = {
        "crg_db_path": os.path.join(REPO, ".code-review-graph", "graph.db"),
        "graphify_data": {},
        "id": 999,
    }
    provider = CRGProvider(proj)
    provider.is_available()
    results = provider.hybrid_search(query, max_results=10, embedding_weight=0.4)
    parts = []
    for r in results[:10]:
        parts.append(f"- {r['name']} ({r.get('kind','')}) - {r['file_path']} [score={r['score']}]")
    provider.close()
    return "\n".join(parts) if parts else "(no results)"

def run_with_agent(query):
    """Full Intelligraph pipeline: hybrid search + multi-hop traversal + snippets."""
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
        parts.append(f"- {r['name']} ({r.get('kind','')}) - {r['file_path']} [score={r['score']}]")
    if trav.get("nodes"):
        parts.append("\n--- Subgraph ---")
        for sn in trav["nodes"][:8]:
            parts.append(f"  {'  '*sn.get('depth',0)}{sn['name']} - {sn.get('file','')}")
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
    print(f"=== Agent Quality Test: 3 queries, 3 LLM calls ===")
    print(f"Model: {MODEL}")
    print(f"With agent    = Intelligraph (hybrid search + multi-hop + snippets)")
    print(f"Without agent = Raw CRG (keyword FTS only)")
    print()

    all_results = []
    for q in QUERIES:
        print(f"\n{'='*60}")
        print(f"Query {q['id']}: {q['prompt']}  [{q['cat']}]")
        print(f"Expected: {q['expected']}")
        print(f"{'='*60}")

        ctx_no = run_without_agent(q["prompt"])
        ctx_yes = run_with_agent(q["prompt"])
        tok_no = est_tokens(ctx_no)
        tok_yes = est_tokens(ctx_yes)
        print(f"  WITHOUT agent | {tok_no:4d} tokens")
        print(f"  WITH agent    | {tok_yes:4d} tokens")

        prompt = (
            f"Answer this question about a codebase: \"{q['prompt']}\"\n\n"
            f"You will answer TWICE, each time using DIFFERENT context.\n"
            f"Rate each answer 1-5 on CORRECTNESS (mentions correct files/functions) and COMPLETENESS.\n\n"
            f"=== CONTEXT A (Without Agent) ===\n{ctx_no[:3000]}\n\n"
            f"=== CONTEXT B (With Agent) ===\n{ctx_yes[:3000]}\n\n"
            f"Expected files for reference: {q['expected']}\n\n"
            f"Respond in this exact format:\n"
            f"ANSWER_A: <your answer using context A>\n"
            f"ANSWER_B: <your answer using context B>\n"
            f"SCORE_A: <1-5> (correctness), <1-5> (completeness)\n"
            f"SCORE_B: <1-5> (correctness), <1-5> (completeness)\n"
            f"BETTER: <A or B or TIE>\n"
        )
        print("  Calling LLM (1 call)...")
        t0 = time.time()
        response = llm_call([{"role": "user", "content": prompt}], max_tokens=4096)
        elapsed = time.time() - t0
        print(f"  LLM responded in {elapsed:.1f}s")

        scores = {}
        for label, key in [("A","without_agent"),("B","with_agent")]:
            m = re.search(f"SCORE_{label}.*?(\\d).*?(\\d)", response)
            if m:
                scores[key] = {"correctness": int(m.group(1)), "completeness": int(m.group(2))}
            else:
                scores[key] = {"correctness": 0, "completeness": 0}

        better = "TIE"
        m = re.search(r"BETTER:\s*(\w+)", response)
        if m:
            better = m.group(1)

        result = {
            "query_id": q["id"], "prompt": q["prompt"], "category": q["cat"],
            "expected": q["expected"],
            "tokens": {"without_agent": tok_no, "with_agent": tok_yes},
            "scores": scores,
            "better": better,
            "llm_elapsed": round(elapsed, 1),
        }
        all_results.append(result)

        for name, key in [("Without agent","without_agent"),("With agent","with_agent")]:
            s = scores[key]
            print(f"  {name:15s} | score: {s['correctness']}/5 correct, {s['completeness']}/5 complete")
        print(f"  Better: {better}")

    print(f"\n\n{'='*60}")
    print("=== AGGREGATE ===")
    print(f"{'='*60}\n")
    for name, key in [("Without agent","without_agent"),("With agent","with_agent")]:
        results = [r["scores"][key] for r in all_results]
        avg_c = sum(r["correctness"] for r in results) / len(results)
        avg_co = sum(r["completeness"] for r in results) / len(results)
        avg_tok = sum(r["tokens"][key] for r in all_results) / len(all_results)
        print(f"--- {name.upper()} ---")
        print(f"  Avg correctness:   {avg_c:.1f}/5")
        print(f"  Avg completeness:  {avg_co:.1f}/5")
        print(f"  Avg context tokens: {avg_tok:.0f}")
        print(f"  Combined score:   {(avg_c + avg_co) / 2:.1f}/5")
        print()

    wins = sum(1 for r in all_results if r["better"] == "B")
    losses = sum(1 for r in all_results if r["better"] == "A")
    ties = sum(1 for r in all_results if r["better"] == "TIE")
    print(f"Agent wins: {wins} | Agent losses: {losses} | Ties: {ties}")

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results_agent_minimal.json")
    with open(out_path, "w") as f:
        json.dump({"queries": all_results, "model": MODEL}, f, indent=2)
    print(f"\nResults saved to {out_path}")

if __name__ == "__main__":
    main()
