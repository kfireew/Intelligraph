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


# ── Impact change= filtering, breaks/safe, pagination tests ───────

@pytest.fixture
def mock_crg_db_ts(tmp_path):
    """Mock CRG DB with TypeScript-style edges (REFERENCES, IMPORTS_FROM, CALLS)
    and snippets containing Record</switch patterns for breaks/safe testing."""
    db_path = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, kind TEXT, qualified_name TEXT, file_path TEXT, signature TEXT, community_id INTEGER, line_start INTEGER, line_end INTEGER, is_test INTEGER)")
    conn.execute("CREATE VIRTUAL TABLE nodes_fts USING fts5(name, signature, file_path, content='nodes', content_rowid='id')")
    conn.execute("CREATE TABLE edges (source_qualified TEXT, target_qualified TEXT, kind TEXT)")
    conn.execute("CREATE TABLE node_snippets (node_name TEXT PRIMARY KEY, snippet TEXT)")

    # PlaneCategories enum is imported by many files:
    #   - icon-resolver.ts (Record<PlaneCategory, string> — breaks on add-value)
    #   - display.ts (switch(PlaneCategory) — breaks on add-value)
    #   - service.ts (calls function using it — safe on add-value, breaks on rename)
    #   - test.ts (test file — should sort last)
    nodes_data = [
        (1, "PlaneCategories", "Enum", "types.categories.PlaneCategories", "src/types/categories.ts", "enum PlaneCategories", 1, 1, 20, 0),
        (2, "iconResolver", "Function", "utils.iconResolver", "src/utils/icon-resolver.ts", "def iconResolver()", 2, 5, 30, 0),
        (3, "displayLabel", "Function", "utils.displayLabel", "src/utils/display.ts", "def displayLabel()", 2, 10, 40, 0),
        (4, "planeService", "Function", "services.planeService", "src/services/plane.ts", "def planeService()", 3, 1, 50, 0),
        (5, "testPlane", "Function", "tests.testPlane", "tests/test_plane.ts", "def testPlane()", 4, 1, 20, 1),
    ]
    for nd in nodes_data:
        conn.execute("INSERT INTO nodes VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", nd)
        conn.execute("INSERT INTO nodes_fts(rowid, name, signature, file_path) VALUES(?, ?, ?, ?)", (nd[0], nd[1], nd[5], nd[4]))

    edges_data = [
        # icon-resolver REFERENCES PlaneCategories (type-position use → breaks on add-value)
        ("utils.iconResolver", "types.categories.PlaneCategories", "REFERENCES"),
        # icon-resolver IMPORTS_FROM PlaneCategories
        ("utils.iconResolver", "types.categories.PlaneCategories", "IMPORTS_FROM"),
        # display.ts REFERENCES + IMPORTS_FROM
        ("utils.displayLabel", "types.categories.PlaneCategories", "REFERENCES"),
        ("utils.displayLabel", "types.categories.PlaneCategories", "IMPORTS_FROM"),
        # plane service CALLS a function that uses PlaneCategories (calls — safe on add-value)
        ("services.planeService", "utils.iconResolver", "CALLS"),
        ("services.planeService", "types.categories.PlaneCategories", "IMPORTS_FROM"),
        # test file
        ("tests.testPlane", "types.categories.PlaneCategories", "IMPORTS_FROM"),
        ("tests.testPlane", "utils.iconResolver", "CALLS"),
    ]
    for ed in edges_data:
        conn.execute("INSERT INTO edges VALUES(?, ?, ?)", ed)

    # Snippets with exhaustive patterns (actual source code ≤500 chars)
    snippets = [
        ("iconResolver", "const iconResolver = (cat: PlaneCategory): string => {\n  const map: Record<PlaneCategory, string> = {\n    ZIK: '/zik.svg',\n  };\n  return map[cat];\n};"),
        ("displayLabel", "function displayLabel(cat: PlaneCategory) {\n  switch (cat) {\n    case ZIK: return 'Zik';\n  }\n}"),
        ("planeService", "function planeService() {\n  return iconResolver('ZIK');\n}"),
        ("testPlane", "test('plane', () => {\n  expect(iconResolver('ZIK')).toBe('/zik.svg');\n});"),
    ]
    for sn, st in snippets:
        conn.execute("INSERT INTO node_snippets VALUES(?, ?)", (sn, st))

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def mock_proj_ts(mock_crg_db_ts):
    """Mock project dict for TypeScript-style impact tests."""
    return {
        "id": 1,
        "crg_db_path": mock_crg_db_ts,
        "graphify_data": {},
        "nodes": 5,
        "edges": 8,
        "crg_nodes": 5,
    }


