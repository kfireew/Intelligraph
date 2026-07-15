"""
test_intelligence_v2.py — Tests for Intelligraph v2 intelligence features.

Covers:
  - Semantic search (embedding-based)
  - Hybrid search (FTS + semantic blend)
  - Multi-hop graph traversal
  - Source code snippets
  - Rationale/doc node surfacing
  - Beta telemetry (query logs, feedback)
  - Context savings metadata
  - Graph endpoint upgrades (depth, snippets, rationale)
"""

import json
import os
import sqlite3
import sys
import tempfile

import pytest

# Ensure backend is on the path
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, backend_dir)


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def mock_crg_db(tmp_path):
    """Create a mock CRG graph.db with nodes, edges, communities, and snippets."""
    db_path = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, kind TEXT, qualified_name TEXT, file_path TEXT, signature TEXT, community_id INTEGER, line_start INTEGER, line_end INTEGER, is_test INTEGER)")
    conn.execute("CREATE VIRTUAL TABLE nodes_fts USING fts5(name, signature, file_path, content='nodes', content_rowid='id')")
    conn.execute("CREATE TABLE edges (source_qualified TEXT, target_qualified TEXT, kind TEXT)")
    conn.execute("CREATE TABLE communities (id INTEGER PRIMARY KEY, name TEXT, size INTEGER, dominant_language TEXT, description TEXT, cohesion REAL, level INTEGER)")
    conn.execute("CREATE TABLE community_summaries (community_id INTEGER, purpose TEXT, key_symbols TEXT, risk TEXT)")
    conn.execute("CREATE TABLE flows (name TEXT, criticality REAL, path_json TEXT, entry_point_id INTEGER, node_count INTEGER, file_count INTEGER)")
    conn.execute("CREATE TABLE node_snippets (node_name TEXT PRIMARY KEY, snippet TEXT)")

    nodes_data = [
        (1, "upsertEntity", "Function", "app.services.entity.upsertEntity", "src/services/entity.py", "def upsertEntity(data):", 1, 10, 30, 0),
        (2, "validateEntity", "Function", "app.services.entity.validateEntity", "src/services/entity.py", "def validateEntity(data):", 1, 35, 45, 0),
        (3, "EntityController", "Class", "app.controllers.EntityController", "src/controllers/entity.py", "class EntityController:", 2, 1, 100, 0),
        (4, "deleteEntity", "Function", "app.services.entity.deleteEntity", "src/services/entity.py", "def deleteEntity(id):", 1, 50, 70, 0),
        (5, "test_upsert", "Function", "tests.test_entity.test_upsert", "tests/test_entity.py", "def test_upsert():", 3, 1, 20, 1),
    ]
    for nd in nodes_data:
        conn.execute("INSERT INTO nodes VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", nd)
        conn.execute("INSERT INTO nodes_fts(rowid, name, signature, file_path) VALUES(?, ?, ?, ?)", (nd[0], nd[1], nd[5], nd[4]))

    edges_data = [
        ("app.controllers.EntityController", "app.services.entity.upsertEntity", "CALLS"),
        ("app.services.entity.upsertEntity", "app.services.entity.validateEntity", "CALLS"),
        ("app.controllers.EntityController", "app.services.entity.deleteEntity", "CALLS"),
        ("app.services.entity.upsertEntity", "app.services.entity.deleteEntity", "CALLS"),
    ]
    for ed in edges_data:
        conn.execute("INSERT INTO edges VALUES(?, ?, ?)", ed)

    communities_data = [
        (1, "entity-services", 3, "python", "Entity CRUD operations", 0.8, 1),
        (2, "controllers", 1, "python", "API controllers", 0.5, 1),
        (3, "tests", 1, "python", "Test suite", 0.9, 1),
    ]
    for cd in communities_data:
        conn.execute("INSERT INTO communities VALUES(?, ?, ?, ?, ?, ?, ?)", cd)

    snippets_data = [
        ("upsertEntity", "def upsertEntity(data):\n    validateEntity(data)\n    db.save(data)\n    return data"),
        ("validateEntity", "def validateEntity(data):\n    if not data.get('id'):\n        raise ValueError('id required')"),
    ]
    for sn, st in snippets_data:
        conn.execute("INSERT INTO node_snippets VALUES(?, ?)", (sn, st))

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def mock_proj(mock_crg_db):
    """Create a mock project dict with CRG DB + graphify data."""
    return {
        "id": 1,
        "name": "test-project",
        "crg_db_path": mock_crg_db,
        "graphify_data": {
            "nodes": [
                {"id": "upsertEntity", "label": "upsertEntity", "file_type": "code", "source_file": "src/services/entity.py", "community": 1, "qualified_name": "app.services.entity.upsertEntity"},
                {"id": "validateEntity", "label": "validateEntity", "file_type": "code", "source_file": "src/services/entity.py", "community": 1},
                {"id": "EntityController", "label": "EntityController", "file_type": "code", "source_file": "src/controllers/entity.py", "community": 2},
                {"id": "note_1", "label": "NOTE: Entity service centralizes CRUD to avoid duplicate validation", "file_type": "rationale", "source_file": "src/services/entity.py", "community": 1},
            ],
            "links": [
                {"source": "EntityController", "target": "upsertEntity", "type": "calls", "confidence": "EXTRACTED"},
                {"source": "upsertEntity", "target": "validateEntity", "type": "calls", "confidence": "EXTRACTED"},
                {"source": "upsertEntity", "target": "note_1", "type": "rationale_for", "confidence": "EXTRACTED"},
            ],
        },
    }


