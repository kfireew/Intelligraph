"""
ChunkRetriever — Fetches code chunks for ranked file lists.

Respects task-level compression policy (raw vs compressed chunks).
Does NOT merge tasks or deduplicate globally — that is ContextMerger's responsibility.

Input:  ranked files + project data + task policy
Output: [{ file_path, name, start_line, end_line, content, compressed_content? }]
"""

import os
import logging

log = logging.getLogger(__name__)

MAX_CHUNKS = 50
MAX_SNIPPET_CHARS = 3000


def retrieve_chunks(ranked_files: list, proj: dict, task_policy: dict = None) -> list:
    """Fetch code chunks for ranked files.
    
    Tries repo_dir first, then graphify_data content fields.
    """
    if not ranked_files:
        return []

    file_paths = [rf["file_path"] for rf in ranked_files[:15]]
    chunks = []

    # Try repo_dir (cloned projects)
    repo_dir = proj.get("repo_dir")
    if repo_dir and os.path.isdir(repo_dir):
        try:
            from code_chunker import chunk_files
            chunks = chunk_files(file_paths, repo_dir=repo_dir, max_chunks=MAX_CHUNKS)
            if chunks:
                return _apply_policy(chunks, task_policy)
        except Exception as e:
            log.warning("ChunkRetriever repo_dir failed: %s", e)

    # Fallback: extract from graphify_data content fields
    gf = proj.get("graphify_data") or {}
    path_set = set(file_paths)
    for n in gf.get("nodes", []):
        sf = n.get("source_file") or n.get("file_path") or ""
        if sf not in path_set:
            continue
        source = n.get("source") or n.get("content") or ""
        if not source:
            continue
        chunks.append({
            "file_path": sf,
            "name": n.get("name") or n.get("label", ""),
            "start_line": n.get("line_start", 1),
            "end_line": n.get("line_end", min(n.get("line_start", 1) + 50, 9999)),
            "content": source[:MAX_SNIPPET_CHARS],
        })

    return _apply_policy(chunks[:MAX_CHUNKS], task_policy)


def _apply_policy(chunks: list, policy: dict) -> list:
    """Apply compression policy to chunks if headroom is available.
    
    Stores both raw and compressed — policy decides which to serve.
    Phase 3: currently a no-op until Headroom is benchmarked.
    """
    if not policy or policy.get("compression") == "none":
        return chunks

    # Future: compress content when Headroom is integrated
    # for c in chunks:
    #     c["raw_content"] = c["content"]
    #     if policy["compression"] in ("partial", "full"):
    #         try:
    #             from headroom import compress
    #             c["content"] = compress(...)
    #         except ImportError:
    #             pass

    return chunks