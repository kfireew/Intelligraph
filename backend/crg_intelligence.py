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
    return any(p in lower for p in _TEST_PATH_PATTERNS)


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

    def impact(self, target: str) -> list[dict]:
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
        return self._conn

    def _extract_repo_prefix(self) -> str:
        """Extract the repo root prefix for path normalization.

        Priority:
        1. proj["repo_dir"] — known, absolute, correct (when MCP runs with --repo-dir)
        2. Common prefix from DB paths — fallback for Docker/chat (no repo_dir)

        The returned prefix is forward-slashed and ends with /.
        """
        # 1. Known repo_dir — always correct, no guessing
        repo_dir = self.proj.get("repo_dir")
        if repo_dir:
            prefix = repo_dir.replace("\\", "/").rstrip("/") + "/"
            _vmsg("CRG INTELLIGENCE: repo prefix (from repo_dir) = %s", prefix)
            return prefix

        # 2. Fallback: common prefix from DB paths (for Docker/chat without repo_dir)
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
        # If nothing extracted (e.g. all stopwords), fall back to the raw query.
        terms = compounds + words
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

    # ── Mode 1: FTS5 search ────────────────────────────────────────

    def search(self, query: str, max_results: int = 20) -> list[dict]:
        """FTS5 search for symbols/files matching the query.

        A LIKE pre-pass catches camelCase / concatenated matches that FTS
        tokenization misses (e.g. "zik plane" → "ZikPlane"). FTS then adds
        tokenized matches. Results carry line_start/line_end so callers
        (MCP) can give the agent surgical read ranges instead of whole files.

        Returns: [{file_path, name, kind, signature, line_start, line_end,
                   community_id, score, exact_match, matched_terms, reason, source, mode}]
        """
        conn = self._get_conn()
        terms = self._extract_terms(query)
        if not terms:
            return []

        file_scores = defaultdict(lambda: {"score": 0.0, "names": [], "kinds": set(),
                                           "matched_terms": set(), "signatures": [],
                                           "exact_match": False,
                                           "line_start": 0, "line_end": 0})

        # Pass 0: LIKE pre-pass — catches camelCase / concatenated / underscore names.
        # "zik plane" → "zikplane" → matches "ZikPlane"
        # "hybrid_search" → tries both "hybrid_search" AND "hybridsearch"
        # FTS5 tokenizes on underscore so "hybrid_search" needs LIKE to match it.
        for term in terms:
            like_term = term.replace(" ", "").replace("_", "")
            # Try both the original term and the underscore-stripped version
            for try_term in [term, like_term]:
                if len(try_term) < 3:
                    continue
                try:
                    rows = conn.execute(
                        "SELECT name, kind, file_path, signature, community_id, "
                        "line_start, line_end FROM nodes "
                        "WHERE kind IN ('Function','Class','Method') "
                        "AND LOWER(name) LIKE ? "
                        "ORDER BY LENGTH(name) ASC LIMIT 10",
                        (f"%{try_term}%",)
                    ).fetchall()
                    for r in rows:
                        fp = self._normalize_path(r["file_path"])
                        if not fp or _is_junk_path(fp) or _is_test_path(fp):
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
            weight = 5.0 if i < 3 else 3.0  # earlier terms weighted higher
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

        # Boost files matching multiple terms
        for fp, data in file_scores.items():
            if len(data["matched_terms"]) >= 2:
                data["score"] *= 1.5

        results = []
        for fp, data in sorted(file_scores.items(), key=lambda x: -x[1]["score"])[:max_results]:
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
                "reason": ["crg_fts_match"],
                "source": "crg",
                "mode": "search",
            })
        _vmsg("CRG SEARCH: query='%s' terms=%s -> %d files", query[:50], terms[:5], len(results))
        return results

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

    def hybrid_search(self, query: str, max_results: int = 20, embedding_weight: float = 0.4) -> list[dict]:
        """Hybrid search using Reciprocal Rank Fusion (RRF).

        RRF is rank-invariant: it uses each system's ranking, not raw scores,
        so mismatched score scales don't corrupt the blend. Files appearing
        in both FTS and semantic results get two additive terms -> higher
        RRF score. Files in only one system get a single term -> lower score.

        After RRF ranking, an adaptive cutoff drops results below 50% of the
        top score — so specific queries return 2-3 files and broad queries
        return 5-8, instead of always returning max_results.

        Each result also carries a `confidence` (HIGH/MEDIUM/LOW) and
        `confidence_reason` string so callers can decide whether to fetch
        more context (snippets, full files) or trust the result.
        """
        fts_weight = 1.0 - embedding_weight
        fts_results = self.search(query, max_results=max_results * 2)

        if embedding_weight <= 0.0:
            ranked = sorted(fts_results, key=lambda x: -x.get("score", 0))
            if ranked:
                top = ranked[0].get("score", 0)
                ranked = [r for r in ranked if r.get("score", 0) >= top * 0.3]
            for r in ranked[:max_results]:
                r["confidence"] = "MEDIUM" if r.get("exact_match") else "LOW"
                r["confidence_reason"] = ("exact match + FTS" if r.get("exact_match") else "FTS only")
                r["semantic_score"] = 0.0
            return ranked[:max_results]

        sem_results = self.semantic_search(query, max_results=max_results * 2)
        if embedding_weight >= 1.0:
            ranked = sorted(sem_results, key=lambda x: -x.get("score", 0))
            if ranked:
                top = ranked[0].get("score", 0)
                ranked = [r for r in ranked if r.get("score", 0) >= top * 0.3]
            for r in ranked[:max_results]:
                r["confidence"] = "HIGH" if r.get("score", 0) >= 0.5 else "LOW"
                r["confidence_reason"] = f"semantic only ({r.get('score', 0):.2f})"
                r["semantic_score"] = r.get("score", 0)
            return ranked[:max_results]

        k = 30  # RRF constant — lower k = steeper drop-off (k=60 is too flat for 10-20 results)
        fts_rank = {}
        for i, r in enumerate(fts_results):
            fp = r.get("file_path", "")
            if fp and fp not in fts_rank:
                fts_rank[fp] = i + 1

        sem_rank = {}
        for i, r in enumerate(sem_results):
            fp = r.get("file_path", "")
            if fp and fp not in sem_rank:
                sem_rank[fp] = i + 1

        # Build lookup MERGING both result lists (not overwriting) so that
        # semantic_score, exact_match, signature, matched_terms, line_start/end all survive.
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
                }
        for r in fts_results:
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
                    }
                lookup[fp]["exact_match"] = r.get("exact_match", False)
                lookup[fp]["matched_terms"] = r.get("matched_terms", [])
                lookup[fp]["fts_score"] = r.get("score", 0)
                if not lookup[fp].get("signature") and r.get("signature"):
                    lookup[fp]["signature"] = r.get("signature", "")
                # Prefer FTS line numbers if semantic had 0
                if r.get("line_start") and not lookup[fp].get("line_start"):
                    lookup[fp]["line_start"] = r["line_start"]
                if r.get("line_end") and not lookup[fp].get("line_end"):
                    lookup[fp]["line_end"] = r["line_end"]

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
            # Confidence signals
            in_both = fp in fts_rank and fp in sem_rank
            sem_score = base.get("semantic_score", 0.0)
            exact = base.get("exact_match", False)
            matched = base.get("matched_terms", [])
            entry["semantic_score"] = sem_score
            entry["exact_match"] = exact
            entry["matched_terms"] = matched
            entry["confidence"] = self._compute_confidence(in_both, exact, sem_score, len(matched))
            entry["confidence_reason"] = self._confidence_reason(in_both, exact, sem_score, matched)
            scored.append(entry)

        ranked = sorted(scored, key=lambda x: -x["score"])

        if ranked:
            top_score = ranked[0]["score"]
            cutoff = top_score * 0.5
            ranked = [r for r in ranked if r["score"] >= cutoff]

        results = ranked[:max_results]
        _vmsg("CRG HYBRID(RRF): query='%s' ew=%.1f -> %d results (fts=%d sem=%d, cutoff=%.1f)",
              query[:50], embedding_weight, len(results), len(fts_results), len(sem_results),
              ranked[0]["score"] * 0.3 if ranked else 0)
        return results

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
                f"SELECT name, file_path, line_start, line_end FROM nodes "
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

    def impact(self, target: str) -> list[dict]:
        """Exhaustive BFS over ALL edge types to find the complete blast radius.

        This is a SAFETY operation, not context delivery. No caps, no token
        budget, no depth limit. Traverses both CRG edges AND graphify
        links until the frontier is empty. The graph is finite — BFS terminates.

        Returns: [{file_path, depth, symbols, edge_types, sources, mode}]
        """
        if not target:
            return []
        conn = self._get_conn()
        target_lower = target.lower()

        # Find target nodes by name match (same logic as before)
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

        # BFS over CRG edges — ALL edge kinds, no filter, no cap, no depth limit.
        # This is a safety operation — traverse until frontier is empty.
        # The graph is finite; BFS terminates.
        visited_qnames = set()
        file_data = defaultdict(lambda: {"depth": 99, "symbols": set(), "edge_types": set(), "sources": set()})
        frontier = set(target_qnames)
        depth = 0

        while frontier:
            depth += 1
            next_frontier = set()
            for qname in frontier:
                if qname in visited_qnames:
                    continue
                visited_qnames.add(qname)

                # Find ALL edges where this qname is the target (callers/dependents)
                try:
                    callers = conn.execute(
                        "SELECT DISTINCT e.source_qualified, e.kind, n.file_path, n.name "
                        "FROM edges e "
                        "LEFT JOIN nodes n ON n.qualified_name = e.source_qualified "
                        "WHERE e.target_qualified = ?",
                        (qname,)
                    ).fetchall()
                    for c in callers:
                        fp = self._normalize_path(c["file_path"]) if c["file_path"] else ""
                        if fp and not _is_junk_path(fp) and not _is_test_path(fp):
                            entry = file_data[fp]
                            entry["depth"] = min(entry["depth"], depth)
                            if c["name"]:
                                entry["symbols"].add(c["name"])
                            entry["edge_types"].add(c["kind"] or "link")
                            entry["sources"].add("crg")
                        if c["source_qualified"]:
                            next_frontier.add(c["source_qualified"])
                except Exception as e:
                    log.warning("CRG impact callers query failed: %s", e)

                # Find ALL edges where this qname is the source (callees/dependencies)
                try:
                    callees = conn.execute(
                        "SELECT DISTINCT e.target_qualified, e.kind, n.file_path, n.name "
                        "FROM edges e "
                        "LEFT JOIN nodes n ON n.qualified_name = e.target_qualified "
                        "WHERE e.source_qualified = ?",
                        (qname,)
                    ).fetchall()
                    for c in callees:
                        fp = self._normalize_path(c["file_path"]) if c["file_path"] else ""
                        if fp and not _is_junk_path(fp) and not _is_test_path(fp):
                            entry = file_data[fp]
                            entry["depth"] = min(entry["depth"], depth)
                            if c["name"]:
                                entry["symbols"].add(c["name"])
                            entry["edge_types"].add(c["kind"] or "link")
                            entry["sources"].add("crg")
                        if c["target_qualified"]:
                            next_frontier.add(c["target_qualified"])
                except Exception as e:
                    log.warning("CRG impact callees query failed: %s", e)

            frontier = next_frontier - visited_qnames

        # Also traverse graphify links (second data source)
        gf = self.proj.get("graphify_data") or {}
        gf_nodes = gf.get("nodes", [])
        gf_links = gf.get("links", [])

        # Build graphify node lookup
        gf_node_lookup = {}
        for n in gf_nodes:
            nid = n.get("id") or n.get("label")
            if nid:
                gf_node_lookup[nid] = n
                gf_node_lookup[nid.lower()] = n

        # Find target in graphify
        gf_target_ids = set()
        for n in gf_nodes:
            for key in (n.get("id"), n.get("label"), n.get("qualified_name")):
                if key and key.lower() in target_names:
                    gf_target_ids.add(n.get("id") or n.get("label"))
                    break
                if key and target_lower in key.lower():
                    gf_target_ids.add(n.get("id") or n.get("label"))
                    break

        # BFS over graphify links — ALL link types, no depth limit
        if gf_target_ids:
            gf_visited = set()
            gf_frontier = set(gf_target_ids)
            gf_depth = 0
            while gf_frontier:
                gf_depth += 1
                gf_next = set()
                for nid in gf_frontier:
                    if nid in gf_visited:
                        continue
                    gf_visited.add(nid)
                    for l in gf_links:
                        src = l.get("source") or l.get("from")
                        tgt = l.get("target") or l.get("to")
                        edge_type = l.get("type") or l.get("kind") or "link"
                        connected_id = None
                        if src == nid and tgt:
                            connected_id = tgt
                        elif tgt == nid and src:
                            connected_id = src
                        if connected_id and connected_id not in gf_visited:
                            gf_next.add(connected_id)
                            cn = gf_node_lookup.get(connected_id, {})
                            fp = cn.get("source_file", "")
                            if fp:
                                fp_norm = self._normalize_path(fp)
                                if fp_norm and not _is_junk_path(fp_norm) and not _is_test_path(fp_norm):
                                    entry = file_data[fp_norm]
                                    entry["depth"] = min(entry["depth"], gf_depth)
                                    if cn.get("label"):
                                        entry["symbols"].add(cn["label"])
                                    entry["edge_types"].add(edge_type)
                                    entry["sources"].add("graphify")
                gf_frontier = gf_next - gf_visited

        # Add target files themselves (depth 0)
        for fp in target_files:
            if not _is_junk_path(fp) and not _is_test_path(fp):
                entry = file_data[fp]
                entry["depth"] = 0
                entry["symbols"].add(target)
                entry["edge_types"].add("definition")
                entry["sources"].add("crg")

        results = []
        for fp, data in sorted(file_data.items(), key=lambda x: (x[1]["depth"], -len(x[1]["symbols"]))):
            results.append({
                "file_path": fp,
                "depth": data["depth"],
                "symbols": sorted(data["symbols"])[:10],
                "edge_types": sorted(data["edge_types"]),
                "sources": sorted(data["sources"]),
                "mode": "impact",
            })
        _vmsg("CRG IMPACT: target='%s' -> %d files (exhaustive, %d hops)", target[:40], len(results), depth)
        return results

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