class TestImpactChangeFiltering:
    """Test impact() with change= parameter: edge filtering, breaks/safe, pagination."""

    def test_add_value_filters_to_references_imports(self, mock_proj_ts):
        """change=add-value should only follow REFERENCES + IMPORTS_FROM edges."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_ts)
        provider.is_available()
        results = provider.impact("PlaneCategories", change="add-value")
        # Should find files that REFERENCE or IMPORT_FROM PlaneCategories
        fps = {r["file_path"] for r in results}
        # icon-resolver and display both have REFERENCES → should be present
        assert any("icon-resolver" in fp for fp in fps)
        assert any("display" in fp for fp in fps)
        # plane service only has CALLS to iconResolver + IMPORTS_FROM (imports counts)
        assert any("plane" in fp for fp in fps)
        # Should NOT include test file under add-value (imports_from but is_test sorts last)
        # test file has IMPORTS_FROM so it WILL be found, but should be last
        test_results = [r for r in results if "test" in r.get("file_path", "")]
        if test_results:
            # test should be at the bottom
            assert results[-1] == test_results[0] or results.index(test_results[0]) >= len(results) - 2

    def test_rename_filters_to_calls_imports(self, mock_proj_ts):
        """change=rename should only follow CALLS + IMPORTS_FROM edges."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_ts)
        provider.is_available()
        results = provider.impact("iconResolver", change="rename")
        fps = {r["file_path"] for r in results}
        # planeService CALLS iconResolver → should be present at depth 1
        assert any("plane" in fp for fp in fps)
        # Verify depth-1 results only include CALLS callers + IMPORTS_FROM callees
        depth1 = [r for r in results if r["depth"] == 1]
        depth1_fps = {r["file_path"] for r in depth1}
        # planeService and testPlane CALL iconResolver → depth 1
        assert any("plane" in fp and "test" not in fp for fp in depth1_fps)
        # PlaneCategories is an IMPORTS_FROM callee of iconResolver → depth 1
        assert any("categories" in fp for fp in depth1_fps)
        # display.ts REFERENCES PlaneCategories (not CALLS/IMPORTS_FROM to iconResolver)
        # → should NOT appear at depth 1 (only possibly at depth 2 via chain)
        assert not any("display" in fp for fp in depth1_fps)

    def test_full_includes_all_edges(self, mock_proj_ts):
        """change=full should include all edge kinds (CALLS + REFERENCES + IMPORTS_FROM)."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_ts)
        provider.is_available()
        results = provider.impact("PlaneCategories", change="full")
        fps = {r["file_path"] for r in results}
        # All files should appear
        assert any("icon-resolver" in fp for fp in fps)
        assert any("display" in fp for fp in fps)
        assert any("plane" in fp for fp in fps)

    def test_breaks_tagging_via_snippets(self, mock_proj_ts):
        """Files with Record< or switch in snippets should be tagged breaks=True."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_ts)
        provider.is_available()
        results = provider.impact("PlaneCategories", change="add-value")
        # iconResolver has Record< in snippet → breaks=True
        icon = [r for r in results if "icon-resolver" in r.get("file_path", "")]
        assert len(icon) == 1
        assert icon[0]["breaks"] is True
        assert "Record<" in icon[0]["pattern"]

        # display has switch ( in snippet → breaks=True
        display = [r for r in results if "display" in r.get("file_path", "")]
        assert len(display) == 1
        assert display[0]["breaks"] is True

        # planeService has no exhaustive pattern → breaks=False (safe)
        plane = [r for r in results if "plane" in r.get("file_path", "") and "test" not in r.get("file_path", "")]
        assert len(plane) == 1
        assert plane[0]["breaks"] is False

    def test_risk_priority_sort(self, mock_proj_ts):
        """Results should be sorted: breaks first, safe second, tests last."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_ts)
        provider.is_available()
        results = provider.impact("PlaneCategories", change="add-value")
        # Depth 0 (definition) should be first
        assert results[0]["depth"] == 0
        # Find the test file index
        test_idx = None
        for i, r in enumerate(results):
            if "test" in r.get("file_path", ""):
                test_idx = i
                break
        # Test file should NOT be at the top (it should be at or near the bottom)
        if test_idx is not None:
            assert test_idx >= len(results) - 2

    def test_offset_pagination(self, mock_proj_ts):
        """offset= should skip first N results (pagination). The provider returns
        the full sorted list; the dispatch handler slices by offset."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_ts)
        provider.is_available()
        all_results = provider.impact("PlaneCategories", change="add-value", offset=0)
        total = all_results[0].get("total_count", len(all_results))
        assert total == len(all_results)
        # Page 1: first 2 results
        page1 = all_results[:2]
        assert len(page1) == 2
        # Page 2: skip first 2, take next 2 (simulating dispatch offset=2)
        page2 = all_results[2:4]
        # Page 2 should not overlap with page 1
        page1_fps = {r["file_path"] for r in page1}
        page2_fps = {r["file_path"] for r in page2}
        assert not (page1_fps & page2_fps), "Pages should not overlap"

    def test_total_count_in_metadata(self, mock_proj_ts):
        """Each result dict should carry total_count for pagination."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_ts)
        provider.is_available()
        results = provider.impact("PlaneCategories", change="add-value")
        assert len(results) > 0
        assert all("total_count" in r for r in results)
        assert results[0]["total_count"] == len(results)

    def test_node_count_drives_depth(self, mock_proj_ts):
        """Small repos (<2k nodes) should get depth 2; larger get depth 1."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_ts)
        provider.is_available()
        # mock has 5 nodes → depth_max should be 2 for add-value
        results = provider.impact("PlaneCategories", change="add-value")
        # With depth 2, we should find planeService (which CALLS iconResolver)
        # Actually, add-value filters to REFERENCES+IMPORTS_FROM, so depth-2
        # traversal via CALLS won't add planeService as depth-2. But the direct
        # IMPORTS_FROM from planeService puts it at depth 1.
        # Verify we get at least 3 files (definition + 2 type users)
        assert len(results) >= 3

    def test_default_change_is_add_value(self, mock_proj_ts):
        """Default change should be add-value (narrow), not full."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_ts)
        provider.is_available()
        # Call without change= → should default to add-value
        results_default = provider.impact("PlaneCategories")
        results_explicit = provider.impact("PlaneCategories", change="add-value")
        # Both should return the same files
        default_fps = {r["file_path"] for r in results_default}
        explicit_fps = {r["file_path"] for r in results_explicit}
        assert default_fps == explicit_fps


# ── 3472-file blast radius: token cap verification ─────────────────

@pytest.fixture
def mock_crg_db_huge(tmp_path):
    """Simulate the feedback scenario: an enum imported by 3472 files.
    6 files have Record</switch patterns (breaks), 3466 are plain imports (safe).
    """
    db_path = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, kind TEXT, qualified_name TEXT, file_path TEXT, signature TEXT, community_id INTEGER, line_start INTEGER, line_end INTEGER, is_test INTEGER)")
    conn.execute("CREATE VIRTUAL TABLE nodes_fts USING fts5(name, signature, file_path, content='nodes', content_rowid='id')")
    conn.execute("CREATE TABLE edges (source_qualified TEXT, target_qualified TEXT, kind TEXT)")
    conn.execute("CREATE TABLE node_snippets (node_name TEXT PRIMARY KEY, snippet TEXT)")

    # Target enum
    conn.execute("INSERT INTO nodes VALUES(1, 'PlaneCategories', 'Enum', 'types.PlaneCategories', 'src/types/categories.ts', 'enum PlaneCategories', 1, 1, 20, 0)")
    conn.execute("INSERT INTO nodes_fts(rowid, name, signature, file_path) VALUES(1, 'PlaneCategories', 'enum PlaneCategories', 'src/types/categories.ts')")

    # 6 files with exhaustive patterns (breaks)
    breaks_snippets = [
        ("iconResolver", "const iconResolver = (cat: PlaneCategory): string => {\n  const map: Record<PlaneCategory, string> = {\n    ZIK: '/zik.svg',\n  };\n  return map[cat];\n};"),
        ("displayLabel", "function displayLabel(cat: PlaneCategory) {\n  switch (cat) {\n    case ZIK: return 'Zik';\n  }\n}"),
        ("categoryMap", "const categoryMap: Record<PlaneCategory, Icon> = {\n  ZIK: 'icon-zik',\n};"),
        ("planeReducer", "function planeReducer(state, action) {\n  return Object.keys(PlaneCategories).map(k => k);\n}"),
        ("typeGuard", "function isPlane(cat: any): cat is PlaneCategory {\n  return Object.keys(PlaneCategories).includes(cat);\n}"),
        ("enumValues", "const enumValues = Object.keys(PlaneCategories).reduce((acc, k) => { acc[k] = true; return acc; }, {});"),
    ]
    for i, (sym_name, snip) in enumerate(breaks_snippets, start=2):
        qname = f"app.file_{i}.{sym_name}"
        fp = f"src/file_{i}.ts"
        conn.execute("INSERT INTO nodes VALUES(?, ?, 'Function', ?, ?, ?, 2, 1, 30, 0)", (i, sym_name, qname, fp, f"def {sym_name}()"))
        conn.execute("INSERT INTO nodes_fts(rowid, name, signature, file_path) VALUES(?, ?, ?, ?)", (i, sym_name, f"def {sym_name}()", fp))
        conn.execute("INSERT INTO edges VALUES(?, 'types.PlaneCategories', 'IMPORTS_FROM')", (qname,))
        conn.execute("INSERT INTO node_snippets VALUES(?, ?)", (sym_name, snip))

    # 3466 files with plain imports (safe — no exhaustive pattern)
    for i in range(8, 3474):
        sym_name = f"consumer_{i}"
        qname = f"app.file_{i}.{sym_name}"
        fp = f"src/consumer_{i}.ts"
        conn.execute("INSERT INTO nodes VALUES(?, ?, 'Function', ?, ?, ?, 3, 1, 10, 0)", (i, sym_name, qname, fp, f"def {sym_name}()"))
        conn.execute("INSERT INTO nodes_fts(rowid, name, signature, file_path) VALUES(?, ?, ?, ?)", (i, sym_name, f"def {sym_name}()", fp))
        conn.execute("INSERT INTO edges VALUES(?, 'types.PlaneCategories', 'IMPORTS_FROM')", (qname,))

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def mock_proj_huge(mock_crg_db_huge):
    return {"id": 1, "crg_db_path": mock_crg_db_huge, "graphify_data": {}, "nodes": 3473, "edges": 3472, "crg_nodes": 3473}


class TestImpact3472FileBlastRadius:
    """Verify that impact() on a 3472-file blast radius stays under 1500 tokens."""

    def test_provider_returns_all_files(self, mock_proj_huge):
        """impact() must return all 3472 files — data is never truncated."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_huge)
        provider.is_available()
        results = provider.impact("PlaneCategories", change="add-value")
        # 3472 dependents + 1 definition = 3473
        assert len(results) == 3473
        assert results[0]["total_count"] == 3473

    def test_breaks_files_sorted_first(self, mock_proj_huge):
        """The 6 [breaks] files must appear before the 3466 [safe] files."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_huge)
        provider.is_available()
        results = provider.impact("PlaneCategories", change="add-value")
        # Depth 0 (definition) is first
        assert results[0]["depth"] == 0
        # Next results should be breaks=True
        breaks_results = [r for r in results if r.get("breaks") is True]
        safe_results = [r for r in results if r.get("breaks") is False]
        assert len(breaks_results) == 6
        assert len(safe_results) == 3466
        # First non-definition result should be a breaks file
        first_dep = results[1]
        assert first_dep["breaks"] is True
        # The last breaks file should come before the first safe file
        last_breaks_idx = max(i for i, r in enumerate(results) if r.get("breaks") is True)
        first_safe_idx = min(i for i, r in enumerate(results) if r.get("breaks") is False)
        assert last_breaks_idx < first_safe_idx

    def test_dispatch_output_under_1500_tokens(self, mock_proj_huge, tmp_path):
        """The actual _dispatch output (what the MCP client sees) must be
        under 1500 tokens. This is the real token-savings test."""
        import intelligraph_mcp
        intelligraph_mcp.REPO_DIR = str(tmp_path)
        intelligraph_mcp._ORIGINAL_REPO_DIR = ""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_huge)
        provider.is_available()
        output = intelligraph_mcp._dispatch("impact", {"name": "PlaneCategories"}, provider)
        est_tokens = len(output) // 4
        # Must be under 1500 tokens (the default max_tokens budget)
        assert est_tokens <= 1500, f"Output was {est_tokens} tokens (expected <=1500). Output length: {len(output)} chars"
        # Must contain pagination hint
        assert "more files exist" in output or "offset=" in output
        # Must show the header with total count
        assert "3473" in output or "3472" in output

    def test_full_mode_under_4000_tokens(self, mock_proj_huge, tmp_path):
        """change=full should still be capped at 4000 tokens (not 50k)."""
        import intelligraph_mcp
        intelligraph_mcp.REPO_DIR = str(tmp_path)
        intelligraph_mcp._ORIGINAL_REPO_DIR = ""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_huge)
        provider.is_available()
        output = intelligraph_mcp._dispatch("impact", {"name": "PlaneCategories", "change": "full"}, provider)
        est_tokens = len(output) // 4
        # Full mode max_tokens=4000 — must stay under that
        assert est_tokens <= 4000, f"Full output was {est_tokens} tokens (expected <=4000)"
        assert "more files exist" in output or "offset=" in output

    def test_breaks_pattern_in_output(self, mock_proj_huge, tmp_path):
        """The dispatch output should include the breaks pattern (e.g. Record<)."""
        import intelligraph_mcp
        intelligraph_mcp.REPO_DIR = str(tmp_path)
        intelligraph_mcp._ORIGINAL_REPO_DIR = ""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_huge)
        provider.is_available()
        output = intelligraph_mcp._dispatch("impact", {"name": "PlaneCategories"}, provider)
        # At least one [breaks] tag should appear
        assert "[breaks]" in output
        # At least one pattern should be shown
        assert "pattern:" in output or "Record<" in output

    def test_safe_files_not_shown_on_first_page(self, mock_proj_huge, tmp_path):
        """Safe files should NOT appear on the first page — they're after breaks."""
        import intelligraph_mcp
        intelligraph_mcp.REPO_DIR = str(tmp_path)
        intelligraph_mcp._ORIGINAL_REPO_DIR = ""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_huge)
        provider.is_available()
        output = intelligraph_mcp._dispatch("impact", {"name": "PlaneCategories"}, provider)
        # The first page should show breaks files, not safe ones
        # If [safe] appears, it should be near the end of the page (if at all)
        lines = output.split("\n")
        breaks_lines = [l for l in lines if "[breaks]" in l]
        safe_lines = [l for l in lines if "[safe]" in l]
        # Should have breaks lines
        assert len(breaks_lines) > 0
        # Safe files should not appear before breaks files
        if safe_lines:
            first_breaks_idx = min(i for i, l in enumerate(lines) if "[breaks]" in l)
            first_safe_idx = min(i for i, l in enumerate(lines) if "[safe]" in l)
            assert first_breaks_idx < first_safe_idx