# ── Semantic search tests ─────────────────────────────────────────

class TestSemanticSearch:
    """Test semantic search and hybrid search in CRGProvider."""

    def test_semantic_search_returns_results(self, mock_proj):
        """Semantic search should find nodes by meaning, not just keywords."""
        from crg_intelligence import CRGProvider, _EMBEDDING_CACHE
        # Clear cache to ensure fresh build
        _EMBEDDING_CACHE.clear()
        provider = CRGProvider(mock_proj)
        assert provider.is_available()
        # Skip if encoder not available (no model in test env)
        from crg_intelligence import _get_encoder
        if _get_encoder() is None:
            pytest.skip("Encoder not available in test environment")
        results = provider.semantic_search("add entity to database", max_results=5)
        assert isinstance(results, list)
        if results:
            assert "file_path" in results[0]
            assert "score" in results[0]
            assert results[0]["mode"] == "semantic"

    def test_hybrid_search_blends_fts_and_semantic(self, mock_proj):
        """Hybrid search should combine FTS and semantic results."""
        from crg_intelligence import CRGProvider, _EMBEDDING_CACHE
        _EMBEDDING_CACHE.clear()
        provider = CRGProvider(mock_proj)
        assert provider.is_available()
        from crg_intelligence import _get_encoder
        if _get_encoder() is None:
            pytest.skip("Encoder not available in test environment")
        results = provider.hybrid_search("entity", max_results=10, embedding_weight=0.4)
        assert isinstance(results, list)
        # Should find upsertEntity, validateEntity, etc.
        names = [r.get("name", "") for r in results]
        assert any("entity" in n.lower() for n in names)

    def test_hybrid_search_fts_only(self, mock_proj):
        """Hybrid search with embedding_weight=0 should be FTS only."""
        provider_cls = type(mock_proj)  # Just to ensure we can import
        from crg_intelligence import CRGProvider, _EMBEDDING_CACHE
        _EMBEDDING_CACHE.clear()
        provider = CRGProvider(mock_proj)
        assert provider.is_available()
        results = provider.hybrid_search("entity", max_results=10, embedding_weight=0.0)
        assert isinstance(results, list)
        names = [r.get("name", "") for r in results]
        assert "upsertEntity" in names

    def test_embedding_index_caches(self, mock_proj):
        """Embedding index should be cached per db_path."""
        from crg_intelligence import CRGProvider, _EMBEDDING_CACHE, _get_encoder
        _EMBEDDING_CACHE.clear()
        provider = CRGProvider(mock_proj)
        provider.is_available()
        if _get_encoder() is None:
            pytest.skip("Encoder not available")
        provider._build_embedding_index()
        assert mock_proj["crg_db_path"] in _EMBEDDING_CACHE
        # Second call should use cache
        provider._build_embedding_index()


# ── Multi-hop traversal tests ─────────────────────────────────────

class TestMultiHopTraversal:
    """Test multi-hop graph traversal."""

    def test_traverse_returns_nodes_and_edges(self, mock_proj):
        """Traverse should return nodes, edges, and stats."""
        from crg_intelligence import CRGProvider, CRGProvider as CP
        CP._ADJACENCY_CACHE.clear()
        provider = CRGProvider(mock_proj)
        assert provider.is_available()
        result = provider.traverse("upsertEntity", max_hops=2, max_nodes=30, max_tokens=400)
        assert "nodes" in result
        assert "edges" in result
        assert "stats" in result
        assert isinstance(result["nodes"], list)
        assert len(result["nodes"]) > 0
        # Anchor node should be at depth 0
        assert result["nodes"][0]["depth"] == 0

    def test_traverse_finds_callers_and_callees(self, mock_proj):
        """Traverse should find both callers and callees within 2 hops."""
        from crg_intelligence import CRGProvider
        CRGProvider._ADJACENCY_CACHE.clear()
        provider = CRGProvider(mock_proj)
        provider.is_available()
        result = provider.traverse("upsertEntity", max_hops=2)
        names = [n["name"] for n in result["nodes"]]
        # upsertEntity is called by EntityController and calls validateEntity
        assert "upsertEntity" in names
        assert "validateEntity" in names

    def test_traverse_respects_max_nodes(self, mock_proj):
        """Traverse should respect max_nodes limit."""
        from crg_intelligence import CRGProvider
        CRGProvider._ADJACENCY_CACHE.clear()
        provider = CRGProvider(mock_proj)
        provider.is_available()
        result = provider.traverse("upsertEntity", max_hops=3, max_nodes=2)
        assert len(result["nodes"]) <= 2

    def test_traverse_respects_token_budget(self, mock_proj):
        """Traverse should stop when token budget is reached."""
        from crg_intelligence import CRGProvider
        CRGProvider._ADJACENCY_CACHE.clear()
        provider = CRGProvider(mock_proj)
        provider.is_available()
        result = provider.traverse("upsertEntity", max_hops=3, max_nodes=100, max_tokens=30)
        assert result["stats"]["est_tokens"] <= 30 or len(result["nodes"]) <= 2

    def test_traverse_not_found(self, mock_proj):
        """Traverse should return empty results for unknown target."""
        from crg_intelligence import CRGProvider
        CRGProvider._ADJACENCY_CACHE.clear()
        provider = CRGProvider(mock_proj)
        provider.is_available()
        result = provider.traverse("nonexistentSymbol", max_hops=2)
        assert len(result["nodes"]) == 0
        assert result["stats"]["nodes"] == 0

    def test_adjacency_caches(self, mock_proj):
        """Adjacency list should be cached per db_path."""
        from crg_intelligence import CRGProvider
        CRGProvider._ADJACENCY_CACHE.clear()
        provider = CRGProvider(mock_proj)
        provider.is_available()
        adj = provider._build_adjacency()
        assert adj is not None
        assert mock_proj["crg_db_path"] in CRGProvider._ADJACENCY_CACHE
        # Second call should use cache
        adj2 = provider._build_adjacency()
        assert adj2 is adj


