"""Benchmark regression tests for the expanded graphify benchmark suite.

Verifies:
1. The expanded benchmark loads and has >= 15 queries (was 6 in original).
2. Each query exposes prompt, expected_files, and task_type.
3. New query types are present: semantic, multihop, rationale.
4. tune.compute_mrr exists and returns a float in [0, 1].

Run: python -m pytest backend/tests/test_benchmark_regression.py -v
"""

import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(REPO_ROOT, "backend")
sys.path.insert(0, BACKEND)

BENCHMARK_PATH = os.path.join(REPO_ROOT, "benchmarks", "graphify_expanded.json")


@pytest.fixture
def benchmark():
    with open(BENCHMARK_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_expanded_benchmark_has_at_least_15_queries(benchmark):
    assert len(benchmark) >= 15, f"Expected >=15 queries, got {len(benchmark)}"


def test_each_query_has_required_fields(benchmark):
    for i, q in enumerate(benchmark):
        assert "prompt" in q, f"Query {i} missing 'prompt'"
        assert "expected_files" in q, f"Query {i} missing 'expected_files'"
        assert "task_type" in q, f"Query {i} missing 'task_type'"
        assert isinstance(q["expected_files"], list), f"Query {i} 'expected_files' not a list"
        assert q["prompt"], f"Query {i} has empty 'prompt'"
        assert q["task_type"], f"Query {i} has empty 'task_type'"


def test_semantic_queries_present(benchmark):
    types = [q["task_type"] for q in benchmark]
    assert types.count("semantic") >= 1, "Expected at least 1 'semantic' query"


def test_multihop_queries_present(benchmark):
    types = [q["task_type"] for q in benchmark]
    assert types.count("multihop") >= 1, "Expected at least 1 'multihop' query"


def test_rationale_queries_present(benchmark):
    types = [q["task_type"] for q in benchmark]
    assert types.count("rationale") >= 1, "Expected at least 1 'rationale' query"


def test_compute_mrr_exists():
    from tune import compute_mrr
    assert callable(compute_mrr)


def test_compute_mrr_returns_float_in_range():
    from tune import compute_mrr
    retrieved = [["a.py", "b.py", "c.py"], ["x.py", "y.py"]]
    expected = [["b.py"], ["z.py"]]
    mrr = compute_mrr(retrieved, expected)
    assert isinstance(mrr, float)
    assert 0.0 <= mrr <= 1.0


def test_compute_mrr_perfect_rank():
    from tune import compute_mrr
    retrieved = [["a.py", "b.py"], ["c.py", "d.py"]]
    expected = [["a.py"], ["c.py"]]
    mrr = compute_mrr(retrieved, expected)
    assert mrr == pytest.approx(1.0)


def test_compute_mrr_no_match():
    from tune import compute_mrr
    retrieved = [["a.py", "b.py"]]
    expected = [["z.py"]]
    mrr = compute_mrr(retrieved, expected)
    assert mrr == 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
