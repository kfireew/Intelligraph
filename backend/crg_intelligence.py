"""
crg_intelligence.py — Multi-mode CRG intelligence provider.

Provides 4 query modes matched to retrieval task types:
  - search(query)       → FTS5 symbol search (what_is, search tasks)
  - architecture()      → community structure + summaries (architecture tasks)
  - impact(target)      → blast-radius over CALLS edges (impact, debug, refactor, security)
  - flows(target)       → execution flow context (how_works tasks)

Designed as part of the IntelligenceProvider framework so future providers
(Nx, Semgrep, etc.) can implement the same interface.

Fixes two bugs from crg_domain_finder.py:
  1. get_crg_db_path now checks proj["crg_db_path"] (relocated artifact) first
  2. Path normalization extracts repo prefix from CRG DB itself (works when repo_dir deleted)
"""

import json
import logging
import os
import re
import sqlite3
import sys
from collections import defaultdict, deque

log = logging.getLogger(__name__)

_VERBOSE = os.environ.get("INTELLIGRAPH_VERBOSE", "true").lower() == "true"

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


_JUNK_PATH_PATTERNS = [
    "/build/", "/bundle/", "/devtools/", "/dist/", "/out/",
    ".min.js", ".chunk.js", ".bundle.js", ".pack.js",
    "/generated/", "/codegen/", "/__generated__/",
    ".ngfactory.ts", "redux-dev-tools", "build-resources",
]

_TEST_PATH_PATTERNS = [
    "/test/", "/tests/", "/__tests__/", "/e2e/", "/e2e-tests/",
    ".test.", ".spec.", ".e2e.", ".stories.",
    "/fixtures/", "/mocks/", "/mock/", "/testdata/",
    "test-helper", "test-utils", "test-setup",
    "/cypress/", "/playwright/", "/jest/",
]


def _is_junk_path(fp):
    if not fp:
        return True
    lower = fp.lower() if isinstance(fp, str) else ""
    return any(p in lower for p in _JUNK_PATH_PATTERNS)


def _is_test_path(fp):
    """Check if a file path looks like a test/spec/mock file.
    Used to filter test noise from graph traversal — node() should find
    real implementation, not E2E test classes."""
    if not fp:
        return True
    lower = fp.lower() if isinstance(fp, str) else ""
    if not lower:
        return True
    # Existing substring patterns (handles /e2e/, .spec., /cypress/, etc.)
    if any(p in lower for p in _TEST_PATH_PATTERNS):
        return True
    # Segment-level check: catches libsE2E/ which lowercased is "libse2e"
    # (no "/e2e/" substring because the path is ".../libsE2E/foo.ts").
    # Match segments EXACTLY to avoid catching a legit "e2e-helpers" source lib.
    _TEST_SEGMENTS = {"e2e", "libse2e", "e2e-tests", "cypress", "playwright", "jest"}
    segs = [s for s in lower.replace("\\", "/").split("/") if s]
    return any(seg in _TEST_SEGMENTS for seg in segs)


def _vmsg(msg, *args):
    if not _VERBOSE:
        return
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    if args:
        try:
            msg = msg % args
        except Exception:
            pass
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


# ── Embedding infrastructure (reuses bundled all-MiniLM-L6-v2) ────

_ENCODER = None
_ENCODER_ERR = None
_EMBEDDING_CACHE = {}  # db_path -> {"names": [...], "ids": [...], "embeddings": ndarray, "built_at": float}


def _get_encoder():
    """Lazily load the sentence-transformers encoder. Reuses the same model
    as semantic_planner.py (all-MiniLM-L6-v2 from backend/models/)."""
    global _ENCODER, _ENCODER_ERR
    if _ENCODER is not None:
        return _ENCODER
    if _ENCODER_ERR is not None:
        return None
    try:
        from semantic_planner import _get_encoder as _sp_encoder
        _ENCODER = _sp_encoder()
        if _ENCODER is None:
            _ENCODER_ERR = "semantic_planner encoder unavailable"
        return _ENCODER
    except Exception as e:
        _ENCODER_ERR = str(e)
        log.warning("Encoder init failed: %s", e)
        return None


# ── Framework: IntelligenceProvider base class ────────────────────

class IntelligenceProvider:
    """Base class for code intelligence providers.

    Subclasses implement one or more query modes. Each mode returns a list of
    dicts with at least: {file_path, score, reason, source, mode}
    Additional metadata (community summaries, flow paths) is returned as structured dicts.

    Future providers (Nx, Semgrep, etc.) implement the same interface.
    """
    name = "base"

    def __init__(self, proj: dict):
        self.proj = proj

    def is_available(self) -> bool:
        """Check if this provider has data for the project."""
        return False

    def extract_target(self, query: str) -> str | None:
        """Extract the target symbol from a natural language query.

        Uses the provider's own data (FTS, node names, etc.) to find
        which codebase symbol the query is about. Returns the symbol
        name, or None if no match.
        """
        return None

    def search(self, query: str, max_results: int = 20) -> list[dict]:
        """FTS/symbol search for files matching the query."""
        return []

    def architecture(self) -> list[dict]:
        """Architecture overview (communities, modules, summaries)."""
        return []

    def impact(self, target: str, change: str = "add-value",
               offset: int = 0, max_tokens: int = 1500) -> list[dict]:
        """Blast-radius analysis: callers, callees, dependents of target."""
        return []

    def flows(self, target: str) -> list[dict]:
        """Execution flows containing the target symbol."""
        return []

    def close(self):
        """Release resources."""
        pass


# ── CRGProvider: Code Review Graph intelligence ───────────────────

