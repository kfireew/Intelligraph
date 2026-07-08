"""
crg_domain_finder.py — Backend-only CRG FTS search for domain/business workflow files.

For broad queries like "architecture" or "how does X work", graph degree can miss
domain workflow files because business logic files may not be high-degree hubs.
This module uses CRG's FTS5 index to find files related to domain keywords.

Keywords are auto-derived from the graph's node labels and source files (TF-IDF),
plus the user's actual query terms — no hardcoded project-specific keywords.

Backend only. No frontend CRG. No sql.js. No browser-side SQL.
"""

import logging
import sqlite3
import os
import re
from collections import Counter

log = logging.getLogger(__name__)


def get_crg_db_path(proj: dict) -> str | None:
    """Get CRG DB path from project metadata, or None if unavailable.

    Checks relocated artifact first (proj['crg_db_path']), then repo_dir.
    """
    # 1. Relocated artifact (post-build cleanup — most common case)
    crg_path = proj.get("crg_db_path")
    if crg_path and os.path.isfile(crg_path):
        return crg_path

    # 2. Artifacts dir fallback
    pid = proj.get("id")
    if pid:
        artifacts = os.environ.get("INTELLIGRAPH_ARTIFACTS_DIR",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "artifacts"))
        p = os.path.join(artifacts, str(pid), "graph.db")
        if os.path.isfile(p):
            return p

    # 3. Repo dir (if still alive — e.g. INTELLIGRAPH_ENABLE_NX_MCP=true)
    repo_dir = proj.get("repo_dir")
    if not repo_dir:
        return None

    crg_path = os.path.join(repo_dir, ".code-review-graph", "graph.db")
    if os.path.isfile(crg_path):
        return crg_path

    # Narrow sibling search
    parent = os.path.dirname(repo_dir)
    repo_basename = os.path.basename(repo_dir)
    if parent and os.path.isdir(parent):
        parent_name = os.path.basename(parent)
        if parent_name == "repos" or ("-1" in parent_name and os.path.isdir(os.path.join(parent, repo_basename))):
            for entry in os.listdir(parent)[:20]:  # cap sibling checks
                if entry == repo_basename:
                    continue
                sibling_crg = os.path.join(parent, entry, ".code-review-graph", "graph.db")
                if os.path.isfile(sibling_crg):
                    log.info("CRG DB found in sibling dir: %s", sibling_crg)
                    return sibling_crg
    return None