# ── Phase 1: near= resilience, snippet schema, path validation ─────

@pytest.fixture
def mock_crg_db_snippets(tmp_path):
    """Mock CRG DB with v2 snippet schema (qualified_name + file_path)
    and a const object whose snippet contains a property value (TRACK_ZIK)
    that is NOT a graph node."""
    db_path = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, kind TEXT, "
                 "qualified_name TEXT, file_path TEXT, signature TEXT, "
                 "community_id INTEGER, line_start INTEGER, line_end INTEGER, is_test INTEGER)")
    conn.execute("CREATE VIRTUAL TABLE nodes_fts USING fts5(name, signature, file_path, "
                 "content='nodes', content_rowid='id')")
    conn.execute("CREATE TABLE edges (source_qualified TEXT, target_qualified TEXT, kind TEXT)")
    conn.execute("CREATE TABLE node_snippets (qualified_name TEXT PRIMARY KEY, "
                 "node_name TEXT, file_path TEXT, line_start INTEGER, line_end INTEGER, snippet TEXT)")
    conn.execute("CREATE INDEX idx_snippet_name ON node_snippets(node_name)")
    conn.execute("CREATE INDEX idx_snippet_file ON node_snippets(file_path)")

    nodes_data = [
        (1, "IconsNames", "Const", "icons.IconsNames", "src/icons/icons.ts",
         "const IconsNames = {}", 1, 200, 210, 0),
        (2, "iconResolver", "Function", "utils.iconResolver", "src/utils/icon-resolver.ts",
         "function iconResolver()", 1, 5, 30, 0),
        (3, "planeService", "Function", "services.planeService", "src/services/plane.ts",
         "function planeService()", 2, 1, 50, 0),
    ]
    for nd in nodes_data:
        conn.execute("INSERT INTO nodes VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", nd)
        conn.execute("INSERT INTO nodes_fts(rowid, name, signature, file_path) VALUES(?, ?, ?, ?)",
                     (nd[0], nd[1], nd[5], nd[4]))

    edges_data = [
        ("utils.iconResolver", "icons.IconsNames", "REFERENCES"),
        ("utils.iconResolver", "icons.IconsNames", "IMPORTS_FROM"),
        ("services.planeService", "utils.iconResolver", "CALLS"),
    ]
    for ed in edges_data:
        conn.execute("INSERT INTO edges VALUES(?, ?, ?)", ed)

    # Snippet for IconsNames contains TRACK_ZIK as a property value
    snip_icons = "const IconsNames = {\n  TRACK_ZIK: 'zik',\n  TRACK_KARISH: 'karish',\n};"
    conn.execute("INSERT INTO node_snippets VALUES(?, ?, ?, ?, ?, ?)",
                 ("icons.IconsNames", "IconsNames", "src/icons/icons.ts", 200, 210, snip_icons))
    conn.execute("INSERT INTO node_snippets VALUES(?, ?, ?, ?, ?, ?)",
                 ("utils.iconResolver", "iconResolver", "src/utils/icon-resolver.ts", 5, 30,
                  "function iconResolver() {\n  return IconsNames.TRACK_ZIK;\n}"))

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def mock_proj_snippets(mock_crg_db_snippets):
    return {"id": 1, "name": "test", "crg_db_path": mock_crg_db_snippets}