class CRGProvider(IntelligenceProvider):
    """CRG-backed intelligence provider.

    Uses the CRG SQLite DB directly (no MCP) for:
    - FTS5 search on node names, signatures, file paths
    - Community structure from Leiden detection
    - Blast-radius over typed CALLS/IMPORTS_FROM edges
    - Execution flow paths from entry points
    """
    name = "crg"

    def __init__(self, proj: dict):
        super().__init__(proj)
        self._db_path = None
        self._conn = None
        self._repo_prefix = None
        self._snippet_schema = None  # "v2" (qualified_name) or "v1" (node_name only)
        self._valid_paths = None    # repo-relative path set (path validation cache)

    def is_available(self) -> bool:
        self._db_path = self._find_db()
        if self._db_path:
            _vmsg("CRG INTELLIGENCE: DB at %s", self._db_path)
        return self._db_path is not None

    def _find_db(self) -> str | None:
        """Find CRG graph.db — checks relocated artifact first, then repo_dir."""
        # 1. Relocated artifact (post-build cleanup)
        crg_path = self.proj.get("crg_db_path")
        if crg_path and os.path.isfile(crg_path):
            return crg_path
        # 2. Repo dir (if still alive — e.g. INTELLIGRAPH_ENABLE_NX_MCP=true)
        repo_dir = self.proj.get("repo_dir")
        if repo_dir:
            p = os.path.join(repo_dir, ".code-review-graph", "graph.db")
            if os.path.isfile(p):
                return p
        # 3. Artifacts dir fallback
        pid = self.proj.get("id")
        if pid:
            artifacts = os.environ.get("INTELLIGRAPH_ARTIFACTS_DIR",
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "data", "artifacts"))
            p = os.path.join(artifacts, str(pid), "graph.db")
            if os.path.isfile(p):
                return p
        return None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            self._conn.row_factory = sqlite3.Row
            self._repo_prefix = self._extract_repo_prefix()
            self._probe_snippet_schema()
        return self._conn

    def _probe_snippet_schema(self):
        """Cache node_snippets schema version once at provider startup.

        v2: has qualified_name column (unambiguous joins).
        v1: only node_name (legacy — joins on (node_name, file_path) or bare node_name).
        """
        try:
            cols = self._conn.execute("PRAGMA table_info(node_snippets)").fetchall()
            names = {r[1] for r in cols}
            if "qualified_name" in names:
                self._snippet_schema = "v2"
            else:
                self._snippet_schema = "v1"
        except Exception:
            self._snippet_schema = "v1"
        _vmsg("CRG SNIPPET schema: %s", self._snippet_schema)

    def _snippet_join_clause(self, alias: str = "n", salias: str = "s") -> str:
        """Return the SQL JOIN clause to match snippets to nodes.

        v2: join on qualified_name (unambiguous).
        v1: join on (node_name, file_path) pair (best effort, may collide for
             duplicate names — but v1 is legacy and gradually replaced by re-sync).
        """
        if self._snippet_schema == "v2":
            return f"JOIN nodes {alias} ON {alias}.qualified_name = {salias}.qualified_name"
        return f"JOIN nodes {alias} ON {alias}.name = {salias}.node_name AND {alias}.file_path = {salias}.file_path"

    def _extract_repo_prefix(self) -> str:
        """Extract the repo root prefix for path normalization.

        Priority:
        1. proj["repo_dir"] — known, absolute, correct (when MCP runs with --repo-dir)
        2. proj["original_repo_dir"] — saved before Docker repo deletion
        3. Common prefix from DB paths — fallback

        The returned prefix is forward-slashed and ends with /.
        """
        # 1. Known repo_dir — always correct, no guessing
        repo_dir = self.proj.get("repo_dir")
        if repo_dir:
            prefix = repo_dir.replace("\\", "/").rstrip("/") + "/"
            _vmsg("CRG INTELLIGENCE: repo prefix (from repo_dir) = %s", prefix)
            return prefix

        # 2. Saved original_repo_dir (Docker path, saved before deletion)
        original = self.proj.get("original_repo_dir")
        if original:
            prefix = original.replace("\\", "/").rstrip("/") + "/"
            _vmsg("CRG INTELLIGENCE: repo prefix (from original_repo_dir) = %s", prefix)
            return prefix

        # 3. Fallback: common prefix from DB paths
        conn = self._conn
        try:
            rows = conn.execute(
                "SELECT DISTINCT file_path FROM nodes WHERE file_path IS NOT NULL LIMIT 200"
            ).fetchall()
            if not rows:
                return ""
            paths = [r["file_path"].replace("\\", "/") for r in rows if r["file_path"]]
            if not paths:
                return ""
            common = os.path.commonprefix(paths)
            idx = common.rfind("/")
            if idx > 0:
                prefix = common[:idx + 1]
                _vmsg("CRG INTELLIGENCE: repo prefix (from DB common) = %s", prefix)
                return prefix
        except Exception as e:
            log.warning("CRG prefix extraction failed: %s", e)
        return ""

    def _normalize_path(self, abs_path: str) -> str:
        """Convert CRG absolute path to repo-relative path.

        Strips the known repo prefix (from repo_dir or DB common prefix).
        No folder-name guessing, no markers — just strips the prefix we know
        is correct.
        """
        if not abs_path:
            return ""
        p = abs_path.replace("\\", "/")
        if self._repo_prefix and p.lower().startswith(self._repo_prefix.lower()):
            return p[len(self._repo_prefix):]
        return p

    def build_valid_paths(self):
        """Walk REPO_DIR once and cache the set of repo-relative file paths.

        Used for path validation: results whose file_path isn't in this set
        are tagged [stale] (phantom files from a stale graph or rewrite mismatch).
        ~100ms for a typical repo. Call after _sync_from_pod or on first use.
        """
        if self._valid_paths is not None:
            return self._valid_paths
        repo_dir = self.proj.get("repo_dir")
        if not repo_dir or not os.path.isdir(repo_dir):
            self._valid_paths = set()
            return self._valid_paths
        valid = set()
        repo_prefix = repo_dir.replace("\\", "/").rstrip("/") + "/"
        for root, _dirs, files in os.walk(repo_dir):
            for f in files:
                full = os.path.join(root, f).replace("\\", "/")
                if full.lower().startswith(repo_prefix.lower()):
                    valid.add(full[len(repo_prefix):])
                else:
                    valid.add(full)
        self._valid_paths = valid
        _vmsg("CRG VALID_PATHS: cached %d files from %s", len(valid), repo_dir)
        return valid

    def is_path_valid(self, repo_rel_path: str) -> bool:
        """Check if a repo-relative path exists in the project filesystem."""
        if not repo_rel_path:
            return False
        if self._valid_paths is None:
            self.build_valid_paths()
        p = repo_rel_path.replace("\\", "/")
        return p in self._valid_paths or p.lstrip("/") in self._valid_paths

    @staticmethod
    def _extract_terms(query: str) -> list[str]:
        """Extract meaningful search terms from a natural language query.

        Returns both individual words AND multi-word compound terms.
        For "who calls build_graph function", returns ['build_graph', 'build', 'graph'].
        Compound terms are tried first (more specific).
        """
        if not query:
            return []
        lower = query.lower()
        stopwords = {
            # Grammar
            "what", "how", "where", "which", "who", "the", "a", "an", "is",
            "are", "does", "do", "can", "should", "would", "could", "explain",
            "describe", "and", "or", "of", "to", "in", "for", "with", "about",
            "tell", "me", "find", "show", "list", "this", "that", "it", "from",
            "if", "i", "on", "all", "behind", "through", "walk", "happens",
            "when", "could", "would", "parts", "touch", "locate",
            # Generic programming terms (match too many symbols)
            "function", "method", "class", "module", "file", "code", "variable",
            "project", "app", "application", "codebase", "system", "component",
            "design", "work", "works", "working", "overview", "architecture",
            "structure", "layout", "organized", "modules", "exist", "entry",
            "point", "run", "available", "targets", "affected", "generators",
            "workspace", "callers", "implementation", "invoke", "invokes",
            "operate", "main", "pipeline", "algorithm", "flow",
            "type", "types", "enum", "enums", "interface", "interfaces",
            "constant", "constants", "import", "imports", "export", "exports",
            # Intent keywords (already captured by semantic router)
            "impact", "blast", "radius", "test", "tests", "coverage", "spec",
            "secure", "security", "vulnerable", "debug", "error", "bug",
            "issue", "refactor", "break", "breaks", "change", "modify",
            "update", "depends", "calls", "uses", "imports", "defined",
            "declared", "called", "used", "rely", "relies", "vulnerabilities",
        }

        # Split on spaces/punctuation but preserve underscores
        raw_tokens = re.split(r"[\s\-./]+", lower)

        # Individual meaningful words
        # Allow underscores in identifiers (hybrid_search, _vmsg, etc.)
        words = []
        for token in raw_tokens:
            token = token.strip()
            if len(token) > 2 and token.replace("_", "").isalpha() and token not in stopwords:
                words.append(token)

        # Also try multi-word compound: "build_graph" from "build graph"
        # Rejoin consecutive meaningful words with underscore
        compounds = []
        current_compound = []
        for token in raw_tokens:
            token = token.strip()
            if token and len(token) > 2 and token not in stopwords and token.replace("_", "").isalpha():
                current_compound.append(token)
            else:
                if len(current_compound) > 1:
                    compounds.append("_".join(current_compound))
                current_compound = []
        if len(current_compound) > 1:
            compounds.append("_".join(current_compound))

        # Return compounds first (more specific), then individual words.
        # Dedupe: "plane type enum plane types" -> ["plane"] not ["plane", "plane"].
        # If nothing extracted (e.g. all stopwords), fall back to the raw query.
        seen = set()
        terms = []
        for t in compounds + words:
            if t not in seen:
                seen.add(t)
                terms.append(t)
        return terms or [query.lower().strip()]

    # ── Mode 0: Target extraction ──────────────────────────────────

    def extract_target(self, query: str) -> str | None:
        """Extract the target symbol from a natural language query using FTS.

        Two-pass approach:
          1. LIKE search for compound terms (e.g. "build_graph") — exact substring
          2. FTS search for individual words — with relevance filtering

        Only returns a match if the search term is a substring of the symbol
        name (prevents "change" matching "_changed_path_candidates").
        """
        conn = self._get_conn()
        terms = self._extract_terms(query)
        if not terms:
            return None

        # Terms are ordered by priority (compounds first, then words in
        # order of appearance in the query). Return the first match instead
        # of comparing relevance scores — the subject of the query ("map")
        # should win over a later higher-relevance term ("add").
        for term in terms:
            matched = self._try_term_match(conn, term)
            if matched:
                _vmsg("CRG EXTRACT_TARGET: query='%s' -> '%s' (first match for '%s')", query[:50], matched, term)
                return matched

        _vmsg("CRG EXTRACT_TARGET: no match for query='%s' terms=%s", query[:50], terms[:5])
        return None

    @staticmethod
    def _try_term_match(conn, term: str) -> str | None:
        """Try to find a symbol matching a single term. Returns name or None."""
        # Pass 1: LIKE search for exact substring match (handles compound terms)
        if "_" in term or len(term) > 4:
            try:
                rows = conn.execute(
                    "SELECT name, kind FROM nodes "
                    "WHERE kind IN ('Function', 'Class') "
                    "AND LOWER(name) LIKE ? "
                    "ORDER BY LENGTH(name) ASC LIMIT 5",
                    (f"%{term}%",)
                ).fetchall()
                for r in rows:
                    name = r["name"]
                    if term in name.lower():
                        return name
            except Exception:
                pass

        # Pass 2: FTS search (handles tokenized matches)
        try:
            rows = conn.execute(
                "SELECT n.name, n.kind FROM nodes_fts f JOIN nodes n ON f.rowid = n.id "
                "WHERE nodes_fts MATCH ? AND n.kind IN ('Function', 'Class') "
                "ORDER BY rank LIMIT 10",
                (f'"{term}"',)
            ).fetchall()
            for r in rows:
                name = r["name"]
                name_lower = name.lower()
                if term in name_lower:
                    return name
                if name_lower in term:
                    return name
        except Exception as e:
            log.warning("CRG extract_target FTS failed for '%s': %s", term, e)
        return None

    # ── Mode 1: Deterministic search (path + symbol LIKE + FTS5) ───

    _MAX_PATH_FILES = 50  # safety net for absurdly broad matches only
    _MAX_CONNECTIONS_PER_FILE = 5
    _NEAR_HOPS = 3

    def _path_search(self, query: str, near: str = "") -> list[dict]:
        """Find files by path pattern. Returns bare paths + graph connections.

        Only runs when the query looks path-like (contains /, \\, *, or a file
        extension). Natural language queries skip this — they'd do a full table
        scan for nothing.
        """
        conn = self._get_conn()
        raw = query.strip()
        if not raw or len(raw) < 2:
            return []

        # Skip path search for natural language queries
        has_path_char = "/" in raw or "\\" in raw or "*" in raw or "?" in raw
        has_file_ext = any(raw.lower().endswith(f".{e}") for e in
                           ("ts", "js", "jsx", "tsx", "py", "java", "go", "rs",
                            "rb", "php", "c", "cpp", "h", "cs", "kt", "swift", "scala"))
        word_count = len(raw.split())
        if not has_path_char and not has_file_ext and word_count > 2:
            return []

        path_query = raw.replace("\\", "/").lower()
        like_pattern = path_query.replace("**", "%").replace("*", "%").replace("?", "_")
        if "%" not in like_pattern and "_" not in like_pattern:
            like_pattern = f"%{like_pattern}%"

        try:
            rows = conn.execute(
                "SELECT DISTINCT file_path FROM nodes "
                "WHERE file_path IS NOT NULL "
                "AND REPLACE(LOWER(file_path), '\\', '/') LIKE ? "
                f"LIMIT {self._MAX_PATH_FILES}",
                (like_pattern,)
            ).fetchall()
        except Exception:
            return []

        # Normalize and deduplicate file paths
        matched_files = []
        seen = set()
        for r in rows:
            fp = self._normalize_path(r["file_path"])
            if not fp or _is_junk_path(fp) or _is_test_path(fp):
                continue
            if fp in seen:
                continue
            seen.add(fp)
            matched_files.append((fp, r["file_path"]))

        if not matched_files:
            return []

        # If `near` param: BFS from near symbol, filter to connected files only
        if near:
            near_files = self._bfs_files_for_symbol(near, self._NEAR_HOPS)
            if near_files:
                filtered = [(fp, raw_fp) for fp, raw_fp in matched_files if fp in near_files]
                if filtered:
                    matched_files = filtered
                    _vmsg("CRG PATH+NEAR: '%s' near '%s' -> %d files (filtered from %d)",
                          raw[:50], near[:50], len(matched_files), len(seen))

        # Rank: by edge count (centrality). Hub files first.
        # One query for all matched files.
        if len(matched_files) > 1:
            raw_paths = [rf for _, rf in matched_files]
            placeholders = ",".join("?" * len(raw_paths))
            try:
                edge_counts = conn.execute(
                    f"SELECT file_path, COUNT(*) as cnt FROM edges "
                    f"WHERE file_path IN ({placeholders}) "
                    f"GROUP BY file_path",
                    raw_paths
                ).fetchall()
                count_map = {self._normalize_path(r["file_path"]): r["cnt"] for r in edge_counts}
            except Exception:
                count_map = {}
            matched_files.sort(key=lambda x: -count_map.get(x[0], 0))

        # Build results: path + best symbol name + connections (max 5)
        results = []
        for fp, raw_fp in matched_files:
            connections = self._get_file_connections(raw_fp)
            # Look up the best (shortest non-File) symbol name from this file
            best_name, best_kind, best_sig, best_ls, best_le = "", "", "", 0, 0
            try:
                sym_row = conn.execute(
                    "SELECT name, kind, signature, line_start, line_end FROM nodes "
                    "WHERE file_path = ? AND name IS NOT NULL AND name != '' "
                    "AND kind != 'File' "
                    "ORDER BY LENGTH(name) ASC, line_start ASC LIMIT 1",
                    (raw_fp,)
                ).fetchone()
                if sym_row:
                    best_name = sym_row["name"] or ""
                    best_kind = sym_row["kind"] or ""
                    best_sig = sym_row["signature"] or ""
                    best_ls = sym_row["line_start"] or 0
                    best_le = sym_row["line_end"] or 0
            except Exception:
                pass
            results.append({
                "file_path": fp,
                "name": best_name,
                "kind": best_kind,
                "signature": best_sig,
                "line_start": best_ls,
                "line_end": best_le,
                "score": 20.0,
                "exact_match": True,
                "matched_terms": [raw],
                "reason": ["path_match"],
                "source": "crg",
                "mode": "search",
                "connections": connections,
            })

        if results:
            _vmsg("CRG PATH: query='%s' near='%s' -> %d files", raw[:50], near[:30], len(results))
        return results

    def _search_nm_index(self, terms: list[str], near: str = "") -> list[dict]:
        """Search node_modules .d.ts index for symbols not in the graph.

        Falls back when graph search finds <3 results. Returns symbols from
        external npm packages (e.g. @romach/enums) with their .d.ts file:line.
        """
        nm_path = self.proj.get("nm_index_path")
        if not nm_path or not os.path.isfile(nm_path):
            return []
        try:
            nm_conn = sqlite3.connect(f"file:{nm_path}?mode=ro", uri=True)
            nm_conn.row_factory = sqlite3.Row
        except Exception:
            return []

        results = []
        try:
            for term in terms:
                try:
                    rows = nm_conn.execute(
                        "SELECT name, kind, file_path, line_start, line_end, signature, package_name "
                        "FROM nm_symbols WHERE LOWER(name) LIKE ? "
                        "ORDER BY LENGTH(name) ASC LIMIT 10",
                        (f"%{term}%",)
                    ).fetchall()
                except Exception:
                    continue
                for r in rows:
                    is_exact = r["name"].lower() == term.lower()
                    results.append({
                        "file_path": r["file_path"],
                        "name": r["name"],
                        "kind": r["kind"] or "External",
                        "signature": r["signature"] or "",
                        "line_start": r["line_start"] or 0,
                        "line_end": r["line_end"] or 0,
                        "score": 15.0 if is_exact else 8.0,
                        "exact_match": is_exact,
                        "matched_terms": [term],
                        "reason": ["node_modules"],
                        "source": "node_modules",
                        "mode": "search",
                        "package": r["package_name"] or "",
                    })
        finally:
            nm_conn.close()

        if results:
            _vmsg("NM SEARCH: terms=%s -> %d results", terms[:3], len(results))
        return results

    def _get_file_connections(self, raw_file_path: str) -> list[str]:
        """Get up to 5 symbol names connected to symbols in this file (both directions).
        Only returns actual symbol names (Function/Class/Method/etc.), not File-kind nodes."""
        conn = self._get_conn()
        connections = set()
        try:
            outgoing = conn.execute(
                "SELECT DISTINCT n2.name FROM nodes n1 "
                "JOIN edges e ON e.source_qualified = n1.qualified_name "
                "JOIN nodes n2 ON n2.qualified_name = e.target_qualified "
                "WHERE n1.file_path = ? AND n2.name IS NOT NULL AND n2.file_path != ? "
                "AND n2.kind != 'File' "
                "LIMIT ?",
                (raw_file_path, raw_file_path, self._MAX_CONNECTIONS_PER_FILE)
            ).fetchall()
            for r in outgoing:
                connections.add(r["name"])
        except Exception:
            pass
        try:
            if len(connections) < self._MAX_CONNECTIONS_PER_FILE:
                remaining = self._MAX_CONNECTIONS_PER_FILE - len(connections)
                incoming = conn.execute(
                    "SELECT DISTINCT n2.name FROM nodes n1 "
                    "JOIN edges e ON e.target_qualified = n1.qualified_name "
                    "JOIN nodes n2 ON n2.qualified_name = e.source_qualified "
                    "WHERE n1.file_path = ? AND n2.name IS NOT NULL AND n2.file_path != ? "
                    "AND n2.kind != 'File' "
                    "LIMIT ?",
                    (raw_file_path, raw_file_path, remaining)
                ).fetchall()
                for r in incoming:
                    connections.add(r["name"])
        except Exception:
            pass
        return sorted(connections)[:self._MAX_CONNECTIONS_PER_FILE]

    def _bfs_files_for_symbol(self, symbol: str, max_depth: int) -> set[str]:
        """BFS from a symbol or file path, return all file_paths within max_depth hops.

        Accepts either a symbol name ('AuthService') or a file path
        ('src/auth/service.ts'). If it looks like a path (contains / or \\.),
        matches against file_path; otherwise matches against symbol names.
        """
        conn = self._get_conn()
        sym_lower = symbol.lower().strip()

        # Detect: is this a file path or a symbol name?
        is_path = "/" in sym_lower or "\\" in sym_lower or sym_lower.endswith(
            tuple(f".{e}" for e in ("ts", "js", "jsx", "tsx", "py", "java", "go", "rs", "rb", "php", "c", "cpp", "h", "cs", "kt", "swift", "scala")))

        start_nodes = []
        if is_path:
            # Match by file path (LIKE substring)
            path_pattern = f"%{sym_lower.replace(chr(92), '/')}%"
            try:
                start_nodes = conn.execute(
                    "SELECT qualified_name, file_path FROM nodes "
                    "WHERE file_path IS NOT NULL "
                    "AND REPLACE(LOWER(file_path), '\\', '/') LIKE ? LIMIT 20",
                    (path_pattern,)
                ).fetchall()
            except Exception:
                pass
        else:
            # Match by symbol name
            start_nodes = conn.execute(
                "SELECT qualified_name, file_path FROM nodes "
                "WHERE LOWER(name) = ? OR LOWER(qualified_name) LIKE ? LIMIT 10",
                (sym_lower, f"%{sym_lower}%")
            ).fetchall()
            if not start_nodes:
                try:
                    start_nodes = conn.execute(
                        "SELECT n.qualified_name, n.file_path FROM nodes_fts f "
                        "JOIN nodes n ON f.rowid = n.id WHERE nodes_fts MATCH ? LIMIT 10",
                        (f'"{symbol}"',)
                    ).fetchall()
                except Exception:
                    pass

        if not start_nodes:
            # Snippet fallback: scan node_snippets for the symbol string.
            # Catches object property values (TRACK_ZIK), string literals,
            # and dotted accessors that are never graph nodes but DO appear
            # in source snippets. Returns the containing files directly —
            # no BFS needed (the snippets define/use the value).
            try:
                if self._snippet_schema == "v2":
                    snip_rows = conn.execute(
                        "SELECT DISTINCT file_path FROM node_snippets "
                        "WHERE LOWER(snippet) LIKE ? LIMIT 50",
                        (f"%{sym_lower}%",)
                    ).fetchall()
                else:
                    snip_rows = conn.execute(
                        "SELECT DISTINCT s.node_name, n.file_path FROM node_snippets s "
                        "LEFT JOIN nodes n ON n.name = s.node_name "
                        "WHERE LOWER(s.snippet) LIKE ? LIMIT 50",
                        (f"%{sym_lower}%",)
                    ).fetchall()
                if snip_rows:
                    result_files = set()
                    for r in snip_rows:
                        fp = r["file_path"]
                        if fp:
                            result_files.add(self._normalize_path(fp))
                    if result_files:
                        _vmsg("CRG BFS near '%s': snippet fallback -> %d files",
                              symbol[:40], len(result_files))
                        return result_files
            except Exception as e:
                log.warning("CRG BFS snippet fallback failed: %s", e)
            return set()

        frontier = set()
        result_files = set()
        for n in start_nodes:
            if n["qualified_name"]:
                frontier.add(n["qualified_name"])
            if n["file_path"]:
                result_files.add(self._normalize_path(n["file_path"]))

        visited = set()
        depth = 0
        while frontier and depth < max_depth:
            depth += 1
            next_frontier = set()
            for qname in frontier:
                if qname in visited:
                    continue
                visited.add(qname)
                try:
                    # Outgoing edges
                    for r in conn.execute(
                        "SELECT target_qualified FROM edges WHERE source_qualified = ?",
                        (qname,)
                    ).fetchall():
                        if r["target_qualified"] and r["target_qualified"] not in visited:
                            next_frontier.add(r["target_qualified"])
                    # Incoming edges
                    for r in conn.execute(
                        "SELECT source_qualified, n.file_path FROM edges e "
                        "LEFT JOIN nodes n ON n.qualified_name = e.source_qualified "
                        "WHERE e.target_qualified = ?",
                        (qname,)
                    ).fetchall():
                        if r["source_qualified"] and r["source_qualified"] not in visited:
                            next_frontier.add(r["source_qualified"])
                        if r["file_path"]:
                            result_files.add(self._normalize_path(r["file_path"]))
                except Exception:
                    pass
            # Also collect file_paths for outgoing edges
            try:
                qnames = list(next_frontier)
                if qnames:
                    placeholders = ",".join("?" * len(qnames))
                    for r in conn.execute(
                        f"SELECT file_path FROM nodes WHERE qualified_name IN ({placeholders})",
                        qnames
                    ).fetchall():
                        if r["file_path"]:
                            result_files.add(self._normalize_path(r["file_path"]))
            except Exception:
                pass
            frontier = next_frontier - visited

        _vmsg("CRG BFS near '%s': %d files within %d hops", symbol[:40], len(result_files), max_depth)
        return result_files

    def search(self, query: str, max_results: int = 20, near: str = "") -> list[dict]:
        """Deterministic search: path LIKE + symbol LIKE + FTS5.

        Three passes, all cheap (indexed columns, no embeddings):
          Pass 0a: file_path LIKE — finds files by path pattern (bare paths + connections)
          Pass 0b: symbol name LIKE — catches camelCase/underscore names
          Pass 1:  FTS5 — tokenized symbol/signature match

        If `near` is provided, ALL results are filtered to only files
        connected to the near symbol/file (within 3 graph hops).

        Returns: [{file_path, name, kind, signature, line_start, line_end,
                   score, exact_match, matched_terms, reason, source, mode, connections?}]
        """
        conn = self._get_conn()
        terms = self._extract_terms(query)
        if not terms:
            return []

        # If near is provided, compute the allowed file set ONCE.
        # If near can't be resolved (not a graph symbol AND not in snippets),
        # we do NOT zero-out — run unfiltered and tag results as near_unresolved.
        near_files = None
        near_unresolved = False
        if near:
            near_files = self._bfs_files_for_symbol(near, self._NEAR_HOPS)
            if not near_files:
                _vmsg("CRG SEARCH: near '%s' not found — running unfiltered", near[:50])
                near_files = None
                near_unresolved = True

        # Pass 0a: Path LIKE — bare paths + graph connections
        path_results = self._path_search(query, near=near)
        path_files = {r["file_path"] for r in path_results}

        file_scores = defaultdict(lambda: {"score": 0.0, "names": [], "kinds": set(),
                                           "matched_terms": set(), "signatures": [],
                                           "exact_match": False,
                                           "line_start": 0, "line_end": 0})

        # Pass 0b: Symbol name LIKE — catches camelCase / concatenated / underscore names.
        # No kind filter — File, Enum, Interface, Test all match.
        for term in terms:
            like_term = term.replace(" ", "").replace("_", "")
            for try_term in [term, like_term]:
                if len(try_term) < 3:
                    continue
                try:
                    rows = conn.execute(
                        "SELECT name, kind, file_path, signature, community_id, "
                        "line_start, line_end FROM nodes "
                        "WHERE LOWER(name) LIKE ? "
                        "ORDER BY LENGTH(name) ASC LIMIT 10",
                        (f"%{try_term}%",)
                    ).fetchall()
                    for r in rows:
                        fp = self._normalize_path(r["file_path"])
                        if not fp or _is_junk_path(fp) or _is_test_path(fp):
                            continue
                        if fp in path_files:
                            continue
                        is_exact = r["name"].lower() == try_term
                        entry = file_scores[fp]
                        entry["score"] += (15.0 if is_exact else 8.0)
                        entry["names"].append(r["name"])
                        entry["kinds"].add(r["kind"])
                        entry["matched_terms"].add(term)
                        if r["signature"]:
                            entry["signatures"].append(r["signature"])
                        if is_exact:
                            entry["exact_match"] = True
                        if r["line_start"]:
                            entry["line_start"] = r["line_start"]
                        if r["line_end"]:
                            entry["line_end"] = r["line_end"]
                except Exception:
                    pass

        # Pass 1: FTS5 search
        for i, term in enumerate(terms):
            weight = 5.0 if i < 3 else 3.0
            try:
                rows = conn.execute(
                    "SELECT n.file_path, n.name, n.kind, n.signature, n.community_id, "
                    "n.line_start, n.line_end "
                    "FROM nodes_fts f JOIN nodes n ON f.rowid = n.id "
                    "WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?",
                    (f'"{term}"', 15)
                ).fetchall()
                for r in rows:
                    fp = self._normalize_path(r["file_path"])
                    if not fp or _is_junk_path(fp):
                        continue
                    if fp in path_files:
                        continue
                    is_exact = r["name"].lower() == term.lower()
                    entry = file_scores[fp]
                    entry["score"] += (15.0 if is_exact else weight)
                    entry["names"].append(r["name"])
                    entry["kinds"].add(r["kind"])
                    entry["matched_terms"].add(term)
                    if r["signature"]:
                        entry["signatures"].append(r["signature"])
                    if is_exact:
                        entry["exact_match"] = True
                    if r["line_start"]:
                        entry["line_start"] = r["line_start"]
                    if r["line_end"]:
                        entry["line_end"] = r["line_end"]
            except Exception as e:
                log.warning("CRG FTS search for '%s' failed: %s", term, e)

        # Pass 1b: Lexical (snippet/value) lookup — catches string values that
        # FTS misses: const object properties (TRACK_ZIK), string literals,
        # dotted accessors (IconsNames.TRACK_ZIK). These live in source code
        # snippets, not as graph nodes. Additive — only runs when results are
        # sparse OR a term looks like a constant/dotted accessor.
        _constant_re = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
        has_exact = any(d["exact_match"] for d in file_scores.values())
        has_constant_term = any(_constant_re.match(t) or "." in t for t in terms)
        # Lexical pass: run when (a) a term looks like a constant/dotted accessor
        # (these are values, not graph nodes), OR (b) results are sparse AND no
        # exact symbol match was found. Skip when we already have exact matches
        # for normal symbol queries — lexical adds noise without value.
        needs_lexical = has_constant_term or (not has_exact and len(file_scores) < 3)
        lexical_hits = {}  # fp -> {names, term} for transparency tracking
        if needs_lexical:
            join_clause = self._snippet_join_clause("n", "s")
            for term in terms:
                if len(term) < 3:
                    continue
                try:
                    if self._snippet_schema == "v2":
                        snip_rows = conn.execute(
                            f"SELECT s.node_name, n.file_path, n.kind, "
                            f"n.line_start, n.line_end, n.qualified_name "
                            f"FROM node_snippets s {join_clause} "
                            f"WHERE LOWER(s.snippet) LIKE ? LIMIT 15",
                            (f"%{term.lower()}%",)
                        ).fetchall()
                    else:
                        snip_rows = conn.execute(
                            f"SELECT s.node_name, n.file_path, n.kind, "
                            f"n.line_start, n.line_end, '' as qualified_name "
                            f"FROM node_snippets s LEFT JOIN nodes n ON n.name = s.node_name "
                            f"WHERE LOWER(s.snippet) LIKE ? LIMIT 15",
                            (f"%{term.lower()}%",)
                        ).fetchall()
                except Exception:
                    continue
                for r in snip_rows:
                    fp = self._normalize_path(r["file_path"]) if r["file_path"] else ""
                    if not fp or _is_junk_path(fp) or _is_test_path(fp):
                        continue
                    if fp in path_files:
                        continue
                    entry = file_scores[fp]
                    entry["score"] += 6.0
                    entry["names"].append(r["node_name"] or "")
                    entry["kinds"].add(r["kind"] or "Value")
                    entry["matched_terms"].add(term)
                    if r["line_start"]:
                        entry["line_start"] = r["line_start"]
                        entry["line_end"] = r["line_end"]
                    lexical_hits.setdefault(fp, {"name": r["node_name"] or "", "term": term})

        # Boost files matching multiple terms
        for fp, data in file_scores.items():
            if len(data["matched_terms"]) >= 2:
                data["score"] *= 1.5

        # Merge: path results (per-symbol) first, then FTS/symbol results (per-file)
        results = list(path_results)
        for fp, data in sorted(file_scores.items(), key=lambda x: -x[1]["score"])[:max_results]:
            reasons = ["crg_fts_match"]
            if fp in lexical_hits:
                reasons.append("lexical")
            results.append({
                "file_path": fp,
                "score": round(data["score"], 1),
                "name": data["names"][0] if data["names"] else "",
                "kind": list(data["kinds"])[0] if data["kinds"] else "",
                "signature": data["signatures"][0] if data["signatures"] else "",
                "line_start": data["line_start"],
                "line_end": data["line_end"],
                "exact_match": data["exact_match"],
                "matched_terms": sorted(data["matched_terms"]),
                "reason": reasons,
                "source": "crg",
                "mode": "search",
                "lexical_hit": lexical_hits.get(fp),
            })

        # If near is provided, filter ALL results to only files in the near subgraph.
        # If near was unresolved (near_files is None + near_unresolved), skip
        # filtering and tag all results so the format layer can emit guidance.
        if near_files is not None:
            results = [r for r in results if r.get("file_path") in near_files]
        elif near_unresolved:
            for r in results:
                reasons = r.get("reason", [])
                if "near_unresolved" not in reasons:
                    reasons.append("near_unresolved")
                    r["reason"] = reasons

        _vmsg("CRG SEARCH: query='%s' terms=%s near='%s' -> %d results (path=%d fts=%d lex=%d%s)",
              query[:50], terms[:5], near[:30], len(results), len(path_results),
              len(file_scores), len(lexical_hits), " [near_unresolved]" if near_unresolved else "")

        # Fallback: node_modules .d.ts index (external packages not in graph)
        if len(results) < 3:
            nm_results = self._search_nm_index(terms, near=near)
            results.extend(nm_results)

        return results[:max_results]

    # ── Mode 1b: Semantic (embedding) search ──────────────────────

    def _build_embedding_index(self):
        """Build a numpy embedding index of all CRG node names + signatures.

        Caches per db_path. For 28k nodes: ~43MB in RAM, ~2s to build.
        """
        if not _HAS_NUMPY:
            return None
        encoder = _get_encoder()
        if encoder is None:
            return None

        cache_key = self._db_path
        cached = _EMBEDDING_CACHE.get(cache_key)
        if cached is not None:
            return cached

        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, kind, signature, file_path, qualified_name, "
                "line_start, line_end "
                "FROM nodes WHERE name IS NOT NULL AND kind IN ('Function','Class','Method','File') "
                "ORDER BY id"
            ).fetchall()
        except Exception:
            rows = conn.execute(
                "SELECT id, name, '' as kind, '' as signature, file_path, '' as qualified_name, "
                "line_start, line_end "
                "FROM nodes WHERE name IS NOT NULL ORDER BY id"
            ).fetchall()

        if not rows:
            _EMBEDDING_CACHE[cache_key] = None
            return None

        texts = []
        ids = []
        meta = []
        for r in rows:
            name = r["name"] or ""
            sig = r["signature"] or ""
            text = f"{name} {sig}".strip() if sig else name
            if not text or len(text) < 2:
                continue
            texts.append(text[:200])
            ids.append(r["id"])
            meta.append({
                "id": r["id"], "name": name, "kind": r["kind"] or "",
                "file_path": self._normalize_path(r["file_path"]) if r["file_path"] else "",
                "qualified_name": r["qualified_name"] or name,
                "signature": r["signature"] or "",
                "line_start": r["line_start"] or 0,
                "line_end": r["line_end"] or 0,
            })

        if not texts:
            _EMBEDDING_CACHE[cache_key] = None
            return None

        try:
            embeddings = encoder.encode(texts, show_progress_bar=False, convert_to_numpy=True)
        except Exception as e:
            log.warning("Embedding encode failed for %d texts: %s", len(texts), e)
            _EMBEDDING_CACHE[cache_key] = None
            return None

        index = {
            "texts": texts, "ids": ids, "meta": meta,
            "embeddings": embeddings,
            "count": len(texts),
        }
        _EMBEDDING_CACHE[cache_key] = index
        _vmsg("CRG EMBED INDEX: built for %s, %d nodes, dim=%d", cache_key, len(texts), embeddings.shape[1])
        return index

    def semantic_search(self, query: str, max_results: int = 20) -> list[dict]:
        """Semantic (embedding-based) search for nodes matching query meaning.

        Returns: [{file_path, name, kind, score, reason, source, mode}]
        Score is raw cosine similarity (0-1). Threshold 0.25 drops weak matches.
        """
        if not _HAS_NUMPY:
            return []
        encoder = _get_encoder()
        if encoder is None:
            return []
        index = self._build_embedding_index()
        if index is None:
            return []

        try:
            q_emb = encoder.encode([query], show_progress_bar=False, convert_to_numpy=True)[0]
        except Exception as e:
            log.warning("Query embedding failed: %s", e)
            return []

        scores = index["embeddings"] @ q_emb
        top_indices = np.argsort(scores)[::-1][:max_results * 2]

        results = []
        for idx in top_indices:
            if len(results) >= max_results:
                break
            score = float(scores[idx])
            if score < 0.25:
                break
            m = index["meta"][idx]
            fp = m["file_path"]
            if not fp or _is_junk_path(fp):
                continue
            results.append({
                "file_path": fp,
                "name": m["name"],
                "kind": m["kind"],
                "signature": m.get("signature", ""),
                "line_start": m.get("line_start", 0),
                "line_end": m.get("line_end", 0),
                "score": round(score, 3),
                "reason": ["semantic_match"],
                "source": "crg",
                "mode": "semantic",
                "matched_terms": [],
            })

        _vmsg("CRG SEMANTIC: query='%s' -> %d results", query[:50], len(results))
        return results

    def hybrid_search(self, query: str, max_results: int = 20, embedding_weight: float = 0.4, near: str = "") -> list[dict]:
        """Unified retrieval router: exact → lexical → FTS → semantic, soft cascade.

        Stage 1 (Lexical retrieval, bundled): path LIKE + symbol LIKE + FTS5 +
        snippet value lookup. All cheap indexed lookups in one pass.
        If path matches found OR ≥3 results OR ≥3 exact matches → return.
        No embeddings computed. Fast (<1s).

        Stage 2 (Semantic): only if Stage 1 produced insufficient results.
        Runs embedding search, merges with RRF.

        Soft cascade: stages run in order, stop as soon as one confidently
        answers. Semantic only runs when lexical+FTS both produce sparse
        results. This prevents retry loops: one call produces the best
        available answer using at most one lexical stage and, if necessary,
        one semantic stage.

        Each result carries:
        - `confidence` (HIGH/MEDIUM/LOW)
        - `confidence_reason` (human-readable)
        - `found_via` (which backend answered: "exact symbol", "lexical (...)",
          "FTS", "FTS + semantic 0.42", etc.)
        - `_stages_tried` (internal: list of stages that ran)
        - `_stages_hit` (internal: list of stages that produced this result)
        The LLM sees confidence + found_via; the format layer uses _stages_*
        for the transparency block.
        """
        stages_tried = []

        # Stage 1: Lexical retrieval (exact + symbol LIKE + FTS + snippet values)
        stages_tried.append("lexical")
        det_results = self.search(query, max_results=max_results * 2, near=near)

        has_path_match = any("path_match" in r.get("reason", []) for r in det_results)
        exact_count = sum(1 for r in det_results if r.get("exact_match"))
        has_lexical = any("lexical" in r.get("reason", []) for r in det_results)

        # Short-circuit: Stage 1 found enough OR embeddings disabled.
        # `len(det_results) >= 3` is the key threshold: if FTS/LIKE/path/lexical
        # found 3+ results, semantic adds latency (~50s first call for encoder
        # load + 28k-node embedding) without changing the answer meaningfully.
        # Semantic is reserved for genuinely sparse queries (<3 det results).
        if has_path_match or len(det_results) >= 3 or exact_count >= 1 or has_lexical or embedding_weight <= 0.0:
            for r in det_results[:max_results]:
                self._annotate_result(r, stages_tried, det_results, near)
            _vmsg("CRG ROUTER: query='%s' -> Stage 1 only (path=%s exact=%d lex=%s) -> %d results",
                  query[:50], has_path_match, exact_count, has_lexical, len(det_results[:max_results]))
            return det_results[:max_results]

        # Stage 2: Semantic fallback
        stages_tried.append("semantic")
        sem_results = self.semantic_search(query, max_results=max_results * 2)

        # If near is provided, filter semantic results too
        if near and sem_results:
            near_files_sem = self._bfs_files_for_symbol(near, self._NEAR_HOPS)
            if near_files_sem:
                sem_results = [r for r in sem_results if r.get("file_path") in near_files_sem]
            else:
                sem_results = []

        if not sem_results:
            # No semantic results — return Stage 1 with LOW confidence
            for r in det_results[:max_results]:
                self._annotate_result(r, stages_tried, det_results, near)
            if not det_results:
                return [{
                    "file_path": "", "name": "", "kind": "",
                    "score": 0.0, "confidence": "LOW",
                    "confidence_reason": "No exact symbol or file match found. Semantic retrieval also produced no candidates. Refine with a symbol name, filename, or feature description.",
                    "semantic_score": 0.0, "reason": ["no_match"],
                    "source": "crg", "mode": "search",
                    "line_start": 0, "line_end": 0,
                    "exact_match": False, "matched_terms": [],
                    "found_via": "none", "_stages_tried": stages_tried, "_stages_hit": [],
                }]
            _vmsg("CRG ROUTER: query='%s' -> Stage 1 only (no semantic results) -> %d results",
                  query[:50], len(det_results[:max_results]))
            return det_results[:max_results]

        # Merge Stage 1 + Stage 2 via RRF
        fts_weight = 1.0 - embedding_weight
        k = 30

        fts_rank = {}
        for i, r in enumerate(det_results):
            fp = r.get("file_path", "")
            if fp and fp not in fts_rank:
                fts_rank[fp] = i + 1

        sem_rank = {}
        for i, r in enumerate(sem_results):
            fp = r.get("file_path", "")
            if fp and fp not in sem_rank:
                sem_rank[fp] = i + 1

        # Build lookup merging both result lists
        lookup = {}
        for r in sem_results:
            fp = r.get("file_path", "")
            if fp:
                lookup[fp] = {
                    "semantic_score": r.get("score", 0),
                    "name": r.get("name", ""),
                    "kind": r.get("kind", ""),
                    "signature": r.get("signature", ""),
                    "exact_match": False,
                    "matched_terms": [],
                    "line_start": r.get("line_start", 0),
                    "line_end": r.get("line_end", 0),
                    "lexical_hit": None,
                }
        for r in det_results:
            fp = r.get("file_path", "")
            if fp:
                if fp not in lookup:
                    lookup[fp] = {
                        "semantic_score": 0.0,
                        "name": r.get("name", ""),
                        "kind": r.get("kind", ""),
                        "signature": r.get("signature", ""),
                        "exact_match": False,
                        "matched_terms": [],
                        "line_start": r.get("line_start", 0),
                        "line_end": r.get("line_end", 0),
                        "lexical_hit": r.get("lexical_hit"),
                    }
                lookup[fp]["exact_match"] = r.get("exact_match", False)
                lookup[fp]["matched_terms"] = r.get("matched_terms", [])
                lookup[fp]["fts_score"] = r.get("score", 0)
                if not lookup[fp].get("signature") and r.get("signature"):
                    lookup[fp]["signature"] = r.get("signature", "")
                if r.get("line_start") and not lookup[fp].get("line_start"):
                    lookup[fp]["line_start"] = r["line_start"]
                if r.get("line_end") and not lookup[fp].get("line_end"):
                    lookup[fp]["line_end"] = r["line_end"]
                if r.get("lexical_hit") and not lookup[fp].get("lexical_hit"):
                    lookup[fp]["lexical_hit"] = r.get("lexical_hit")

        all_fps = set(fts_rank.keys()) | set(sem_rank.keys())
        scored = []
        for fp in all_fps:
            rrf = 0.0
            if fp in fts_rank:
                rrf += fts_weight / (k + fts_rank[fp])
            if fp in sem_rank:
                rrf += embedding_weight / (k + sem_rank[fp])
            base = lookup.get(fp, {})
            entry = dict(base)
            entry["file_path"] = fp
            entry["score"] = round(rrf * 1000, 1)
            entry["mode"] = "hybrid" if fp in fts_rank and fp in sem_rank else base.get("mode", "hybrid")
            reasons = set(base.get("reason", []))
            if fp in fts_rank:
                reasons.add("rrf_fts")
            if fp in sem_rank:
                reasons.add("rrf_semantic")
            entry["reason"] = sorted(reasons)
            in_both = fp in fts_rank and fp in sem_rank
            sem_score = base.get("semantic_score", 0.0)
            exact = base.get("exact_match", False)
            matched = base.get("matched_terms", [])
            is_lex = bool(base.get("lexical_hit"))
            entry["semantic_score"] = sem_score
            entry["exact_match"] = exact
            entry["matched_terms"] = matched
            entry["confidence"] = self._compute_confidence_v2(in_both, exact, sem_score, len(matched), is_lex)
            entry["confidence_reason"] = self._confidence_reason_v2(in_both, exact, sem_score, matched, is_lex, base.get("lexical_hit"))
            entry["found_via"] = self._found_via(in_both, exact, sem_score, is_lex, base.get("lexical_hit"))
            entry["_stages_tried"] = list(stages_tried)
            stages_hit = []
            if fp in fts_rank:
                stages_hit.append("lexical")
            if fp in sem_rank:
                stages_hit.append("semantic")
            entry["_stages_hit"] = stages_hit
            scored.append(entry)

        ranked = sorted(scored, key=lambda x: -x["score"])

        if ranked:
            top_score = ranked[0]["score"]
            cutoff = top_score * 0.5
            ranked = [r for r in ranked if r["score"] >= cutoff]

        results = ranked[:max_results]
        # If all results are LOW confidence, append guidance
        if results and all(r.get("confidence") == "LOW" for r in results):
            results.append({
                "file_path": "", "name": "", "kind": "",
                "score": 0.0, "confidence": "LOW",
                "confidence_reason": "No exact match found. Refine with a symbol name, filename, or feature description.",
                "semantic_score": 0.0, "reason": ["low_confidence_guidance"],
                "source": "crg", "mode": "search",
                "line_start": 0, "line_end": 0,
                "exact_match": False, "matched_terms": [],
                "found_via": "none", "_stages_tried": stages_tried, "_stages_hit": [],
            })

        _vmsg("CRG ROUTER: query='%s' -> Stage 1+2 (det=%d sem=%d) -> %d results",
              query[:50], len(det_results), len(sem_results), len(results))
        return results

    def _annotate_result(self, r: dict, stages_tried: list, det_results: list, near: str):
        """Annotate a Stage-1-only result with confidence, found_via, stage trace."""
        is_path = "path_match" in r.get("reason", [])
        is_exact = r.get("exact_match", False)
        is_near_unresolved = "near_unresolved" in r.get("reason", [])
        is_lexical = "lexical" in r.get("reason", [])
        lex = r.get("lexical_hit") or {}
        if is_near_unresolved:
            r["confidence"] = "LOW"
            r["confidence_reason"] = "near= not resolved; showing unfiltered results"
            r["found_via"] = "near= unresolved"
        elif is_lexical and not is_exact:
            r["confidence"] = "MEDIUM"
            r["confidence_reason"] = (f"lexical ({lex.get('term','?')} in {lex.get('name','?')}) "
                                      "— value found in source, not a graph node")
            r["found_via"] = f"lexical ({lex.get('term','?')} in {lex.get('name','?')})"
        else:
            r["confidence"] = "HIGH" if (is_path or is_exact) else "MEDIUM"
            r["confidence_reason"] = ("path match" if is_path else
                                      ("exact match" if is_exact else "FTS match"))
            r["found_via"] = ("path match" if is_path else
                              ("exact symbol" if is_exact else "FTS"))
        r["semantic_score"] = 0.0
        r["_stages_tried"] = list(stages_tried)
        r["_stages_hit"] = ["lexical"]

    @staticmethod
    def _compute_confidence_v2(in_both: bool, exact_match: bool, semantic_score: float,
                               num_matched_terms: int, is_lexical: bool) -> str:
        """Confidence with lexical factor.
        HIGH: hybrid + exact + strong semantic, OR exact match (any stage).
        MEDIUM: hybrid without exact, OR lexical-only, OR FTS-only exact, OR semantic-only strong.
        LOW: semantic-only weak, or FTS-only fuzzy.
        """
        if in_both and exact_match and semantic_score >= 0.5:
            return "HIGH"
        if in_both and (exact_match or semantic_score >= 0.4):
            return "MEDIUM"
        if not in_both and exact_match:
            return "MEDIUM"
        if not in_both and semantic_score >= 0.5:
            return "MEDIUM"
        if is_lexical and not in_both:
            return "MEDIUM"
        return "LOW"

    @staticmethod
    def _confidence_reason_v2(in_both: bool, exact_match: bool, semantic_score: float,
                              matched_terms, is_lexical: bool, lexical_hit) -> str:
        parts = []
        if exact_match:
            parts.append("exact match")
        if is_lexical and lexical_hit:
            parts.append(f"lexical ({lexical_hit.get('term','?')} in {lexical_hit.get('name','?')})")
        if in_both:
            parts.append(f"hybrid (FTS + semantic {semantic_score:.2f})")
        elif semantic_score > 0:
            parts.append(f"semantic only ({semantic_score:.2f})")
        else:
            parts.append("FTS only")
        if matched_terms:
            parts.append(f"matched: {', '.join(matched_terms[:3])}")
        return " + ".join(parts)

    @staticmethod
    def _found_via(in_both: bool, exact_match: bool, semantic_score: float,
                   is_lexical: bool, lexical_hit) -> str:
        """Consolidated 'found via' string for transparency."""
        via_parts = []
        if exact_match:
            via_parts.append("exact symbol")
        if is_lexical and lexical_hit:
            via_parts.append(f"lexical ({lexical_hit.get('term','?')} in {lexical_hit.get('name','?')})")
        if not via_parts:
            via_parts.append("FTS")
        if in_both:
            via_parts.append(f"semantic {semantic_score:.2f}")
        return " + ".join(via_parts)

    @staticmethod
    def _compute_confidence(in_both: bool, exact_match: bool, semantic_score: float, num_matched_terms: int) -> str:
        """HIGH: hybrid + exact match + strong semantic (>=0.5).
        MEDIUM: hybrid without exact (but decent semantic), OR FTS-only exact, OR semantic-only strong.
        LOW: semantic-only weak, or FTS-only fuzzy."""
        if in_both and exact_match and semantic_score >= 0.5:
            return "HIGH"
        if in_both and (exact_match or semantic_score >= 0.4):
            return "MEDIUM"
        if not in_both and exact_match:
            return "MEDIUM"
        if not in_both and semantic_score >= 0.5:
            return "MEDIUM"
        return "LOW"

    @staticmethod
    def _confidence_reason(in_both: bool, exact_match: bool, semantic_score: float, matched_terms) -> str:
        parts = []
        if exact_match:
            parts.append("exact match")
        if in_both:
            parts.append(f"hybrid (FTS + semantic {semantic_score:.2f})")
        elif semantic_score > 0:
            parts.append(f"semantic only ({semantic_score:.2f})")
        else:
            parts.append("FTS only")
        if matched_terms:
            parts.append(f"matched: {', '.join(matched_terms[:3])}")
        return " + ".join(parts)

    # ── Mode 1c: Multi-hop graph traversal ────────────────────────

    _ADJACENCY_CACHE = {}  # db_path -> {adj: dict, node_lookup: dict}

    def _build_adjacency(self):
        """Build and cache bidirectional adjacency from CRG edges.

        For 140k edges: ~20MB in RAM, built once per project.
        """
        cache_key = self._db_path
        cached = CRGProvider._ADJACENCY_CACHE.get(cache_key)
        if cached is not None:
            return cached

        conn = self._get_conn()
        adj = defaultdict(list)
        node_lookup = {}
        qname_lookup = {}

        try:
            nodes = conn.execute("SELECT id, name, kind, file_path, qualified_name FROM nodes").fetchall()
            for n in nodes:
                info = {
                    "name": n["name"], "kind": n["kind"] or "",
                    "file_path": self._normalize_path(n["file_path"]) if n["file_path"] else "",
                    "qualified_name": n["qualified_name"] or n["name"],
                }
                node_lookup[n["id"]] = info
                qname = n["qualified_name"] or n["name"]
                if qname:
                    qname_lookup[qname] = info

            edges = conn.execute("SELECT source_qualified, target_qualified, kind FROM edges").fetchall()
            for e in edges:
                src = e["source_qualified"]
                tgt = e["target_qualified"]
                ek = e["kind"] or "link"
                if src and tgt:
                    adj[src].append((tgt, ek))
                    adj[tgt].append((src, ek))
        except Exception as e:
            log.warning("Adjacency build failed: %s", e)
            CRGProvider._ADJACENCY_CACHE[cache_key] = None
            return None

        result = {"adj": dict(adj), "node_lookup": node_lookup, "qname_lookup": qname_lookup}
        CRGProvider._ADJACENCY_CACHE[cache_key] = result
        _vmsg("CRG ADJACENCY: built for %s, %d nodes, %d edges", cache_key, len(node_lookup), len(adj))
        return result

    def traverse(self, target: str, max_hops: int = 2, max_nodes: int = 30, max_tokens: int = 400) -> dict:
        """Multi-hop BFS traversal from target node with token budget.

        Returns: {nodes: [{name, file, kind, depth, degree}], edges: [{source, target, type}],
                  stats: {hops, nodes, edges, est_tokens}}
        """
        if not target:
            return {"nodes": [], "edges": [], "stats": {"hops": 0, "nodes": 0, "edges": 0, "est_tokens": 0}}

        adj_data = self._build_adjacency()
        if adj_data is None:
            return {"nodes": [], "edges": [], "stats": {"hops": 0, "nodes": 0, "edges": 0, "est_tokens": 0}}

        adj = adj_data["adj"]
        node_lookup = adj_data["node_lookup"]
        qname_lookup = adj_data.get("qname_lookup", {})

        target_lower = target.lower()
        start_qnames = []
        # Pass 1: exact name match — prefer non-test, non-File, real implementation nodes
        for nid, ninfo in node_lookup.items():
            if ninfo["name"].lower() == target_lower or (ninfo["qualified_name"] and target_lower in ninfo["qualified_name"].lower()):
                fp = ninfo.get("file_path", "")
                if _is_junk_path(fp) or _is_test_path(fp):
                    continue
                if ninfo.get("kind") in ("Function", "Class", "Method"):
                    start_qnames.append(ninfo["qualified_name"])
                    break
        # Pass 2: exact name match — accept any non-test node
        if not start_qnames:
            for nid, ninfo in node_lookup.items():
                if ninfo["name"].lower() == target_lower or (ninfo["qualified_name"] and target_lower in ninfo["qualified_name"].lower()):
                    fp = ninfo.get("file_path", "")
                    if _is_junk_path(fp) or _is_test_path(fp):
                        continue
                    start_qnames.append(ninfo["qualified_name"])
                    break
        # Pass 3: substring match — non-test only
        if not start_qnames:
            for nid, ninfo in node_lookup.items():
                if target_lower in ninfo["name"].lower():
                    fp = ninfo.get("file_path", "")
                    if _is_junk_path(fp) or _is_test_path(fp):
                        continue
                    start_qnames.append(ninfo["qualified_name"])
                    break

        if not start_qnames:
            return {"nodes": [], "edges": [], "stats": {"hops": 0, "nodes": 0, "edges": 0, "est_tokens": 0}}

        visited = set()
        edges_result = []
        nodes_result = []
        queue = deque()

        for sq in start_qnames:
            if sq not in visited:
                visited.add(sq)
                ni = qname_lookup.get(sq, {})
                fp = ni.get("file_path", "")
                if ni and not _is_junk_path(fp) and not _is_test_path(fp):
                    nodes_result.append({
                        "name": ni.get("name", sq), "file": fp,
                        "kind": ni.get("kind", ""), "depth": 0,
                        "degree": len(adj.get(sq, [])),
                    })
                    queue.append((sq, 0))

        est_tokens = len(nodes_result) * 15
        current_hop = 0

        while queue and len(nodes_result) < max_nodes and est_tokens < max_tokens:
            current, depth = queue.popleft()
            if depth >= max_hops:
                continue
            neighbors = adj.get(current, [])
            neighbors_sorted = sorted(neighbors, key=lambda x: len(adj.get(x[0], [])), reverse=True)

            for neighbor_qname, edge_type in neighbors_sorted:
                if neighbor_qname in visited:
                    continue
                if len(nodes_result) >= max_nodes or est_tokens >= max_tokens:
                    break
                visited.add(neighbor_qname)
                ni = qname_lookup.get(neighbor_qname, {})
                fp = ni.get("file_path", "")
                if _is_junk_path(fp) or _is_test_path(fp):
                    continue
                nodes_result.append({
                    "name": ni.get("name", neighbor_qname), "file": fp,
                    "kind": ni.get("kind", ""), "depth": depth + 1,
                    "degree": len(adj.get(neighbor_qname, [])),
                })
                edges_result.append({
                    "source": qname_lookup.get(current, {}).get("name", current),
                    "target": ni.get("name", neighbor_qname),
                    "type": edge_type,
                })
                est_tokens += 15
                queue.append((neighbor_qname, depth + 1))

        stats = {"hops": max(n["depth"] for n in nodes_result) if nodes_result else 0,
                 "nodes": len(nodes_result), "edges": len(edges_result), "est_tokens": est_tokens}
        _vmsg("CRG TRAVERSE: target='%s' hops=%d -> %d nodes, %d edges (%d est tokens)",
              target[:30], max_hops, len(nodes_result), len(edges_result), est_tokens)
        return {"nodes": nodes_result, "edges": edges_result, "stats": stats}

    # ── Mode 1d: Source code snippets ─────────────────────────────

    def get_snippets(self, node_names: list[str], max_chars: int = 500) -> dict:
        """Fetch source code snippets for given node names from CRG DB.

        Returns: {node_name: {snippet, file_path, line_start, line_end}}
        """
        if not node_names:
            return {}
        conn = self._get_conn()
        result = {}
        placeholders = ",".join("?" * min(len(node_names), 50))
        query_names = node_names[:50]

        try:
            rows = conn.execute(
                f"SELECT name, qualified_name, file_path, line_start, line_end FROM nodes "
                f"WHERE name IN ({placeholders}) AND file_path IS NOT NULL "
                f"ORDER BY LENGTH(name) ASC LIMIT 50",
                query_names
            ).fetchall()

            for r in rows:
                name = r["name"]
                if name in result:
                    continue
                fp = self._normalize_path(r["file_path"])
                if not fp or _is_junk_path(fp):
                    continue
                snippet = ""
                try:
                    if self._snippet_schema == "v2":
                        qname = r["qualified_name"] or f"{name}|{r['file_path']}"
                        snip_row = conn.execute(
                            "SELECT snippet FROM node_snippets WHERE qualified_name = ?",
                            (qname,)
                        ).fetchone()
                    else:
                        # v1 schema: node_snippets only has (node_name, snippet).
                        # No file_path column — use bare node_name lookup.
                        snip_row = conn.execute(
                            "SELECT snippet FROM node_snippets WHERE node_name = ?",
                            (name,)
                        ).fetchone()
                    if snip_row and snip_row["snippet"]:
                        snippet = snip_row["snippet"][:max_chars]
                except Exception:
                    pass
                result[name] = {
                    "snippet": snippet,
                    "file_path": fp,
                    "line_start": r["line_start"] or 0,
                    "line_end": r["line_end"] or 0,
                }
        except Exception as e:
            log.warning("get_snippets failed: %s", e)

        return result

    # ── Mode 1e: Rationale/doc nodes from graphify ────────────────

    def get_rationale(self, node_name: str) -> list[dict]:
        """Find rationale/doc nodes connected to a symbol in graphify data.

        Returns: [{text, confidence, source_file}]
        """
        gf = self.proj.get("graphify_data")
        if not gf:
            return []

        nodes = gf.get("nodes", [])
        links = gf.get("links", [])
        name_lower = node_name.lower()

        matched_id = None
        for n in nodes:
            for key in (n.get("id"), n.get("label"), n.get("qualified_name")):
                if key and key.lower() == name_lower:
                    matched_id = n.get("id") or n.get("label")
                    break
            if matched_id:
                break
        if not matched_id:
            for n in nodes:
                label = (n.get("label") or "").lower()
                if name_lower in label:
                    matched_id = n.get("id") or n.get("label")
                    break

        if not matched_id:
            return []

        rationale_nodes = {n.get("id") or n.get("label"): n for n in nodes if n.get("file_type") == "rationale"}
        if not rationale_nodes:
            return []

        result = []
        for l in links:
            src = l.get("source") or l.get("from")
            tgt = l.get("target") or l.get("to")
            rel = l.get("type") or l.get("kind") or ""
            conf = l.get("confidence", "")

            connected_rationale = None
            if src == matched_id and rel == "rationale_for" and tgt in rationale_nodes:
                connected_rationale = rationale_nodes[tgt]
            elif tgt == matched_id and rel == "rationale_for" and src in rationale_nodes:
                connected_rationale = rationale_nodes[src]

            if connected_rationale:
                text = connected_rationale.get("label") or connected_rationale.get("id") or ""
                if text:
                    result.append({
                        "text": text[:500],
                        "confidence": conf,
                        "source_file": connected_rationale.get("source_file", ""),
                    })

        _vmsg("CRG RATIONALE: target='%s' -> %d notes", node_name[:30], len(result))
        return result

    def architecture(self) -> list[dict]:
        """Get community structure with summaries for architecture context.

        Returns: [{name, purpose, key_symbols, risk, size, dominant_language, files, source, mode}]
        """
        conn = self._get_conn()
        results = []
        try:
            communities = conn.execute(
                "SELECT c.id, c.name, c.size, c.dominant_language, c.description, "
                "c.cohesion, c.level, "
                "cs.purpose, cs.key_symbols, cs.risk "
                "FROM communities c "
                "LEFT JOIN community_summaries cs ON c.id = cs.community_id "
                "WHERE c.size > 2 "
                "ORDER BY c.size DESC"
            ).fetchall()

            for c in communities:
                # Get representative files (top 5 by degree)
                rep_files = conn.execute(
                    "SELECT DISTINCT n.file_path, n.name, n.kind "
                    "FROM nodes n "
                    "WHERE n.community_id = ? AND n.kind IN ('Function', 'Class', 'File') "
                    "ORDER BY n.line_end - n.line_start DESC LIMIT 5",
                    (c["id"],)
                ).fetchall()
                files = [self._normalize_path(r["file_path"]) for r in rep_files if r["file_path"]]

                # Parse key_symbols JSON
                key_symbols = []
                try:
                    key_symbols = json.loads(c["key_symbols"] or "[]")
                    if isinstance(key_symbols, list):
                        key_symbols = key_symbols[:10]
                except (json.JSONDecodeError, TypeError):
                    pass

                results.append({
                    "name": c["name"] or f"community-{c['id']}",
                    "purpose": c["purpose"] or "",
                    "key_symbols": key_symbols,
                    "risk": c["risk"] or "unknown",
                    "size": c["size"] or 0,
                    "dominant_language": c["dominant_language"] or "",
                    "cohesion": round(c["cohesion"] or 0, 3),
                    "files": files,
                    "source": "crg",
                    "mode": "architecture",
                })
        except Exception as e:
            log.warning("CRG architecture failed: %s", e)
        _vmsg("CRG ARCHITECTURE: %d communities", len(results))
        return results

    # ── Mode 3: Impact (exhaustive blast-radius over ALL edges) ────

    # Edge kinds filtered per change type.
    # add-value: type-position uses break (REFERENCES/IMPORTS_FROM); call sites don't.
    # rename: callers break (CALLS/IMPORTS_FROM).
    # remove: everything breaks.
    # full: current exhaustive behavior (all kinds, all depths).
    _CHANGE_EDGE_FILTERS = {
        "add-value": ("REFERENCES", "IMPORTS_FROM", "INHERITS", "CONTAINS"),
        "rename": ("CALLS", "IMPORTS_FROM"),
        "remove": None,        # None = all kinds
        "full": None,
    }

    # Exhaustive-usage patterns scanned in node_snippets to tag breaks vs safe.
    # These appear in actual source lines (node_snippets has verbatim code, ≤500 chars).
    _BREAKS_PATTERNS = (
        "Record<", "[K in ", "switch (", "switch(", "Object.keys(",
        ".map(", ".reduce(", "satisfies ", " as const", "Partial<", "Pick<",
        "Omit<", "keyof ", "enum ", "values(", "entries(",
    )

    def impact(self, target: str, change: str = "add-value",
               offset: int = 0, max_tokens: int = 1500) -> list[dict]:
        """Blast-radius analysis with change-type edge filtering + token-budget pagination.

        change="add-value" (default): narrow — only type-position users (REFERENCES,
          IMPORTS_FROM, INHERITS). ~6-30 files for most repos. Use for adding an enum
          value, a new optional property, a new type variant.
        change="rename": callers + importers (CALLS, IMPORTS_FROM).
        change="remove": all edge kinds, all depths — removing a symbol breaks everything.
        change="full": exhaustive — all edges, unlimited depth (the original behavior).
          Use for repo-wide refactors where you truly need every transitive dependent.

        Dynamic depth: computed from node count. Small repos (<2k nodes) get depth 2;
        larger repos get depth 1 (narrow). change="full" always unlimited.

        offset: skip first N results (pagination). The full result list is computed;
        only the DISPLAY is paginated to stay within max_tokens. Callers can fetch
        more pages with impact(target, offset=N).

        max_tokens: display budget (default 1500; 4000 for full mode). Traversal is
        never truncated — only output lines are capped.

        Returns: [{file_path, depth, symbols, edge_types, sources, breaks, pattern,
                   is_test, mode, total_count, shown_count, has_more}]
        """
        if not target:
            return []
        conn = self._get_conn()
        target_lower = target.lower()

        change = change or "add-value"
        allowed_kinds = self._CHANGE_EDGE_FILTERS.get(change, self._CHANGE_EDGE_FILTERS["add-value"])

        # ── Dynamic depth limit based on repo size ──────────────────
        if change == "full":
            depth_max = 0  # unlimited
        else:
            try:
                node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            except Exception:
                node_count = 0
            if node_count < 2000:
                depth_max = 2
            else:
                depth_max = 1
            _vmsg("CRG IMPACT: node_count=%d -> depth_max=%d (change=%s)", node_count, depth_max, change)

        # ── Find target nodes ────────────────────────────────────────
        target_nodes = conn.execute(
            "SELECT id, name, qualified_name, file_path FROM nodes "
            "WHERE LOWER(name) = ? OR LOWER(qualified_name) LIKE ? "
            "LIMIT 10",
            (target_lower, f"%{target_lower}%")
        ).fetchall()

        if not target_nodes:
            target_nodes = conn.execute(
                "SELECT n.id, n.name, n.qualified_name, n.file_path "
                "FROM nodes_fts f JOIN nodes n ON f.rowid = n.id "
                "WHERE nodes_fts MATCH ? LIMIT 10",
                (f'"{target}"',)
            ).fetchall()

        if not target_nodes:
            words = [w for w in re.split(r'[\s_\-\.]+', target_lower) if len(w) > 2 and w not in
                     ("the", "and", "for", "that", "this", "with", "from", "what", "break", "change",
                      "modify", "update", "function", "method", "class", "module", "file", "code")]
            if words:
                for word in words:
                    try:
                        rows = conn.execute(
                            "SELECT n.id, n.name, n.qualified_name, n.file_path "
                            "FROM nodes_fts f JOIN nodes n ON f.rowid = n.id "
                            "WHERE nodes_fts MATCH ? LIMIT 10",
                            (f'"{word}"',)
                        ).fetchall()
                        target_nodes.extend(rows)
                    except Exception:
                        pass
                seen_ids = set()
                deduped = []
                for n in target_nodes:
                    if n["id"] not in seen_ids:
                        seen_ids.add(n["id"])
                        deduped.append(n)
                target_nodes = deduped

        if not target_nodes:
            _vmsg("CRG IMPACT: target '%s' not found", target)
            return []

        # Collect target info
        target_qnames = set()
        target_names = set()
        target_files = set()
        for n in target_nodes:
            if n["qualified_name"]:
                target_qnames.add(n["qualified_name"])
            if n["name"]:
                target_names.add(n["name"].lower())
            if n["file_path"]:
                target_files.add(self._normalize_path(n["file_path"]))

        # ── BFS over CRG edges with edge-kind filter + depth limit ──
        visited_qnames = set()
        file_data = defaultdict(lambda: {
            "depth": 99, "symbols": set(), "edge_types": set(),
            "sources": set(), "is_test": False,
        })
        frontier = set(target_qnames)
        depth = 0

        while frontier and (depth_max == 0 or depth < depth_max):
            depth += 1
            next_frontier = set()
            for qname in frontier:
                if qname in visited_qnames:
                    continue
                visited_qnames.add(qname)

                # Edges where this qname is the target (callers/dependents)
                try:
                    callers = conn.execute(
                        "SELECT DISTINCT e.source_qualified, e.kind, n.file_path, n.name "
                        "FROM edges e "
                        "LEFT JOIN nodes n ON n.qualified_name = e.source_qualified "
                        "WHERE e.target_qualified = ?",
                        (qname,)
                    ).fetchall()
                    for c in callers:
                        ek = c["kind"] or "link"
                        if allowed_kinds is not None and ek not in allowed_kinds:
                            # Edge kind filtered out — don't record file, but still
                            # traverse if we're going deeper (so we don't miss chains).
                            if c["source_qualified"]:
                                next_frontier.add(c["source_qualified"])
                            continue
                        fp = self._normalize_path(c["file_path"]) if c["file_path"] else ""
                        if fp and not _is_junk_path(fp):
                            entry = file_data[fp]
                            entry["depth"] = min(entry["depth"], depth)
                            if c["name"]:
                                entry["symbols"].add(c["name"])
                            entry["edge_types"].add(ek)
                            entry["sources"].add("crg")
                            entry["is_test"] = entry["is_test"] or _is_test_path(fp)
                        if c["source_qualified"]:
                            next_frontier.add(c["source_qualified"])
                except Exception as e:
                    log.warning("CRG impact callers query failed: %s", e)

                # Edges where this qname is the source (callees/dependencies)
                try:
                    callees = conn.execute(
                        "SELECT DISTINCT e.target_qualified, e.kind, n.file_path, n.name "
                        "FROM edges e "
                        "LEFT JOIN nodes n ON n.qualified_name = e.target_qualified "
                        "WHERE e.source_qualified = ?",
                        (qname,)
                    ).fetchall()
                    for c in callees:
                        ek = c["kind"] or "link"
                        if allowed_kinds is not None and ek not in allowed_kinds:
                            if c["target_qualified"]:
                                next_frontier.add(c["target_qualified"])
                            continue
                        fp = self._normalize_path(c["file_path"]) if c["file_path"] else ""
                        if fp and not _is_junk_path(fp):
                            entry = file_data[fp]
                            entry["depth"] = min(entry["depth"], depth)
                            if c["name"]:
                                entry["symbols"].add(c["name"])
                            entry["edge_types"].add(ek)
                            entry["sources"].add("crg")
                            entry["is_test"] = entry["is_test"] or _is_test_path(fp)
                        if c["target_qualified"]:
                            next_frontier.add(c["target_qualified"])
                except Exception as e:
                    log.warning("CRG impact callees query failed: %s", e)

            frontier = next_frontier - visited_qnames

        # ── Graphify links (second data source) ─────────────────────
        # Only traverse graphify for full/remove; for narrow modes it adds noise.
        if change in ("full", "remove"):
            gf = self.proj.get("graphify_data") or {}
            gf_nodes = gf.get("nodes", [])
            gf_links = gf.get("links", [])

            gf_node_lookup = {}
            for n in gf_nodes:
                nid = n.get("id") or n.get("label")
                if nid:
                    gf_node_lookup[nid] = n
                    gf_node_lookup[nid.lower()] = n

            gf_target_ids = set()
            for n in gf_nodes:
                for key in (n.get("id"), n.get("label"), n.get("qualified_name")):
                    if key and key.lower() in target_names:
                        gf_target_ids.add(n.get("id") or n.get("label"))
                        break
                    if key and target_lower in key.lower():
                        gf_target_ids.add(n.get("id") or n.get("label"))
                        break

            if gf_target_ids:
                gf_adj = defaultdict(list)
                for l in gf_links:
                    src = l.get("source") or l.get("from")
                    tgt = l.get("target") or l.get("to")
                    edge_type = l.get("type") or l.get("kind") or "link"
                    if src and tgt:
                        gf_adj[src].append((tgt, edge_type))
                        gf_adj[tgt].append((src, edge_type))

                gf_visited = set()
                gf_frontier = set(gf_target_ids)
                gf_depth = 0
                gf_depth_max = 0 if change == "full" else 1
                while gf_frontier and (gf_depth_max == 0 or gf_depth < gf_depth_max):
                    gf_depth += 1
                    gf_next = set()
                    for nid in gf_frontier:
                        if nid in gf_visited:
                            continue
                        gf_visited.add(nid)
                        for connected_id, edge_type in gf_adj.get(nid, []):
                            if connected_id not in gf_visited:
                                gf_next.add(connected_id)
                                cn = gf_node_lookup.get(connected_id, {})
                                fp = cn.get("source_file", "")
                                if fp:
                                    fp_norm = self._normalize_path(fp)
                                    if fp_norm and not _is_junk_path(fp_norm):
                                        entry = file_data[fp_norm]
                                        entry["depth"] = min(entry["depth"], gf_depth)
                                        if cn.get("label"):
                                            entry["symbols"].add(cn["label"])
                                        entry["edge_types"].add(edge_type)
                                        entry["sources"].add("graphify")
                                        entry["is_test"] = entry["is_test"] or _is_test_path(fp_norm)
                    gf_frontier = gf_next - gf_visited

        # Add target files themselves (depth 0)
        for fp in target_files:
            if not _is_junk_path(fp):
                entry = file_data[fp]
                entry["depth"] = 0
                entry["symbols"].add(target)
                entry["edge_types"].add("definition")
                entry["sources"].add("crg")
                entry["is_test"] = _is_test_path(fp)

        # ── breaks/safe tagging via node_snippets (depth-1 only) ────
        # Fetch snippets for symbols in depth-1 files and scan for exhaustive patterns.
        # Depth-2+ files are not scanned (too expensive) — they sort lower.
        depth1_files = {fp for fp, d in file_data.items() if d["depth"] == 1}
        if depth1_files:
            # Collect all symbol names from depth-1 files for batch snippet lookup
            depth1_symbols = set()
            for fp in depth1_files:
                depth1_symbols.update(file_data[fp]["symbols"])
            snippet_map = {}
            if depth1_symbols:
                try:
                    # Batch in chunks of 500 to avoid SQLite MAX_VARIABLE_NUMBER
                    # limit (999 on older SQLite, 32766 on newer). Silent failure
                    # here means breaks/safe tagging is lost — not acceptable.
                    sym_list = sorted(depth1_symbols)
                    for i in range(0, len(sym_list), 500):
                        batch = sym_list[i:i + 500]
                        placeholders = ",".join("?" * len(batch))
                        snip_rows = conn.execute(
                            f"SELECT node_name, snippet FROM node_snippets "
                            f"WHERE node_name IN ({placeholders})",
                            tuple(batch)
                        ).fetchall()
                        for sr in snip_rows:
                            snippet_map[sr["node_name"]] = sr["snippet"] or ""
                except Exception as e:
                    log.warning("CRG impact snippet batch query failed: %s", e)

            for fp in depth1_files:
                entry = file_data[fp]
                found_pattern = ""
                for sym in entry["symbols"]:
                    snip = snippet_map.get(sym, "")
                    if not snip:
                        continue
                    for pat in self._BREAKS_PATTERNS:
                        if pat in snip:
                            found_pattern = pat.strip()
                            break
                    if found_pattern:
                        break
                entry["breaks"] = bool(found_pattern)
                entry["pattern"] = found_pattern

        # Set defaults for unscanned files
        for fp, data in file_data.items():
            data.setdefault("breaks", None)  # None = unscanned (depth 2+ or no snippet)
            data.setdefault("pattern", "")

        # ── Risk-priority sort ───────────────────────────────────────
        # tier 0: depth 0 (definition)
        # tier 1: depth 1 + breaks
        # tier 2: depth 1 + safe (breaks=False)
        # tier 3: depth 1 + unscanned (breaks=None)
        # tier 4: depth 2+ (unscanned)
        # tier 5: test files (always last within their depth)
        def _sort_key(item):
            fp, d = item
            if d["depth"] == 0:
                tier = 0
            elif d["depth"] == 1:
                if d["breaks"] is True:
                    tier = 1
                elif d["breaks"] is False:
                    tier = 2
                else:
                    tier = 3
            else:
                tier = 4
            if d["is_test"]:
                tier += 10  # push tests to the bottom of any tier
            return (tier, d["depth"], -len(d["symbols"]))

        sorted_files = sorted(file_data.items(), key=_sort_key)

        # ── Build full result list (not truncated — pagination is display-only) ──
        all_results = []
        for fp, data in sorted_files:
            all_results.append({
                "file_path": fp,
                "depth": data["depth"],
                "symbols": sorted(data["symbols"])[:10],
                "edge_types": sorted(data["edge_types"]),
                "sources": sorted(data["sources"]),
                "breaks": data["breaks"],
                "pattern": data["pattern"],
                "is_test": data["is_test"],
                "mode": "impact",
            })

        # Attach pagination metadata to every result dict so the dispatch
        # handler can emit the summary line. The list is NOT sliced here —
        # the caller (dispatch) handles offset/limit for display.
        total = len(all_results)
        for r in all_results:
            r["total_count"] = total
            r["has_more"] = True  # dispatch sets False for last page

        _vmsg("CRG IMPACT: target='%s' change=%s -> %d files (depth_max=%d, %d hops)",
              target[:40], change, total, depth_max, depth)
        return all_results

    # ── Mode 4: Execution flows ────────────────────────────────────

    def flows(self, target: str) -> list[dict]:
        """Find execution flows containing the target symbol.

        Returns: [{flow_name, criticality, node_count, file_count, files, path_nodes, source, mode}]
        """
        if not target:
            return []
        conn = self._get_conn()
        target_lower = target.lower()

        # Find nodes matching the target
        target_nodes = conn.execute(
            "SELECT id, name, file_path FROM nodes "
            "WHERE LOWER(name) LIKE ? OR LOWER(qualified_name) LIKE ? LIMIT 10",
            (f"%{target_lower}%", f"%{target_lower}%")
        ).fetchall()

        if not target_nodes:
            # FTS fallback
            try:
                target_nodes = conn.execute(
                    "SELECT n.id, n.name, n.file_path "
                    "FROM nodes_fts f JOIN nodes n ON f.rowid = n.id "
                    "WHERE nodes_fts MATCH ? LIMIT 10",
                    (f'"{target}"',)
                ).fetchall()
            except Exception:
                pass

        if not target_nodes:
            _vmsg("CRG FLOWS: target '%s' not found", target)
            return []

        target_node_ids = set(n["id"] for n in target_nodes)

        # Find flows whose path contains any target node
        results = []
        try:
            all_flows = conn.execute(
                "SELECT * FROM flows ORDER BY criticality DESC LIMIT 50"
            ).fetchall()

            for f in all_flows:
                path_json = f["path_json"] or "[]"
                try:
                    path_ids = json.loads(path_json)
                except (json.JSONDecodeError, TypeError):
                    path_ids = []

                if not any(pid in target_node_ids for pid in path_ids):
                    continue

                # Get file paths for all nodes in this flow
                if path_ids:
                    placeholders = ",".join("?" * len(path_ids[:30]))
                    flow_nodes = conn.execute(
                        f"SELECT DISTINCT file_path, name FROM nodes WHERE id IN ({placeholders})",
                        path_ids[:30]
                    ).fetchall()
                else:
                    flow_nodes = []

                files = [self._normalize_path(r["file_path"]) for r in flow_nodes if r["file_path"]]

                # Get entry point name
                entry_node = conn.execute(
                    "SELECT name FROM nodes WHERE id = ?",
                    (f["entry_point_id"],)
                ).fetchone()

                results.append({
                    "flow_name": f["name"] or "",
                    "entry_point": entry_node["name"] if entry_node else "",
                    "criticality": round(f["criticality"] or 0, 2),
                    "node_count": f["node_count"] or 0,
                    "file_count": f["file_count"] or 0,
                    "files": files,
                    "path_nodes": [n["name"] for n in flow_nodes[:10]],
                    "source": "crg",
                    "mode": "flows",
                })
        except Exception as e:
            log.warning("CRG flows query failed: %s", e)

        _vmsg("CRG FLOWS: target='%s' -> %d flows", target[:40], len(results))
        return results

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# ── Factory: get available providers for a project ────────────────

