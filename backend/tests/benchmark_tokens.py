"""
Benchmark script — measures token reduction and quality of retrieval pipeline.

Runs 5 benchmark questions through the retrieval pipeline and reports:
- Context size (chars + estimated tokens)
- Number of files retrieved
- Number of code chunks
- Whether expected files were found (precision/recall)

Usage:
    python backend/tests/benchmark_tokens.py
"""

import os
import sys
import json

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BACKEND)

from retrieval import retrieve_context


def make_test_project():
    """Create a test project with known structure for benchmarking."""
    nodes = []
    links = []
    
    files = {
        "auth.py": ["login", "logout", "authenticate", "check_token", "refresh_token"],
        "models.py": ["User", "Session", "Token", "Role", "Permission"],
        "routes.py": ["auth_routes", "api_routes", "health_check"],
        "utils.py": ["hash_password", "verify_password", "generate_token", "format_response"],
        "main.py": ["create_app", "run_server"],
        "database.py": ["get_connection", "execute_query", "close_connection"],
    }
    
    for fname, names in files.items():
        for name in names:
            nid = f"{fname}::{name}"
            nodes.append({
                "id": nid,
                "label": name,
                "source_file": fname,
                "community": hash(fname) % 5,
                "content": f"def {name}():\n    pass  # implementation",
            })
    
    # Create call relationships
    call_edges = [
        ("routes.py::auth_routes", "auth.py::login"),
        ("auth.py::login", "auth.py::authenticate"),
        ("auth.py::authenticate", "utils.py::hash_password"),
        ("auth.py::authenticate", "utils.py::verify_password"),
        ("auth.py::login", "models.py::Session"),
        ("auth.py::login", "database.py::execute_query"),
        ("main.py::create_app", "routes.py::auth_routes"),
        ("main.py::create_app", "routes.py::api_routes"),
        ("models.py::User", "database.py::get_connection"),
        ("auth.py::refresh_token", "auth.py::check_token"),
        ("auth.py::check_token", "models.py::Token"),
    ]
    
    for src, tgt in call_edges:
        links.append({"source": src, "target": tgt, "relation": "calls"})
    
    return {
        "graphify_data": {"nodes": nodes, "links": links},
        "repo_dir": None,
        "git_url": "",
        "nx_metadata": {},
    }


BENCHMARK_QUERIES = [
    {
        "query": "What is the login function?",
        "expected_files": ["auth.py"],
        "intent": "what_is",
    },
    {
        "query": "How does authentication work?",
        "expected_files": ["auth.py", "utils.py"],
        "intent": "how_works",
    },
    {
        "query": "Who calls the login function?",
        "expected_files": ["routes.py"],
        "intent": "callers",
    },
    {
        "query": "What is the architecture of this project?",
        "expected_files": ["main.py", "auth.py", "routes.py", "models.py"],
        "intent": "architecture",
    },
    {
        "query": "What breaks if I change the User model?",
        "expected_files": ["models.py", "auth.py", "database.py"],
        "intent": "impact",
    },
]


def estimate_tokens(text):
    """Rough token estimate: ~4 chars per token for code."""
    return len(text) // 4


def run_benchmark():
    proj = make_test_project()
    results = []
    
    print("=" * 80)
    print("INTELLIGRAPH RETRIEVAL BENCHMARK")
    print("=" * 80)
    print()
    
    for bq in BENCHMARK_QUERIES:
        result = retrieve_context(proj, bq["query"])
        
        context = result.get("context", "")
        files = result.get("files", [])
        stats = result.get("context_stats", {})
        
        # Compute precision/recall
        expected = set(bq["expected_files"])
        retrieved = set(files)
        if retrieved:
            precision = len(expected & retrieved) / len(retrieved)
        else:
            precision = 0.0
        recall = len(expected & retrieved) / len(expected) if expected else 1.0
        
        est_tokens = estimate_tokens(context)
        
        results.append({
            "query": bq["query"],
            "intent": bq["intent"],
            "context_chars": len(context),
            "est_tokens": est_tokens,
            "files_retrieved": len(files),
            "files": files,
            "raw_chunks": stats.get("raw_chunks", 0),
            "raw_code_chars": stats.get("raw_code_chars", 0),
            "strategy": result.get("strategy", ""),
            "precision": round(precision, 2),
            "recall": round(recall, 2),
            "expected_found": sorted(expected & retrieved),
            "expected_missing": sorted(expected - retrieved),
        })
    
    # Print results table
    print(f"{'Query':<45} {'Chars':>7} {'Tokens':>7} {'Files':>6} {'Chunks':>7} {'Prec':>5} {'Recall':>6}")
    print("-" * 90)
    for r in results:
        print(f"{r['query'][:44]:<45} {r['context_chars']:>7} {r['est_tokens']:>7} "
              f"{r['files_retrieved']:>6} {r['raw_chunks']:>7} {r['precision']:>5.2f} {r['recall']:>6.2f}")
    
    print()
    total_chars = sum(r["context_chars"] for r in results)
    total_tokens = sum(r["est_tokens"] for r in results)
    avg_precision = sum(r["precision"] for r in results) / len(results)
    avg_recall = sum(r["recall"] for r in results) / len(results)
    
    print(f"{'TOTAL':<45} {total_chars:>7} {total_tokens:>7}")
    print(f"{'AVERAGE':<45} {'':>7} {'':>7} {'':>6} {'':>7} {avg_precision:>5.2f} {avg_recall:>6.2f}")
    print()
    print(f"Total context: {total_chars:,} chars (~{total_tokens:,} tokens)")
    print(f"Average precision: {avg_precision:.2%}")
    print(f"Average recall: {avg_recall:.2%}")
    
    # Save results
    out_path = os.path.join(os.path.dirname(__file__), "benchmark_results.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {out_path}")
    
    return results


if __name__ == "__main__":
    run_benchmark()