def search_crg_fts(crg_db_path: str, term: str, limit: int = 20) -> list[dict]:
    """Search CRG FTS5 index for a single term. Returns node matches."""
    if not crg_db_path or not os.path.isfile(crg_db_path):
        return []
    try:
        conn = sqlite3.connect(crg_db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT n.file_path, n.name, n.kind, n.qualified_name, n.signature
            FROM nodes_fts f
            JOIN nodes n ON f.rowid = n.id
            WHERE nodes_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (term, limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("CRG FTS search failed for term '%s': %s", term, e)
        return []


def _auto_derive_keywords(graphify_data: dict, max_keywords: int = 20) -> list[str]:
    """Derive domain keywords from graph node labels and source files using TF.
    
    Extracts frequent meaningful tokens from node names and file paths.
    These represent the domain vocabulary of the codebase.
    """
    if not graphify_data:
        return []
    
    nodes = graphify_data.get("nodes", [])
    token_freq = Counter()
    
    # Stopwords to filter out
    stopwords = {
        "test", "tests", "init", "main", "index", "util", "utils", "helper",
        "helpers", "common", "config", "setup", "base", "abstract", "generic",
        "data", "type", "class", "func", "function", "method", "def", "self",
        "true", "false", "none", "null", "return", "import", "from", "with",
    }
    
    for n in nodes:
        # Extract from label
        label = (n.get("label") or n.get("name") or "").lower()
        for token in re.split(r"[\s_\-\./]+", label):
            token = token.strip()
            if len(token) > 2 and token.isalpha() and token not in stopwords:
                token_freq[token] += 2  # label tokens weighted higher
        
        # Extract from source_file
        sf = (n.get("source_file") or "").lower()
        for token in re.split(r"[\s_\-\./]+", sf):
            token = token.strip()
            if len(token) > 2 and token.isalpha() and token not in stopwords:
                token_freq[token] += 1
    
    return [kw for kw, _ in token_freq.most_common(max_keywords)]


def _extract_query_terms(query: str) -> list[str]:
    """Extract meaningful terms from the user's query."""
    if not query:
        return []
    lower = query.lower()
    stopwords = {
        "what", "how", "where", "which", "who", "the", "a", "an", "is", "are",
        "does", "do", "can", "should", "would", "could", "explain", "describe",
        "architecture", "structure", "overview", "and", "or", "of", "to", "in",
        "for", "with", "about", "tell", "me", "find", "show", "list",
    }
    terms = []
    for token in re.split(r"[\s_\-\./]+", lower):
        token = token.strip()
        if len(token) > 2 and token.isalpha() and token not in stopwords:
            terms.append(token)
    return terms


def find_domain_files_with_crg(
    crg_db_path: str,
    query: str,
    repo_dir: str = None,
    max_files: int = 12,
    graphify_data: dict = None,
) -> list[dict]:
    """Search CRG for domain/business files relevant to the query.

    Uses auto-derived keywords from the graph + user's query terms.
    No hardcoded project-specific keywords.

    Args:
        crg_db_path: Path to CRG graph.db
        query: User prompt (e.g. "architecture", "how does OCR work")
        repo_dir: Repo root for path normalization
        max_files: Max domain files to return
        graphify_data: Graph data for keyword derivation

    Returns:
        [{file_path, score, matched_terms, source, reason}]
    """
    if not crg_db_path or not os.path.isfile(crg_db_path):
        return []

    # Auto-derive keywords from graph + query terms
    keywords = _auto_derive_keywords(graphify_data, max_keywords=15) if graphify_data else []
    query_terms = _extract_query_terms(query)
    
    # Query terms get priority (searched first, higher weight)
    all_search_terms = query_terms + keywords
    
    if not all_search_terms:
        return []

    file_scores = {}
    for i, term in enumerate(all_search_terms):
        # Query terms (first in list) get higher weight
        weight = 3.0 if i < len(query_terms) else 2.0
        results = search_crg_fts(crg_db_path, f'"{term}"', limit=10)
        for r in results:
            rel_path = r["file_path"]
            # Convert CRG absolute path to repo-relative path
            if repo_dir:
                norm_repo = repo_dir.replace("\\", "/")
                norm_path = rel_path.replace("\\", "/")
                if norm_repo in norm_path:
                    rel_path = norm_path.replace(norm_repo, "").lstrip("/")
                else:
                    crg_root = os.path.dirname(crg_db_path)
                    crg_root = os.path.dirname(crg_root)
                    norm_crg = crg_root.replace("\\", "/")
                    if norm_crg in norm_path:
                        rel_path = norm_path.replace(norm_crg, "").lstrip("/")
            rel_path = rel_path.replace("\\", "/")
            if rel_path not in file_scores:
                file_scores[rel_path] = {"score": 0.0, "matched_terms": set(), "search_count": 0}
            file_scores[rel_path]["score"] += weight
            file_scores[rel_path]["matched_terms"].add(term)
            file_scores[rel_path]["search_count"] += 1

    # Boost files matching multiple terms (cross-domain workflow files)
    for fp, data in file_scores.items():
        term_count = len(data["matched_terms"])
        if term_count >= 2:
            data["score"] *= 1.5
        if term_count >= 3:
            data["score"] *= 1.5

    sorted_files = sorted(file_scores.items(), key=lambda x: -x[1]["score"])

    result = []
    for fp, data in sorted_files[:max_files]:
        result.append({
            "file_path": fp,
            "score": round(data["score"], 1),
            "matched_terms": sorted(data["matched_terms"]),
            "groups": ["auto_derived"],
            "source": "crg_fts",
            "reason": "domain_workflow_match",
        })

    return result
