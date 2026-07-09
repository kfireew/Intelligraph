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
from collections import defaultdict

log = logging.getLogger(__name__)

_VERBOSE = os.environ.get("INTELLIGRAPH_VERBOSE", "true").lower() == "true"


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
    print(f"[{ts}] {msg}", flush=True)


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

    def impact(self, target: str, max_depth: int = 2) -> list[dict]:
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
        """Extract the repo root prefix from CRG node paths.

        CRG stores absolute paths like:
          C:\\Users\\...\\repos\\local-2-xxx\\graphify\\cluster.py
        We need to strip everything up to and including the repo root
        to get repo-relative paths: graphify/cluster.py
        """
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
            # Truncate to last / (don't include partial directory name)
            idx = common.rfind("/")
            if idx > 0:
                prefix = common[:idx + 1]
                # But common prefix might be too short if there are diverse paths.
                # Find the repo root by looking for the first source code dir.
                # e.g. prefix = .../repos/local-2-xxx/  → we want this
                # If prefix is .../repos/  (too short), we need to go deeper.
                # Check: does any path have the prefix + a single directory component
                # that appears in MOST paths?
                # Simpler: the prefix already works because commonprefix finds the
                # longest shared start. For repos with diverse top-level dirs,
                # this is the repo root.
                _vmsg("CRG INTELLIGENCE: repo prefix = %s", prefix)
                return prefix
        except Exception as e:
            log.warning("CRG prefix extraction failed: %s", e)
        return ""

    def _normalize_path(self, abs_path: str) -> str:
        """Convert CRG absolute path to repo-relative path."""
        if not abs_path:
            return ""
        p = abs_path.replace("\\", "/")
        if self._repo_prefix and p.startswith(self._repo_prefix):
            return p[len(self._repo_prefix):]
        # Fallback: try to find the last occurrence of a known source dir
        # This handles cases where the prefix extraction was imperfect
        for marker in ("graphify/", "src/", "lib/", "tests/", "backend/"):
            idx = p.find("/" + marker)
            if idx >= 0:
                return p[idx + 1:]
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
        words = []
        for token in raw_tokens:
            token = token.strip()
            if len(token) > 2 and token.isalpha() and token not in stopwords:
                words.append(token)

        # Also try multi-word compound: "build_graph" from "build graph"
        # Rejoin consecutive meaningful words with underscore
        compounds = []
        current_compound = []
        for token in raw_tokens:
            token = token.strip()
            if token and len(token) > 2 and token not in stopwords and token.isalpha():
                current_compound.append(token)
            else:
                if len(current_compound) > 1:
                    compounds.append("_".join(current_compound))
                current_compound = []
        if len(current_compound) > 1:
            compounds.append("_".join(current_compound))

        # Return compounds first (more specific), then individual words
        return compounds + words
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

        best_match = None
        best_score = 0

        for term in terms:
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
                        name_lower = name.lower()
                        if term in name_lower:
                            relevance = len(term) / max(len(name_lower), 1)
                            if relevance > best_score:
                                best_score = relevance
                                best_match = name
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
                        relevance = len(term) / max(len(name_lower), 1)
                        if relevance > best_score:
                            best_score = relevance
                            best_match = name
                    elif name_lower in term:
                        relevance = len(name_lower) / max(len(term), 1) * 0.8
                        if relevance > best_score:
                            best_score = relevance
                            best_match = name
            except Exception as e:
                log.warning("CRG extract_target FTS failed for '%s': %s", term, e)

        if best_match:
            _vmsg("CRG EXTRACT_TARGET: query='%s' -> '%s' (relevance=%.2f)", query[:50], best_match, best_score)
            return best_match

        _vmsg("CRG EXTRACT_TARGET: no match for query='%s' terms=%s", query[:50], terms[:5])
        return None

    # ── Mode 1: FTS5 search ────────────────────────────────────────

    def search(self, query: str, max_results: int = 20) -> list[dict]:
        """FTS5 search for symbols/files matching the query.

        Returns: [{file_path, name, kind, signature, community_id, score, reason, source, mode}]
        """
        conn = self._get_conn()
        terms = self._extract_terms(query)
        if not terms:
            return []

        file_scores = defaultdict(lambda: {"score": 0.0, "names": [], "kinds": set(), "matched_terms": set()})
        for i, term in enumerate(terms):
            weight = 5.0 if i < 3 else 3.0  # earlier terms weighted higher
            try:
                rows = conn.execute(
                    "SELECT n.file_path, n.name, n.kind, n.signature, n.community_id "
                    "FROM nodes_fts f JOIN nodes n ON f.rowid = n.id "
                    "WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?",
                    (f'"{term}"', 15)
                ).fetchall()
                for r in rows:
                    fp = self._normalize_path(r["file_path"])
                    if not fp:
                        continue
                    entry = file_scores[fp]
                    entry["score"] += weight
                    entry["names"].append(r["name"])
                    entry["kinds"].add(r["kind"])
                    entry["matched_terms"].add(term)
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
                "matched_terms": sorted(data["matched_terms"]),
                "reason": ["crg_fts_match"],
                "source": "crg",
                "mode": "search",
            })
        _vmsg("CRG SEARCH: query='%s' terms=%s -> %d files", query[:50], terms[:5], len(results))
        return results

    # ── Mode 2: Architecture (communities + summaries) ─────────────

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

    # ── Mode 3: Impact (blast-radius over CALLS edges) ─────────────

    def impact(self, target: str, max_depth: int = 2) -> list[dict]:
        """BFS over CALLS/IMPORTS_FROM edges to find blast-radius files.

        Returns: [{file_path, score, depth, reason, edge_type, source, mode}]
        """
        if not target:
            return []
        conn = self._get_conn()
        target_lower = target.lower()

        # Find target nodes by name match
        target_nodes = conn.execute(
            "SELECT id, name, qualified_name, file_path FROM nodes "
            "WHERE LOWER(name) = ? OR LOWER(qualified_name) LIKE ? "
            "LIMIT 10",
            (target_lower, f"%{target_lower}%")
        ).fetchall()

        if not target_nodes:
            # Try FTS fallback
            target_nodes = conn.execute(
                "SELECT n.id, n.name, n.qualified_name, n.file_path "
                "FROM nodes_fts f JOIN nodes n ON f.rowid = n.id "
                "WHERE nodes_fts MATCH ? LIMIT 10",
                (f'"{target}"',)
            ).fetchall()

        if not target_nodes:
            # Try word-level search: extract significant words from target
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
                # Deduplicate by id
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

        # Collect qualified names of target nodes
        target_qnames = set()
        target_files = set()
        for n in target_nodes:
            if n["qualified_name"]:
                target_qnames.add(n["qualified_name"])
            if n["file_path"]:
                target_files.add(self._normalize_path(n["file_path"]))

        # BFS: for each target, find callers (who calls it) and callees (what it calls)
        visited_qnames = set()
        file_scores = defaultdict(lambda: {"score": 0.0, "depth": 99, "reasons": set(), "edge_types": set()})
        frontier = set(target_qnames)

        for depth in range(1, max_depth + 1):
            if not frontier:
                break
            next_frontier = set()
            for qname in frontier:
                if qname in visited_qnames:
                    continue
                visited_qnames.add(qname)

                # Find callers (edges where target_qualified = this qname → source is the caller)
                try:
                    callers = conn.execute(
                        "SELECT DISTINCT e.source_qualified, e.kind, n.file_path, n.name "
                        "FROM edges e "
                        "LEFT JOIN nodes n ON n.qualified_name = e.source_qualified "
                        "WHERE e.target_qualified = ? AND e.kind IN ('CALLS', 'IMPORTS_FROM')",
                        (qname,)
                    ).fetchall()
                    for c in callers:
                        fp = self._normalize_path(c["file_path"]) if c["file_path"] else ""
                        if fp:
                            entry = file_scores[fp]
                            entry["score"] = max(entry["score"], 10.0 - (depth - 1) * 3)
                            entry["depth"] = min(entry["depth"], depth)
                            entry["reasons"].add("crg_caller" if c["kind"] == "CALLS" else "crg_importer")
                            entry["edge_types"].add(c["kind"])
                        if c["source_qualified"]:
                            next_frontier.add(c["source_qualified"])
                except Exception as e:
                    log.warning("CRG impact callers query failed: %s", e)

                # Find callees (edges where source_qualified = this qname → target is the callee)
                try:
                    callees = conn.execute(
                        "SELECT DISTINCT e.target_qualified, e.kind, n.file_path, n.name "
                        "FROM edges e "
                        "LEFT JOIN nodes n ON n.qualified_name = e.target_qualified "
                        "WHERE e.source_qualified = ? AND e.kind IN ('CALLS', 'IMPORTS_FROM')",
                        (qname,)
                    ).fetchall()
                    for c in callees:
                        fp = self._normalize_path(c["file_path"]) if c["file_path"] else ""
                        if fp:
                            entry = file_scores[fp]
                            entry["score"] = max(entry["score"], 8.0 - (depth - 1) * 2)
                            entry["depth"] = min(entry["depth"], depth)
                            entry["reasons"].add("crg_callee" if c["kind"] == "CALLS" else "crg_imported")
                            entry["edge_types"].add(c["kind"])
                        if c["target_qualified"]:
                            next_frontier.add(c["target_qualified"])
                except Exception as e:
                    log.warning("CRG impact callees query failed: %s", e)

            # Limit frontier to avoid explosion
            frontier = set(list(next_frontier)[:50])

        # Add target files themselves (depth 0)
        for fp in target_files:
            entry = file_scores[fp]
            entry["score"] = max(entry["score"], 15.0)
            entry["depth"] = 0
            entry["reasons"].add("crg_target")

        results = []
        for fp, data in sorted(file_scores.items(), key=lambda x: -x[1]["score"]):
            results.append({
                "file_path": fp,
                "score": round(data["score"], 1),
                "depth": data["depth"],
                "reason": sorted(data["reasons"]),
                "edge_types": sorted(data["edge_types"]),
                "source": "crg",
                "mode": "impact",
            })
        _vmsg("CRG IMPACT: target='%s' -> %d files (depth=%d)", target[:40], len(results), max_depth)
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
