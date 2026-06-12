"""
crg_domain_finder.py — Backend-only CRG FTS search for domain/business workflow files.

For broad queries like "architecture" or "how does X work", graph degree can miss
domain workflow files because business logic files may not be high-degree hubs.
This module uses CRG's FTS5 index to find files related to domain keywords.

Backend only. No frontend CRG. No sql.js. No browser-side SQL.
"""

import logging
import sqlite3
import os

log = logging.getLogger(__name__)

# Domain keyword groups for architecture/overview queries.
# Each group represents a business workflow layer in the application.
DOMAIN_KEYWORD_GROUPS = {
    "receipt_ocr": [
        "receipt", "ocr", "parser", "correct", "extract", "detect"
    ],
    "column_schema_mapping": [
        "column", "mapping", "schema", "field", "normalize", "type"
    ],
    "pipeline_phases": [
        "phase", "pipeline", "step", "stage", "flow", "process"
    ],
    "vendor_product_catalog": [
        "vendor", "product", "catalog", "merchant", "cache"
    ],
    "email_io": [
        "email", "fetch", "attachment", "import", "inbox", "poll"
    ],
    "database_data": [
        "database", "db", "model", "store", "repository", "save"
    ],
}

def get_crg_db_path(proj: dict) -> str | None:
    """Get CRG DB path from project metadata, or None if unavailable."""
    repo_dir = proj.get("repo_dir")
    if not repo_dir:
        return None

    # Standard location: inside the repo
    crg_path = os.path.join(repo_dir, ".code-review-graph", "graph.db")
    if os.path.isfile(crg_path):
        return crg_path

    # Narrow sibling search: only if parent has a known clone store pattern.
    parent = os.path.dirname(repo_dir)
    repo_basename = os.path.basename(repo_dir)
    if parent and os.path.isdir(parent):
        parent_name = os.path.basename(parent)
        if parent_name == "repos" or ("-1" in parent_name and os.path.isdir(os.path.join(parent, repo_basename))):
            for entry in os.listdir(parent):
                if entry == repo_basename:
                    continue
                sibling_crg = os.path.join(parent, entry, ".code-review-graph", "graph.db")
                sibling_gf = os.path.join(parent, entry, "graphify-out", "graph.json")
                if os.path.isfile(sibling_crg) and os.path.isfile(sibling_gf):
                    log.info("CRG DB found in sibling dir: %s", sibling_crg)
                    return sibling_crg
    return None


def search_crg_fts(crg_db_path: str, term: str, limit: int = 20) -> list[dict]:
    """Search CRG FTS5 index for a single term. Returns node matches."""
    if not os.path.isfile(crg_db_path):
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


def find_domain_files_with_crg(
    crg_db_path: str,
    query: str,
    repo_dir: str = None,
    max_files: int = 12,
) -> list[dict]:
    """Search CRG for domain/business files relevant to the query.

    Args:
        crg_db_path: Path to CRG graph.db
        query: User prompt (e.g. "architecture", "how does OCR work")
        max_files: Max domain files to return

    Returns:
        [{file_path, score, matched_terms, source, reason}]
    """
    if not crg_db_path or not os.path.isfile(crg_db_path):
        return []

    query_lower = query.lower()
    active_groups = list(DOMAIN_KEYWORD_GROUPS.keys())
    # If query mentions specific domains, prioritize those groups
    for group_name, keywords in DOMAIN_KEYWORD_GROUPS.items():
        for kw in keywords:
            if kw in query_lower:
                if group_name in active_groups:
                    active_groups.remove(group_name)
                    active_groups.insert(0, group_name)
                break

    # Search each group via CRG FTS
    file_scores = {}
    for group_name in active_groups:
        keywords = DOMAIN_KEYWORD_GROUPS[group_name]
        for kw in keywords:
            results = search_crg_fts(crg_db_path, f'"{kw}"', limit=10)
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
                        crg_root = os.path.dirname(crg_root)  # .code-review-graph
                        norm_crg = crg_root.replace("\\", "/")
                        if norm_crg in norm_path:
                            rel_path = norm_path.replace(norm_crg, "").lstrip("/")
                rel_path = rel_path.replace("\\", "/")
                if rel_path not in file_scores:
                    file_scores[rel_path] = {"score": 0.0, "matched_terms": set(), "groups": set()}
                file_scores[rel_path]["score"] += 3.0
                file_scores[rel_path]["matched_terms"].add(kw)
                file_scores[rel_path]["groups"].add(group_name)

    # Boost files matching multiple groups (cross-domain workflow files)
    for fp, data in file_scores.items():
        group_count = len(data["groups"])
        if group_count >= 2:
            data["score"] *= 1.5
        if group_count >= 3:
            data["score"] *= 1.5

    # Sort by score descending
    sorted_files = sorted(file_scores.items(), key=lambda x: -x[1]["score"])

    result = []
    for fp, data in sorted_files[:max_files]:
        result.append({
            "file_path": fp,
            "score": round(data["score"], 1),
            "matched_terms": sorted(data["matched_terms"]),
            "groups": list(data["groups"]),
            "source": "crg_fts",
            "reason": "domain_workflow_match",
        })

    return result