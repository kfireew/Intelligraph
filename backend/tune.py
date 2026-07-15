"""
tune.py — Offline auto-tuning for retrieval pipeline.

Two-stage approach:
  Stage 1 (free): sweep 108 combos of (file_count, crg_ratio, depth) × benchmark queries
                  Score by recall, precision, F1 — no LLM calls
  Stage 2 (paid): top 5 combos per task type × benchmark queries
                  LLM judge (qwen3.6-35b-a3b) scores answer quality 1-5

Usage:
  python tune.py --benchmark benchmarks/graphify.json --project-id 2 --base http://localhost:5050
  python tune.py --benchmark benchmarks/graphify.json --project-id 2 --stage 2 --openrouter-key sk-or-v1-...

Output:
  tuning_profile.json — optimal config per task type
"""

import argparse
import json
import os
import sys
import time
import itertools
from datetime import datetime

import requests

BASE_URL = "http://localhost:5050"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
JUDGE_MODEL = "qwen/qwen3.6-35b-a3b"

GRID_FILE_COUNTS = [5, 10, 15, 20, 25, 30]
GRID_CRG_RATIOS = [0.0, 0.2, 0.33, 0.5, 0.66, 1.0]
GRID_DEPTHS = [1, 2, 3]
GRID_EMBEDDING_WEIGHTS = [0.0, 0.2, 0.4, 0.5, 0.6, 0.8, 1.0]
GRID_SNIPPET_CHARS = [0, 500, 1000, 1500, 2000, 3000]

# Reduced grid for quick runs (24 combos instead of 108)
GRID_FILE_COUNTS_FAST = [5, 10, 15, 20]
GRID_CRG_RATIOS_FAST = [0.0, 0.33, 0.5]
GRID_DEPTHS_FAST = [1, 2]
GRID_EMBEDDING_WEIGHTS_FAST = [0.0, 0.5, 1.0]
GRID_SNIPPET_CHARS_FAST = [0, 1000, 2000]

JUDGE_SYSTEM_PROMPT = """You are evaluating a code assistant's answer for quality.
Rate the answer from 1-5 on three dimensions:
1. CORRECTNESS: Does the answer accurately describe the codebase?
2. COMPLETENESS: Does it cover the key components and their relationships?
3. SPECIFICITY: Does it reference actual files, functions, and symbols (not generic)?

Also list any files the answer mentions that do NOT exist in the expected files list.
These are "hallucinated files."

Respond in this exact format:
SCORE: <1-5>
HALLUCINATED: <comma-separated list or "none">
NOTES: <one sentence>"""