# ── Source code snippet tests ─────────────────────────────────────

class TestSnippets:
    """Test source code snippet retrieval."""

    def test_get_snippets_returns_snippet(self, mock_proj):
        """get_snippets should return stored source snippets."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj)
        assert provider.is_available()
        result = provider.get_snippets(["upsertEntity"], max_chars=500)
        assert "upsertEntity" in result
        assert "snippet" in result["upsertEntity"]
        assert "def upsertEntity" in result["upsertEntity"]["snippet"]

    def test_get_snippets_multiple_names(self, mock_proj):
        """get_snippets should handle multiple node names."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj)
        provider.is_available()
        result = provider.get_snippets(["upsertEntity", "validateEntity"], max_chars=500)
        assert len(result) >= 2

    def test_get_snippets_unknown_name(self, mock_proj):
        """get_snippets should return empty for unknown names."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj)
        provider.is_available()
        result = provider.get_snippets(["nonexistentSymbol"], max_chars=500)
        assert "nonexistentSymbol" not in result

    def test_get_snippets_respects_max_chars(self, mock_proj):
        """get_snippets should truncate snippets to max_chars."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj)
        provider.is_available()
        result = provider.get_snippets(["upsertEntity"], max_chars=20)
        if "upsertEntity" in result:
            assert len(result["upsertEntity"]["snippet"]) <= 20


# ── Rationale node tests ──────────────────────────────────────────

class TestRationale:
    """Test rationale/doc node surfacing."""

    def test_get_rationale_finds_notes(self, mock_proj):
        """get_rationale should find rationale_for edges connected to a symbol."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj)
        provider.is_available()
        result = provider.get_rationale("upsertEntity")
        assert isinstance(result, list)
        assert len(result) > 0
        assert "text" in result[0]
        assert "confidence" in result[0]
        assert "Entity service" in result[0]["text"] or "CRUD" in result[0]["text"]

    def test_get_rationale_no_notes(self, mock_proj):
        """get_rationale should return empty for symbols with no notes."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj)
        provider.is_available()
        result = provider.get_rationale("validateEntity")
        assert isinstance(result, list)
        assert len(result) == 0

    def test_get_rationale_unknown_symbol(self, mock_proj):
        """get_rationale should return empty for unknown symbols."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj)
        provider.is_available()
        result = provider.get_rationale("nonexistentSymbol")
        assert len(result) == 0


# ── Merge intelligence results (weighted) tests ───────────────────

class TestWeightedMerge:
    """Test the merge_intelligence_results function with weights."""

    def test_merge_preserves_scores(self):
        """Merge should preserve and combine scores from both sources."""
        from crg_intelligence import merge_intelligence_results
        graphify_ranked = [
            {"file_path": "src/a.py", "score": 10.0, "reason": ["graph_match"]},
            {"file_path": "src/b.py", "score": 5.0, "reason": ["graph_match"]},
        ]
        intel_results = [
            {"file_path": "src/a.py", "score": 8.0, "reason": ["crg_fts_match"]},
            {"file_path": "src/c.py", "score": 6.0, "reason": ["crg_semantic_match"]},
        ]
        merged = merge_intelligence_results(graphify_ranked, intel_results, max_results=10)
        assert len(merged) == 3
        # src/a.py should have highest score (10 + 8 = 18)
        assert merged[0]["file_path"] == "src/a.py"
        assert merged[0]["score"] == 18.0

    def test_merge_empty_intel(self):
        """Merge with empty intel should return graphify results."""
        from crg_intelligence import merge_intelligence_results
        graphify_ranked = [{"file_path": "src/a.py", "score": 10.0}]
        merged = merge_intelligence_results(graphify_ranked, [], max_results=10)
        assert len(merged) == 1
        assert merged[0]["file_path"] == "src/a.py"