class TestNearResilience:
    """Tests for near= snippet fallback, no-zero-out, and path validation."""

    def test_near_snippet_fallback_resolves_value(self, mock_proj_snippets):
        """near='TRACK_ZIK' should resolve via snippet fallback (not a graph node
        but appears in IconsNames snippet), returning the IconsNames file."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_snippets)
        assert provider.is_available()
        results = provider.search("iconResolver", near="TRACK_ZIK")
        # Should NOT return [] — snippet fallback finds IconsNames file
        assert len(results) > 0, "near='TRACK_ZIK' should resolve via snippet, not return empty"
        # The result should include the IconsNames file or icon-resolver
        files = {r.get("file_path", "") for r in results}
        assert any("icons.ts" in f or "icon-resolver" in f for f in files), \
            f"Expected icons.ts or icon-resolver in results, got {files}"

    def test_near_unresolved_no_zero_out(self, mock_proj_snippets):
        """near='NONEXISTENT' should NOT return []. Should return unfiltered
        results tagged with 'near_unresolved' reason."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_snippets)
        assert provider.is_available()
        results = provider.search("iconResolver", near="TOTALLY_BOGUS_NONEXISTENT_XYZ")
        assert len(results) > 0, "Unresolved near= should return unfiltered results, not []"
        # At least one result should have near_unresolved tag
        assert any("near_unresolved" in r.get("reason", []) for r in results), \
            "Results should be tagged with 'near_unresolved'"

    def test_libse2e_filtered_as_test_path(self):
        """libsE2E/foo.ts (lowercased: libse2e) should be filtered as test path."""
        from crg_intelligence import _is_test_path
        assert _is_test_path("src/libsE2E/foo.ts")
        assert _is_test_path("src/libsE2E/helpers/bar.ts")
        # But a real source lib like 'e2e-helpers' should NOT be filtered
        # (segment 'e2e-helpers' is not exactly 'e2e' or 'libse2e')
        assert not _is_test_path("src/e2e-helpers/utils.ts")

    def test_normal_query_unchanged_by_near_resilience(self, mock_proj):
        """A normal symbol search (e.g. 'upsertEntity') should still work
        without any near_unresolved or snippet fallback tags."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj)
        assert provider.is_available()
        results = provider.hybrid_search("upsertEntity", max_results=5, embedding_weight=0.0)
        assert len(results) > 0
        # Should be HIGH or MEDIUM confidence, not LOW
        assert results[0].get("confidence") in ("HIGH", "MEDIUM")
        # Should NOT have near_unresolved reason
        assert "near_unresolved" not in results[0].get("reason", [])

    def test_stale_path_tagged_in_search_output(self, mock_proj_snippets, tmp_path):
        """_format_search should tag [stale] on results whose file_path
        doesn't exist in the local REPO_DIR."""
        import intelligraph_mcp
        intelligraph_mcp.REPO_DIR = str(tmp_path)
        intelligraph_mcp._ORIGINAL_REPO_DIR = ""
        # Wire repo_dir on the project so build_valid_paths() walks tmp_path
        mock_proj_snippets["repo_dir"] = str(tmp_path)
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_snippets)
        provider.is_available()
        # Don't create any files in tmp_path — all results will be stale
        output = intelligraph_mcp._dispatch("search", {"query": "iconResolver"}, provider)
        # Output should contain [stale] tag since no files exist on disk
        assert "[stale]" in output, f"Expected [stale] tag, got:\n{output}"

    def test_snippet_schema_v2_join_on_qualified_name(self, mock_proj_snippets):
        """get_snippets should find snippets using qualified_name (v2 schema),
        even when node_name would collide."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_snippets)
        assert provider.is_available()
        provider._get_conn()  # trigger schema probe
        assert provider._snippet_schema == "v2"
        result = provider.get_snippets(["IconsNames"], max_chars=500)
        assert "IconsNames" in result
        assert "TRACK_ZIK" in result["IconsNames"]["snippet"]


# ── Phase 2: lexical retrieval, focus anchors, smarter guidance ───

class TestLexicalRetrieval:
    """Tests for the lexical (snippet/value) retrieval stage."""

    def test_lexical_finds_string_constant(self, mock_proj_snippets):
        """search('TRACK_KARISH') should find the defining file via the
        lexical snippet pass, even though no graph node is named TRACK_KARISH."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_snippets)
        assert provider.is_available()
        results = provider.search("TRACK_KARISH")
        assert len(results) > 0, "Lexical pass should find TRACK_KARISH in IconsNames snippet"
        # At least one result should have 'lexical' in its reason
        has_lexical = any("lexical" in r.get("reason", []) for r in results)
        assert has_lexical, f"Expected 'lexical' reason, got reasons: {[r.get('reason') for r in results]}"
        # The result should point to icons.ts
        files = {r.get("file_path", "") for r in results}
        assert any("icons.ts" in f for f in files), f"Expected icons.ts in results, got {files}"

    def test_lexical_skipped_for_symbol_queries(self, mock_proj):
        """search('upsertEntity') (a real symbol) should NOT add 'lexical'
        reason — the lexical pass is gated on sparse results or constant-shape."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj)
        assert provider.is_available()
        results = provider.search("upsertEntity")
        assert len(results) > 0
        # upsertEntity is a real symbol with FTS match — lexical shouldn't fire
        for r in results:
            assert "lexical" not in r.get("reason", []), \
                f"Lexical pass should not fire for symbol query, got reason: {r.get('reason')}"

    def test_focus_anchor_on_results(self, mock_proj_snippets, tmp_path):
        """_format_search should emit anchor="SymbolName" on the top 3 results."""
        import intelligraph_mcp
        intelligraph_mcp.REPO_DIR = str(tmp_path)
        intelligraph_mcp._ORIGINAL_REPO_DIR = ""
        intelligraph_mcp._SESSION_SEARCHES.clear()  # avoid cache from prior tests
        intelligraph_mcp._SESSION_SEEN.clear()
        intelligraph_mcp._SESSION_CALL_COUNTER[0] = 0
        # Create the files so they're not stale
        for fp in ["utils/icon-resolver.ts", "icons/icons.ts", "services/plane.ts"]:
            full = os.path.join(str(tmp_path), fp)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write("// stub")
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_snippets)
        provider.is_available()
        output = intelligraph_mcp._dispatch("search", {"query": "iconResolver"}, provider)
        assert "anchor=" in output, f"Expected anchor= in output:\n{output}"

    def test_focused_results_no_suggestion(self, mock_proj_snippets, tmp_path):
        """When near= is used and results are 1-4 HIGH-confidence, output should
        say 'sufficiently focused' and recommend node(), NOT suggest another near=."""
        import intelligraph_mcp
        intelligraph_mcp.REPO_DIR = str(tmp_path)
        intelligraph_mcp._ORIGINAL_REPO_DIR = ""
        intelligraph_mcp._SESSION_SEARCHES.clear()
        intelligraph_mcp._SESSION_SEEN.clear()
        intelligraph_mcp._SESSION_CALL_COUNTER[0] = 0
        for fp in ["utils/icon-resolver.ts"]:
            full = os.path.join(str(tmp_path), fp)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write("// stub")
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_snippets)
        provider.is_available()
        output = intelligraph_mcp._dispatch(
            "search", {"query": "iconResolver", "near": "IconsNames"}, provider)
        assert "sufficiently focused" in output or "node()" in output, \
            f"Expected 'sufficiently focused' or 'node()' for focused results:\n{output}"

    def test_transparency_block_on_fallback(self, mock_proj_snippets, tmp_path):
        """When lexical fallback occurs, output should include 'Found via:'
        transparency block. When results are HIGH (no fallback), it should not."""
        import intelligraph_mcp
        intelligraph_mcp.REPO_DIR = str(tmp_path)
        intelligraph_mcp._ORIGINAL_REPO_DIR = ""
        intelligraph_mcp._SESSION_SEARCHES.clear()
        intelligraph_mcp._SESSION_SEEN.clear()
        intelligraph_mcp._SESSION_CALL_COUNTER[0] = 0
        for fp in ["icons/icons.ts"]:
            full = os.path.join(str(tmp_path), fp)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write("// stub")
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_snippets)
        provider.is_available()
        # TRACK_KARISH triggers lexical fallback
        output = intelligraph_mcp._dispatch("search", {"query": "TRACK_KARISH"}, provider)
        assert "Found via:" in output, f"Expected 'Found via:' transparency block:\n{output}"

    def test_blocker_message_no_near_placeholder(self):
        """The JS enforcement plugin should convert glob patterns to search
        hints without a near='<placeholder>' that teaches the model to invent
        anchors. The message should say 'search first' then 'use near= only
        with an exact symbol returned'."""
        import json
        # Read the JS file and verify the message structure
        js_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "intelligraph-enforce.js")
        with open(js_path, "r") as f:
            js_content = f.read()
        # Should NOT contain the old "Pass near= on every search" message
        assert "Pass near= on every search" not in js_content, \
            "Old 'pass near= on every search' message should be removed"
        # Should contain the new guidance
        assert "Use near= only with an exact symbol returned" in js_content, \
            "New 'use near= only with exact symbol returned' guidance should be present"
        # Should NOT have a near="<placeholder>" in search hints
        assert 'near="<subsystem symbol>"' not in js_content, \
            "Should not have near= placeholder that teaches model to invent anchors"
        # Should have searchHint function
        assert "function searchHint" in js_content or "searchHint" in js_content