_PROVIDERS = []

def get_providers(proj: dict) -> list[IntelligenceProvider]:
    """Get all available intelligence providers for a project.

    Returns a list of initialized, available providers.
    Future providers (Nx, Semgrep, etc.) are added to the _PROVIDER_CLASSES list.
    """
    providers = []
    for ProviderClass in _PROVIDER_CLASSES:
        try:
            p = ProviderClass(proj)
            if p.is_available():
                providers.append(p)
            else:
                p.close()
        except Exception as e:
            log.warning("Provider %s init failed: %s", ProviderClass.name, e)
    if providers:
        _vmsg("INTELLIGENCE: %d providers available: %s", len(providers), [p.name for p in providers])
    return providers


_PROVIDER_CLASSES = [CRGProvider]  # Add future providers here: NxProvider, SemgrepProvider, etc.


# ── Helper: merge intelligence results into graphify ranked list ──

def merge_intelligence_results(
    graphify_ranked: list[dict],
    intel_results: list[dict],
    provider_name: str = "crg",
    max_results: int = 30,
) -> list[dict]:
    """Merge intelligence provider file results into graphify-ranked list.

    Same file in both → sum scores, merge metadata.
    New intelligence file → insert with graph_score=0, intel_score=intel score.
    Graphify-only file → intel_score=0, graph_score=existing score.

    Returns merged list sorted by score descending, capped at max_results.
    """
    if not intel_results:
        result = []
        for gr in graphify_ranked:
            entry = dict(gr)
            entry.setdefault("graph_score", entry.get("score", 0))
            entry.setdefault("intel_score", 0)
            entry.setdefault("matched_terms", [])
            result.append(entry)
        return result[:max_results]

    merged_map = {}

    # Index graphify entries
    for gr in graphify_ranked:
        fp = gr.get("file_path", "")
        if not fp:
            continue
        base_score = gr.get("score", 0)
        reasons = gr.get("reason", [])
        if isinstance(reasons, str):
            reasons = [reasons]
        merged_map[fp] = {
            "file_path": fp,
            "score": base_score,
            "graph_score": base_score,
            "intel_score": 0,
            "reason": list(reasons),
            "source": "graphify",
            "matched_terms": [],
        }

    # Merge intelligence entries
    for ir in intel_results:
        fp = ir.get("file_path", "")
        if not fp:
            continue
        intel_score = ir.get("score", 0)
        ir_reasons = ir.get("reason", [])
        if isinstance(ir_reasons, str):
            ir_reasons = [ir_reasons]
        ir_reasons = [f"{provider_name}:{r}" for r in ir_reasons]

        if fp in merged_map:
            entry = merged_map[fp]
            entry["intel_score"] = intel_score
            entry["score"] = entry["graph_score"] + intel_score
            for r in ir_reasons:
                if r not in entry["reason"]:
                    entry["reason"].append(r)
            entry["source"] = f"graphify+{provider_name}"
        else:
            merged_map[fp] = {
                "file_path": fp,
                "score": intel_score,
                "graph_score": 0,
                "intel_score": intel_score,
                "reason": list(ir_reasons),
                "source": f"{provider_name}",
                "matched_terms": ir.get("matched_terms", []),
            }

    merged = sorted(merged_map.values(), key=lambda x: -x["score"])
    return merged[:max_results]


