"""Regression tests for the core retrieval pipeline."""

import json
import os
import sys
import tempfile
import unittest

# Ensure backend is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestChunkingModes(unittest.TestCase):
    """Verify AST vs raw_fallback chunking behavior."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmpdir, "test.py")
        with open(self.test_file, "w") as f:
            f.write("def hello():\n    return 'world'\n\nclass Test:\n    pass\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ast_mode_returns_function_chunks(self):
        from code_chunker import chunk_file, CHUNKING_MODE
        if CHUNKING_MODE != "ast":
            self.skipTest("AST modules not installed")
        chunks = chunk_file("test.py", repo_dir=self.tmpdir)
        self.assertGreater(len(chunks), 0)
        names = [c["name"] for c in chunks]
        self.assertIn("hello", names)
        self.assertIn("Test", names)

    def test_raw_fallback_marks_degraded(self):
        import code_chunker
        code_chunker.CHUNKING_MODE = "raw_fallback"
        from code_chunker import chunk_file
        chunks = chunk_file("test.py", repo_dir=self.tmpdir)
        self.assertEqual(len(chunks), 1)
        c = chunks[0]
        self.assertTrue(c.get("degraded", False))
        self.assertEqual(c.get("chunking_mode"), "raw_fallback")

    def test_fallback_truncation_visible(self):
        import code_chunker
        code_chunker.CHUNKING_MODE = "raw_fallback"
        from code_chunker import chunk_file
        large = "x" * 5000
        fpath = os.path.join(self.tmpdir, "large.txt")
        with open(fpath, "w") as f:
            f.write(large)
        chunks = chunk_file("large.txt", repo_dir=self.tmpdir)
        self.assertEqual(len(chunks), 1)
        c = chunks[0]
        self.assertTrue(c.get("truncated", False))
        self.assertLessEqual(len(c["content"]), 3000)

    def test_degraded_chunks_have_file_path(self):
        import code_chunker
        code_chunker.CHUNKING_MODE = "raw_fallback"
        from code_chunker import chunk_file
        chunks = chunk_file("test.py", repo_dir=self.tmpdir)
        self.assertEqual(chunks[0]["file_path"], "test.py")


class TestRetrievalPipeline(unittest.TestCase):
    """Verify retrieve_context returns expected structure."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.gf_dir = os.path.join(self.tmpdir, "graphify-out")
        os.makedirs(self.gf_dir)
        self.graph_data = {
            "nodes": [
                {"id": "test_func", "label": "test_func", "source_file": "app.py",
                 "community": 1, "content": "def test_func(): pass"},
                {"id": "helper", "label": "helper", "source_file": "utils.py",
                 "community": 1, "content": "def helper(): pass"},
            ],
            "links": [
                {"source": "test_func", "target": "helper"},
            ],
            "communities": {"1": {"name": "core"}},
        }
        with open(os.path.join(self.gf_dir, "graph.json"), "w") as f:
            json.dump(self.graph_data, f)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_retrieve_context_returns_expected_format(self):
        from retrieval import retrieve_context
        proj = {"graphify_data": self.graph_data, "repo_dir": self.tmpdir, "_G": None}
        result = retrieve_context(proj, "test function architecture")
        self.assertIn("context", result)
        self.assertIn("files", result)
        self.assertIn("strategy", result)
        self.assertIn("plan", result)
        self.assertIn("matched_nodes", result)

    def test_retrieve_context_no_graph_data(self):
        from retrieval import retrieve_context
        result = retrieve_context({}, "anything")
        self.assertEqual(result["strategy"], "no_data")

    def test_matched_nodes_list(self):
        from retrieval import retrieve_context
        proj = {"graphify_data": self.graph_data, "repo_dir": self.tmpdir, "_G": None}
        result = retrieve_context(proj, "test function architecture")
        self.assertIsInstance(result["matched_nodes"], list)


class TestRepoPersistence(unittest.TestCase):
    """Verify repo storage is durable and missing repo is handled."""

    def test_persistent_repo_path(self):
        from app import REPO_DIR
        self.assertTrue(os.path.isdir(REPO_DIR) or "data/repos" in REPO_DIR)
        self.assertIn("repos", REPO_DIR)