def load_benchmark(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def retrieve(base_url, project_id, prompt, file_count=None, crg_ratio=None, depth=None,
             embedding_weight=None, snippet_chars=None):
    params = {}
    if file_count is not None:
        params["file_count"] = file_count
    if crg_ratio is not None:
        params["crg_ratio"] = crg_ratio
    if depth is not None:
        params["depth"] = depth
    if embedding_weight is not None:
        params["embedding_weight"] = embedding_weight
    if snippet_chars is not None:
        params["snippet_chars"] = snippet_chars
    try:
        r = requests.post(
            f"{base_url}/graph/retrieve-context",
            json={"prompt": prompt, "project_id": project_id},
            params=params,
            timeout=120,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  ERROR: {e}")
        return None


def score_retrieval(result, expected_files):
    retrieved = set(result.get("files", []))
    expected = set(expected_files)
    if not expected:
        return {"recall": 0, "precision": 0, "f1": 0}
    hits = len(retrieved & expected)
    recall = hits / len(expected)
    precision = hits / max(len(retrieved), 1)
    f1 = 2 * (precision * recall) / max(precision + recall, 0.001)
    context = result.get("context", "")
    return {
        "recall": round(recall, 3),
        "precision": round(precision, 3),
        "f1": round(f1, 3),
        "context_chars": len(context),
        "context_tokens": len(context) // 4,
    }


def compute_mrr(retrieved_files, expected_files):
    """Compute Mean Reciprocal Rank across queries.

    For each query, find the rank (1-indexed) of the first relevant file in the
    retrieved list. The reciprocal rank (RR) is 1/rank, or 0 if no relevant
    file appears in the retrieved list. MRR is the mean RR across all queries.

    Args:
        retrieved_files: list of ordered lists, one per query (retrieval order).
        expected_files: list of relevant-file lists, one per query.

    Returns:
        float in [0.0, 1.0].
    """
    rrs = []
    for retrieved, expected in zip(retrieved_files, expected_files):
        expected_set = set(expected)
        rr = 0.0
        for rank, f in enumerate(retrieved, start=1):
            if f in expected_set:
                rr = 1.0 / rank
                break
        rrs.append(rr)
    if not rrs:
        return 0.0
    return sum(rrs) / len(rrs)


def llm_answer(base_url, openrouter_key, model, context, prompt, max_tokens=500):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": context},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    try:
        r = requests.post(
            f"{base_url}/llm/ask",
            json={
                "url": OPENROUTER_URL,
                "token": openrouter_key,
                "payload": payload,
            },
            timeout=60,
        )
        data = r.json()
        if data.get("status") == 200:
            body = json.loads(data["body"])
            answer = body.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = body.get("usage", {})
            return {
                "answer": answer,
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            }
    except Exception as e:
        print(f"  LLM ERROR: {e}")
    return None


def llm_judge(base_url, openrouter_key, model, prompt, expected_files, answer):
    user_msg = (
        f"Question: {prompt}\n"
        f"Expected relevant files: {', '.join(expected_files)}\n"
        f"Answer: {answer}\n"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 200,
        "temperature": 0.1,
    }
    try:
        r = requests.post(
            f"{base_url}/llm/ask",
            json={
                "url": OPENROUTER_URL,
                "token": openrouter_key,
                "payload": payload,
            },
            timeout=30,
        )
        data = r.json()
        if data.get("status") == 200:
            body = json.loads(data["body"])
            text = body.get("choices", [{}])[0].get("message", {}).get("content", "")
            score = 3
            for line in text.split("\n"):
                if line.strip().upper().startswith("SCORE:"):
                    try:
                        score = int(line.split(":")[1].strip().split("/")[0].strip()[0])
                    except (ValueError, IndexError):
                        pass
                if line.strip().upper().startswith("HALLUCINATED:"):
                    hallucinated = line.split(":", 1)[1].strip().lower()
                    has_hallucination = hallucinated != "none" and hallucinated
                    break
            else:
                has_hallucination = False
            return {"score": score, "hallucinated": has_hallucination, "judge_text": text}
    except Exception as e:
        print(f"  JUDGE ERROR: {e}")
    return {"score": 3, "hallucinated": False, "judge_text": "error"}


def normalize(values):
    if not values or max(values) == min(values):
        return [0.5] * len(values)
    lo, hi = min(values), max(values)
    return [(v - lo) / (hi - lo) for v in values]


def main():
    parser = argparse.ArgumentParser(description="Auto-tune retrieval pipeline")
    parser.add_argument("--benchmark", required=True, help="Path to benchmark JSON")
    parser.add_argument("--project-id", type=int, required=True, help="Project ID")
    parser.add_argument("--base", default=BASE_URL, help="Intelligraph base URL")
    parser.add_argument("--stage", type=int, default=1, choices=[1, 2], help="Stage to run (1=free, 2=LLM judge)")
    parser.add_argument("--openrouter-key", default=None, help="OpenRouter API key (for stage 2)")
    parser.add_argument("--top-k", type=int, default=5, help="Top K combos per task type for stage 2")
    parser.add_argument("--output", default="tuning_profile.json", help="Output file")
    parser.add_argument("--fast", action="store_true", help="Use reduced grid (24 combos instead of 108)")
    args = parser.parse_args()

    benchmark = load_benchmark(args.benchmark)
    print(f"=== Benchmark: {len(benchmark)} queries ===")

    # Select grid
    if args.fast:
        grid_fc, grid_crg, grid_depth = GRID_FILE_COUNTS_FAST, GRID_CRG_RATIOS_FAST, GRID_DEPTHS_FAST
        grid_ew, grid_sc = GRID_EMBEDDING_WEIGHTS_FAST, GRID_SNIPPET_CHARS_FAST
    else:
        grid_fc, grid_crg, grid_depth = GRID_FILE_COUNTS, GRID_CRG_RATIOS, GRID_DEPTHS
        grid_ew, grid_sc = GRID_EMBEDDING_WEIGHTS, GRID_SNIPPET_CHARS

    # Group by task_type
    by_type = {}
    for q in benchmark:
        t = q.get("task_type", "architecture")
        by_type.setdefault(t, []).append(q)
    print(f"Task types: {', '.join(f'{k}({len(v)})' for k, v in by_type.items())}")

    # ── Stage 1: Free retrieval sweep ──
    combos = list(itertools.product(grid_fc, grid_crg, grid_depth, grid_ew, grid_sc))
    print(f"\n=== STAGE 1: Retrieval Sweep ({len(combos)} combos x {len(benchmark)} queries = {len(combos) * len(benchmark)} calls) ===")

    results = {}
    for i, (fc, crg, depth, ew, sc) in enumerate(combos):
        print(f"[{i+1}/{len(combos)}] files={fc} crg={crg} depth={depth} emb={ew} snip={sc}", end="")
        combo_scores = []
        retrieved_lists = []
        expected_lists = []
        for q in benchmark:
            r = retrieve(args.base, args.project_id, q["prompt"], file_count=fc, crg_ratio=crg,
                         depth=depth, embedding_weight=ew, snippet_chars=sc)
            if r:
                s = score_retrieval(r, q.get("expected_files", []))
                combo_scores.append(s)
                retrieved_lists.append(r.get("files", []))
                expected_lists.append(q.get("expected_files", []))
            else:
                combo_scores.append({"recall": 0, "precision": 0, "f1": 0, "context_tokens": 0})
                retrieved_lists.append([])
                expected_lists.append(q.get("expected_files", []))
        avg_f1 = sum(s["f1"] for s in combo_scores) / max(len(combo_scores), 1)
        avg_recall = sum(s["recall"] for s in combo_scores) / max(len(combo_scores), 1)
        avg_tokens = sum(s["context_tokens"] for s in combo_scores) / max(len(combo_scores), 1)
        avg_mrr = compute_mrr(retrieved_lists, expected_lists)
        print(f"  recall={avg_recall:.3f}  f1={avg_f1:.3f}  mrr={avg_mrr:.3f}  tokens={int(avg_tokens)}")
        results[(fc, crg, depth, ew, sc)] = {
            "file_count": fc,
            "crg_ratio": crg,
            "depth": depth,
            "embedding_weight": ew,
            "snippet_chars": sc,
            "avg_f1": round(avg_f1, 3),
            "avg_recall": round(avg_recall, 3),
            "avg_tokens": int(avg_tokens),
            "avg_mrr": round(avg_mrr, 3),
        }

    # Find top K per task type (using all queries for now, can refine per-type)
    ranked_combos = sorted(results.values(), key=lambda x: -x["avg_f1"])
    top_combos = ranked_combos[:args.top_k]
    print(f"\nTop {args.top_k} combos:")
    for c in top_combos:
        print(f"  files={c['file_count']} crg={c['crg_ratio']} depth={c['depth']} emb={c.get('embedding_weight')} snip={c.get('snippet_chars')}  f1={c['avg_f1']}  mrr={c['avg_mrr']}  tokens={c['avg_tokens']}")

    # ── Stage 2: LLM judge ──
    if args.stage == 2:
        if not args.openrouter_key:
            print("ERROR: --openrouter-key required for stage 2")
            sys.exit(1)
        print(f"\n=== STAGE 2: LLM Judge ({len(top_combos)} combos × {len(benchmark)} queries = {len(top_combos) * len(benchmark)} LLM calls) ===")
        print(f"Model: {JUDGE_MODEL}")

        stage2_results = []
        for combo in top_combos:
            fc, crg, depth = combo["file_count"], combo["crg_ratio"], combo["depth"]
            ew, sc = combo.get("embedding_weight"), combo.get("snippet_chars")
            print(f"\n  Combo: files={fc} crg={crg} depth={depth} emb={ew} snip={sc}")
            scores = []
            for j, q in enumerate(benchmark):
                print(f"    [{j+1}/{len(benchmark)}] {q['prompt'][:60]}...", end="")
                r = retrieve(args.base, args.project_id, q["prompt"], file_count=fc, crg_ratio=crg,
                             depth=depth, embedding_weight=ew, snippet_chars=sc)
                if not r or not r.get("context"):
                    print(" — no context, skip")
                    continue
                context = r["context"]
                answer_resp = llm_answer(args.base, args.openrouter_key, JUDGE_MODEL, context, q["prompt"])
                if not answer_resp:
                    print(" — LLM failed")
                    continue
                judge = llm_judge(args.base, args.openrouter_key, JUDGE_MODEL, q["prompt"], q.get("expected_files", []), answer_resp["answer"])
                scores.append({
                    "judge_score": judge["score"],
                    "hallucinated": judge["hallucinated"],
                    "prompt_tokens": answer_resp["prompt_tokens"],
                })
                print(f"  judge={judge['score']}  halluc={'Y' if judge['hallucinated'] else 'N'}  tok={answer_resp['prompt_tokens']}")
            avg_judge = sum(s["judge_score"] for s in scores) / max(len(scores), 1)
            halluc_rate = sum(1 for s in scores if s["hallucinated"]) / max(len(scores), 1)
            avg_llm_tokens = sum(s["prompt_tokens"] for s in scores) / max(len(scores), 1)
            stage2_results.append({
                **combo,
                "judge_score": round(avg_judge, 2),
                "hallucination_rate": round(halluc_rate, 3),
                "avg_llm_tokens": int(avg_llm_tokens),
            })
            print(f"  → avg_judge={avg_judge:.2f}  halluc_rate={halluc_rate:.3f}")

        # Final ranking: normalize f1, judge, token savings, mrr
        f1_vals = [r["avg_f1"] for r in stage2_results]
        judge_vals = [r["judge_score"] for r in stage2_results]
        token_vals = [r["avg_tokens"] for r in stage2_results]
        mrr_vals = [r["avg_mrr"] for r in stage2_results]
        norm_f1 = normalize(f1_vals)
        norm_judge = normalize(judge_vals)
        norm_tokens = [1 - n for n in normalize(token_vals)]  # fewer tokens = better
        norm_mrr = normalize(mrr_vals)
        for i, r in enumerate(stage2_results):
            r["final_score"] = round(0.4 * norm_f1[i] + 0.25 * norm_judge[i] + 0.15 * norm_tokens[i] + 0.2 * norm_mrr[i], 3)

        stage2_results.sort(key=lambda x: -x["final_score"])
        best = stage2_results[0]
        print(f"\n=== BEST: files={best['file_count']} crg={best['crg_ratio']} depth={best['depth']} ===")
        print(f"  f1={best['avg_f1']}  judge={best['judge_score']}  halluc={best['hallucination_rate']}  mrr={best['avg_mrr']}  tokens={best['avg_tokens']}  final={best['final_score']}")
    else:
        best = top_combos[0]
        print(f"\n=== BEST (Stage 1 only): files={best['file_count']} crg={best['crg_ratio']} depth={best['depth']}  f1={best['avg_f1']} ===")

    profile = {
        best.get("task_type", "architecture"): {
            "optimal": {
                "files": best["file_count"],
                "crg_ratio": best["crg_ratio"],
                "depth": best["depth"],
                "embedding_weight": best.get("embedding_weight"),
                "snippet_chars": best.get("snippet_chars"),
            },
            "scores": {k: v for k, v in best.items() if k not in ("file_count", "crg_ratio", "depth", "embedding_weight", "snippet_chars")},
        },
        "_generated": datetime.now().isoformat(),
        "_stage": args.stage,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)
    print(f"\nWritten to: {args.output}")


if __name__ == "__main__":
    main()