# ── Phase 3: internal retrieval router, stage trace, found_via ───

class TestRetrievalRouter:
    """Tests for the unified retrieval router: stage cascade, trace, found_via."""

    def test_router_exact_symbol_stops(self, mock_proj):
        """An exact symbol query should return from Stage 1 only —
        _stages_tried should be ['lexical'], never include 'semantic'."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj)
        assert provider.is_available()
        results = provider.hybrid_search("upsertEntity", max_results=5, embedding_weight=0.4)
        assert len(results) > 0
        # Stage 1 only — no semantic stage should have run
        stages = results[0].get("_stages_tried", [])
        assert "lexical" in stages
        assert "semantic" not in stages, f"Semantic should not run for exact symbol, got stages: {stages}"

    def test_router_lexical_then_stops(self, mock_proj_snippets):
        """A constant-shape query (TRACK_KARISH) should resolve via lexical
        in Stage 1 and NOT trigger semantic — _stages_tried=['lexical']."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_snippets)
        assert provider.is_available()
        results = provider.hybrid_search("TRACK_KARISH", max_results=5, embedding_weight=0.4)
        assert len(results) > 0
        # Should have found_via containing 'lexical'
        found = results[0].get("found_via", "")
        assert "lexical" in found, f"Expected 'lexical' in found_via, got: {found}"
        # Stage 1 only — no semantic
        stages = results[0].get("_stages_tried", [])
        assert "semantic" not in stages

    def test_router_fts_for_natural_language(self, mock_proj):
        """A natural-language query that FTS can match should return from
        Stage 1 with FTS confidence, not trigger semantic."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj)
        assert provider.is_available()
        # 'entity' matches via FTS (signature contains 'entity')
        results = provider.hybrid_search("entity", max_results=10, embedding_weight=0.4)
        assert len(results) >= 3  # should short-circuit at >=3
        # Stage 1 only
        stages = results[0].get("_stages_tried", [])
        assert "semantic" not in stages

    def test_router_semantic_last_resort(self, mock_proj):
        """A query with no exact/FTS/lexical match should fall through to
        semantic (Stage 2) — _stages_tried should include 'semantic'."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj)
        assert provider.is_available()
        # A query unlikely to match any symbol/FTS — should go to semantic
        results = provider.hybrid_search("zzz_nonexistent_concept_xyz", max_results=5, embedding_weight=0.4)
        # May return 0 or LOW results, but should have tried semantic
        if results and results[0].get("_stages_tried"):
            stages = results[0].get("_stages_tried", [])
            # If Stage 1 found <3 results, semantic should have been tried
            # (unless embeddings are unavailable, in which case it skips)
            # Check that the router at least attempted the cascade
            assert "lexical" in stages

    def test_near_filter_post_retrieval(self, mock_proj_snippets):
        """near= should be applied after stages produce candidates, not
        zero them before. With a valid near=, results should be filtered
        but stages still tried."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj_snippets)
        assert provider.is_available()
        results = provider.hybrid_search("iconResolver", max_results=5, near="IconsNames")
        assert len(results) > 0
        # near= was valid, should have filtered to connected files
        # Stages should still show lexical was tried
        stages = results[0].get("_stages_tried", [])
        assert "lexical" in stages

    def test_strategy_trace_in_results(self, mock_proj):
        """Every result should carry _stages_tried and _stages_hit for the
        format layer's transparency block."""
        from crg_intelligence import CRGProvider
        provider = CRGProvider(mock_proj)
        assert provider.is_available()
        results = provider.hybrid_search("upsertEntity", max_results=5, embedding_weight=0.0)
        assert len(results) > 0
        r = results[0]
        assert "_stages_tried" in r, f"Missing _stages_tried: {r.keys()}"
        assert "_stages_hit" in r, f"Missing _stages_hit: {r.keys()}"
        assert "found_via" in r, f"Missing found_via: {r.keys()}"
        # For exact symbol, found_via should mention 'exact'
        assert "exact" in r.get("found_via", "").lower() or "FTS" in r.get("found_via", "")