class TestNxAdapter(unittest.TestCase):
    """Verify Nx adapter is safe and doesn't crash."""

    def test_detect_nx_workspace_safe_nx(self):
        from nx_adapter import detect_nx_workspace
        tmpdir = tempfile.mkdtemp()
        try:
            result = detect_nx_workspace(tmpdir)
            self.assertFalse(result)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_extract_nx_context_graceful(self):
        from nx_adapter import extract_nx_context
        tmpdir = tempfile.mkdtemp()
        try:
            result = extract_nx_context(tmpdir)
            self.assertFalse(result.get("available", True))
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestSsemojibake(unittest.TestCase):
    """Verify SSE mojibake handling."""

    def test_mojibake_replace_at_all_points(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "services", "apiClient.js")) as f:
            content = f.read()
        self.assertIn("replace(/\\u00e2\\u0080\\u0094/g", content)

    def test_fallback_replaces_mojibake(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "hooks", "useChat.js")) as f:
            content = f.read()
        self.assertIn("replace(/\\u00e2\\u0080\\u0094/g", content)


class TestFrontendGraph(unittest.TestCase):
    """Verify frontend no longer fetches crg-db."""

    def test_no_crg_db_in_use_graph(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "hooks", "useGraph.js")) as f:
            content = f.read()
        self.assertNotIn("crgDb", content)
        self.assertNotIn("fetchCrgDb", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestFrontendDeadCodeRemoval(unittest.TestCase):
    """Verify dead frontend code has been removed."""

    def test_no_fetch_crg_db_in_graph_service(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "services", "graphService.js")) as f:
            content = f.read()
        self.assertNotIn("fetchCrgDb", content)
        self.assertNotIn("upload", content)

    def test_no_dead_methods_in_projects_service(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "services", "projectsService.js")) as f:
            content = f.read()
        self.assertNotIn("getCrgDb", content)
        self.assertNotIn("uploadData", content)
        self.assertNotIn("getMCPToken", content)

    def test_mcp_service_file_removed(self):
        path = os.path.join(os.path.dirname(__file__), "..", "..", "src", "services", "mcpService.js")
        self.assertFalse(os.path.exists(path))

    def test_no_crg_db_endpoint_in_config(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "config", "endpoints.js")) as f:
            content = f.read()
        self.assertNotIn("projectCrgDb", content)


class TestNxAdapterClosedNetwork(unittest.TestCase):
    """Verify Nx adapter prefers local binary over npx."""

    def test_nx_command_prefers_local(self):
        import tempfile, os
        from nx_adapter import _nx_command
        tmpdir = tempfile.mkdtemp()
        try:
            # No node_modules — fallback to npx nx
            cmd = _nx_command(tmpdir)
            self.assertEqual(cmd, ["npx", "nx"])
            # With local nx binary — prefer it
            local_bin = os.path.join(tmpdir, "node_modules", ".bin")
            os.makedirs(local_bin, exist_ok=True)
            nx_path = os.path.join(local_bin, "nx")
            with open(nx_path, "w") as f:
                f.write("#!/bin/sh\necho mock")
            cmd = _nx_command(tmpdir)
            self.assertEqual(cmd, [nx_path])
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_nx_command_env_var_override(self):
        import os
        os.environ["NX_MCP_COMMAND"] = "/custom/nx"
        try:
            from nx_adapter import _nx_command
            import tempfile
            tmpdir = tempfile.mkdtemp()
            try:
                cmd = _nx_command(tmpdir)
                self.assertEqual(cmd, ["/custom/nx"])
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)
        finally:
            del os.environ["NX_MCP_COMMAND"]