# ── Helper: render intelligence metadata as context text ──────────

def render_intelligence_context(
    intel_results: list[dict],
    mode: str,
    max_chars: int = 1500,
) -> str:
    """Render intelligence provider results as a context text section.

    For architecture mode: renders community summaries.
    For flows mode: renders execution flow paths.
    For impact mode: renders blast-radius summary.
    For search mode: renders matched symbols.
    """
    if not intel_results:
        return ""

    lines = []
    if mode == "architecture":
        lines.append(f"\n## CRG Architecture: {len(intel_results)} communities")
        for c in intel_results[:10]:
            purpose = c.get("purpose", "")
            key_syms = c.get("key_symbols", [])
            key_str = ", ".join(key_syms[:5]) if key_syms else ""
            files = c.get("files", [])
            files_str = ", ".join(f"`{f}`" for f in files[:3]) if files else ""
            lines.append(f"### {c['name']} ({c.get('size',0)} nodes, {c.get('dominant_language','')})")
            if purpose:
                lines.append(f"  Purpose: {purpose}")
            if key_str:
                lines.append(f"  Key symbols: {key_str}")
            if files_str:
                lines.append(f"  Files: {files_str}")
            risk = c.get("risk", "unknown")
            if risk != "unknown":
                lines.append(f"  Risk: {risk}")
    elif mode == "flows":
        lines.append(f"\n## CRG Execution Flows: {len(intel_results)} matching flows")
        for f in intel_results[:5]:
            files = f.get("files", [])
            files_str = ", ".join(f"`{f2}`" for f2 in files[:5]) if files else ""
            path = f.get("path_nodes", [])
            path_str = " -> ".join(path[:8]) if path else ""
            lines.append(f"### Flow: {f['flow_name']} (criticality: {f.get('criticality',0)})")
            if path_str:
                lines.append(f"  Path: {path_str}")
            if files_str:
                lines.append(f"  Files: {files_str}")
    elif mode == "impact":
        lines.append(f"\n## CRG Impact Analysis: {len(intel_results)} affected files")
        for r in intel_results[:10]:
            reasons = ", ".join(r.get("reason", []))
            depth = r.get("depth", "?")
            lines.append(f"- `{r['file_path']}` (depth={depth}, score={r.get('score',0)}, {reasons})")
    elif mode == "search":
        lines.append(f"\n## CRG Symbol Search: {len(intel_results)} matches")
        for r in intel_results[:10]:
            name = r.get("name", "")
            kind = r.get("kind", "")
            terms = r.get("matched_terms", [])
            terms_str = ", ".join(terms[:3]) if terms else ""
            lines.append(f"- `{r['file_path']}` — {name} ({kind}) matched: {terms_str}")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...(truncated)"
    return text