class TestArchitectureContext(unittest.TestCase):
    """Verify architecture queries produce real source code context."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.gf_dir = os.path.join(self.tmpdir, "graphify-out")
        os.makedirs(self.gf_dir)
        # Create a realistic graph with entrypoints and hubs
        self.graph_data = {
            "nodes": [
                {"id": "main", "label": "main", "source_file": "main.py",
                 "community": 1, "content": "def main(): pass"},
                {"id": "App", "label": "App", "source_file": "gui/app.py",
                 "community": 1, "content": "class App: pass"},
                {"id": "Database", "label": "Database", "source_file": "database.py",
                 "community": 1, "content": "class Database: pass"},
                {"id": "utility", "label": "utility", "source_file": "utils.py",
                 "community": 2, "content": "def util(): pass"},
                {"id": "hub_node", "label": "hub_node", "source_file": "bridge.py",
                 "community": 1, "content": "def bridge(): pass"},
                {"id": "config", "label": "config", "source_file": "config.py",
                 "community": 2, "content": "SETTINGS = {}"},
            ],
            "links": [
                {"source": "hub_node", "target": "main"},
                {"source": "hub_node", "target": "App"},
                {"source": "hub_node", "target": "Database"},
                {"source": "hub_node", "target": "utility"},
                {"source": "hub_node", "target": "config"},
                {"source": "main", "target": "App"},
            ],
            "communities": {"1": {"name": "core"}, "2": {"name": "util"}},
        }
        # Create actual source files
        for fn in ["main.py", "gui/app.py", "database.py", "utils.py", "bridge.py", "config.py"]:
            fpath = os.path.join(self.tmpdir, fn)
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "w") as f:
                f.write(f"# {fn}\ndef function_in_{fn.replace('.', '_')}():\n    return 42\n")
        self.proj = {"graphify_data": self.graph_data, "repo_dir": self.tmpdir, "_G": None}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_architecture_query_includes_raw_code(self):
        from retrieval import retrieve_context
        result = retrieve_context(self.proj, "architecture")
        ctx = result["context"]
        stats = result.get("context_stats", {})
        self.assertGreater(stats.get("raw_chunks", 0), 0, "architecture should produce raw chunks")
        self.assertGreater(stats.get("raw_code_chars", 0), 100, "should contain real code chars")
        self.assertIn("def function_in_", ctx, "context should contain raw code from source files")
        self.assertIn("```", ctx, "context should contain code fences")
        self.assertFalse(stats.get("degraded", True), "architecture context should not be degraded")

    def test_architecture_query_seeds_from_entrypoints(self):
        from retrieval import _seed_architecture_fallback
        matched = _seed_architecture_fallback(self.graph_data, self.graph_data.get("links", []))
        sf_list = [n.get("source_file", "") for n in matched]
        self.assertIn("main.py", sf_list, "entrypoint main.py should be seeded")
        self.assertIn("gui/app.py", sf_list, "entrypoint app.py should be seeded")
        self.assertIn("database.py", sf_list, "entrypoint database.py should be seeded")
        self.assertIn("bridge.py", sf_list, "hub node bridge.py should be seeded")

    def test_architecture_query_broad_no_exact_symbol(self):
        """Architecture query should work without exact symbol match."""
        from retrieval import retrieve_context
        proj = {"graphify_data": self.graph_data, "repo_dir": self.tmpdir, "_G": None}
        # Query "architecture" with no node literally called "architecture"
        result = retrieve_context(proj, "architecture")
        self.assertIn("context", result)
        self.assertGreater(len(result.get("files", [])), 0, "should produce file list")
        stats = result.get("context_stats", {})
        self.assertGreater(stats.get("raw_chunks", 0), 0, "should produce at least one chunk")

    def test_architecture_missing_source_is_explicit(self):
        from retrieval import retrieve_context
        proj = {"graphify_data": self.graph_data, "repo_dir": None, "_G": None}
        result = retrieve_context(proj, "architecture")
        stats = result.get("context_stats", {})
        self.assertTrue(stats.get("degraded", False), "missing repo should be degraded")
        ctx = result["context"]
        self.assertIn("DEGRADED", ctx, "degraded context should warn the LLM")

    def test_context_stats_format(self):
        from retrieval import retrieve_context
        result = retrieve_context(self.proj, "architecture")
        stats = result.get("context_stats", {})
        self.assertIn("raw_chunks", stats)
        self.assertIn("raw_code_chars", stats)
        self.assertIn("source_available", stats)
        self.assertIn("degraded", stats)
        self.assertIn("task_count", stats)

    def test_architecture_context_has_graphify_sections(self):
        from retrieval import retrieve_context
        result = retrieve_context(self.proj, "architecture")
        ctx = result["context"]
        self.assertIn("Graphify Architecture Summary", ctx)
        self.assertIn("Important Hubs", ctx)
        self.assertIn("Key Relationships", ctx)

    def test_architecture_context_has_community_structure(self):
        from retrieval import retrieve_context
        result = retrieve_context(self.proj, "architecture")
        ctx = result["context"]
        self.assertIn("Community Structure", ctx)
        self.assertIn("Community 1", ctx)
        self.assertIn("Community 2", ctx)

    def test_architecture_context_not_filelist_only(self):
        """Architecture context must not be just codebase structure."""
        from retrieval import retrieve_context
        result = retrieve_context(self.proj, "architecture")
        ctx = result["context"]
        # Should have graphify sections BEFORE codebase structure
        gi = ctx.index("Graphify Architecture Summary")
        cs = ctx.index("Codebase Structure")
        self.assertLess(gi, cs, "graphify sections should appear before codebase structure")
        # Should have real code
        self.assertIn("## Source Code", ctx)

    def test_architecture_context_has_hubs_and_edges(self):
        from retrieval import retrieve_context
        result = retrieve_context(self.proj, "architecture")
        ctx = result["context"]
        self.assertIn("degree", ctx.lower())
        self.assertIn("hub_node", ctx)
        self.assertIn("bridge.py", ctx)


class TestCRGDomainFinder(unittest.TestCase):
    """Verify CRG domain finder is backend-only and graceful."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        gf_dir = os.path.join(self.tmpdir, "graphify-out")
        os.makedirs(gf_dir)
        self.graph_data = {
            "nodes": [
                {"id": "main", "label": "main", "source_file": "main.py",
                 "community": 1, "content": "def main(): pass"},
                {"id": "App", "label": "App", "source_file": "gui/app.py",
                 "community": 1, "content": "class App: pass"},
            ],
            "links": [{"source": "main", "target": "App"}],
            "communities": {},
        }
        for fn in ["main.py", "gui/app.py"]:
            fpath = os.path.join(self.tmpdir, fn)
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "w") as f:
                f.write(f"# {fn}\ndef function_in_{fn.replace('.', '_')}():\n    return 42\n")
        self.proj = {"graphify_data": self.graph_data, "repo_dir": self.tmpdir, "_G": None}
        self.crg_db_path = os.path.join(self.tmpdir, ".code-review-graph", "graph.db")
        os.makedirs(os.path.dirname(self.crg_db_path), exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_crg_unavailable_graceful(self):
        """Missing CRG DB should not crash retrieval."""
        from retrieval import retrieve_context
        result = retrieve_context(self.proj, "architecture")
        stats = result.get("context_stats", {})
        self.assertIn("crg_domain_files_found", stats)
        self.assertEqual(stats["crg_domain_files_found"], 0)
        self.assertIn("context", result)

    def test_crg_available_returns_files(self):
        """CRG finder with FTS returns domain file candidates."""
        import sqlite3
        conn = sqlite3.connect(self.crg_db_path)
        conn.execute("""
            CREATE VIRTUAL TABLE nodes_fts USING fts5(
                name, qualified_name, file_path, signature,
                tokenize='porter unicode61'
            )
        """)
        conn.execute("""
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, name TEXT, qualified_name TEXT,
                file_path TEXT, line_start INTEGER, line_end INTEGER,
                language TEXT, signature TEXT
            )
        """)
        conn.execute(
            "INSERT INTO nodes (id, kind, name, qualified_name, file_path, language) "
            "VALUES (1, 'File', 'ocr_correction.py', 'ocr_correction', "
            "'phases/ocr_correction.py', 'python')"
        )
        conn.execute(
            "INSERT INTO nodes (id, kind, name, qualified_name, file_path, language) "
            "VALUES (2, 'Function', 'parse_receipt', 'parse_receipt', "
            "'phases/phase1_parse.py', 'python')"
        )
        conn.execute(
            "INSERT INTO nodes_fts (rowid, name, qualified_name, file_path) "
            "VALUES (1, 'ocr_correction', 'ocr_correction', 'phases/ocr_correction.py')"
        )
        conn.execute(
            "INSERT INTO nodes_fts (rowid, name, qualified_name, file_path) "
            "VALUES (2, 'parse_receipt', 'parse_receipt', 'phases/phase1_parse.py')"
        )
        conn.commit()
        conn.close()

        from crg_domain_finder import find_domain_files_with_crg
        results = find_domain_files_with_crg(self.crg_db_path, "architecture", repo_dir=self.tmpdir)
        self.assertGreater(len(results), 0, "CRG should find domain files")
        matched_terms = set()
        for r in results:
            matched_terms.update(r.get("matched_terms", []))
        self.assertIn("ocr", matched_terms, "should match OCR term")
        self.assertIn("receipt", matched_terms, "should match receipt term")

    def test_crg_architecture_context_includes_domain_files(self):
        """Architecture context should include domain workflow section when CRG is available."""
        import sqlite3
        conn = sqlite3.connect(self.crg_db_path)
        conn.execute("""
            CREATE VIRTUAL TABLE nodes_fts USING fts5(
                name, qualified_name, file_path, signature,
                tokenize='porter unicode61'
            )
        """)
        conn.execute("""
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, name TEXT, qualified_name TEXT,
                file_path TEXT, line_start INTEGER, line_end INTEGER,
                language TEXT, signature TEXT
            )
        """)
        conn.execute(
            "INSERT INTO nodes (id, kind, name, qualified_name, file_path, language) "
            "VALUES (1, 'File', 'column_mapping.py', 'column_mapping', "
            "'phases/column_mapping.py', 'python')"
        )
        conn.execute(
            "INSERT INTO nodes_fts (rowid, name, qualified_name, file_path) "
            "VALUES (1, 'column_mapping', 'column_mapping', 'phases/column_mapping.py')"
        )
        conn.commit()
        conn.close()

        from retrieval import retrieve_context
        result = retrieve_context(self.proj, "architecture")
        ctx = result.get("context", "")
        stats = result.get("context_stats", {})
        self.assertGreater(stats.get("crg_domain_files_found", 0), 0, "should find CRG files")
        self.assertIn("Domain Workflow", ctx, "context should have domain workflow section")

    def test_no_frontend_crg(self):
        """Frontend should not reference CRG DB or sql.js in active code."""
        import os
        src_dir = os.path.join(os.path.dirname(__file__), "..", "..", "src")
        # Active CRG usage patterns that should NOT appear in frontend
        # Allow 'crg-db' in docstrings / MCP config examples.
        forbidden = ["sql.js", "initSqlJs", "fetchCrgDb", "getCrgDb"]
        for pattern in forbidden:
            found = False
            for root, dirs, files in os.walk(src_dir):
                for fname in files:
                    if not fname.endswith((".js", ".jsx", ".ts", ".tsx")):
                        continue
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                            if pattern in content:
                                found = True
                                break
                    except:
                        pass
                if found:
                    break
            self.assertFalse(found, f"Pattern '{pattern}' should not appear in frontend source")

    def test_crg_does_not_replace_graphify(self):
        """Architecture context should still have graphify sections even with CRG."""
        import sqlite3
        conn = sqlite3.connect(self.crg_db_path)
        conn.execute("""
            CREATE VIRTUAL TABLE nodes_fts USING fts5(
                name, qualified_name, file_path, signature,
                tokenize='porter unicode61'
            )
        """)
        conn.execute("""
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, name TEXT, qualified_name TEXT,
                file_path TEXT, line_start INTEGER, line_end INTEGER,
                language TEXT, signature TEXT
            )
        """)
        conn.execute(
            "INSERT INTO nodes (id, kind, name, qualified_name, file_path, language) "
            "VALUES (1, 'File', 'schema.py', 'schema', "
            "'gui/schema.py', 'python')"
        )
        conn.execute(
            "INSERT INTO nodes_fts (rowid, name, qualified_name, file_path) "
            "VALUES (1, 'schema', 'schema', 'gui/schema.py')"
        )
        conn.commit()
        conn.close()

        from retrieval import retrieve_context
        result = retrieve_context(self.proj, "architecture")
        ctx = result.get("context", "")
        self.assertIn("Graphify Architecture Summary", ctx)
        self.assertIn("Important Hubs", ctx)
        self.assertIn("Key Relationships", ctx)
        self.assertIn("Community Structure", ctx)