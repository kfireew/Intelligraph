"""
ChunkRetriever — Fetches code chunks for ranked file lists.

Respects task-level compression policy (raw vs compressed chunks).
Does NOT merge tasks or deduplicate globally — that is ContextMerger's responsibility.

Input:  ranked files + project data + task policy
Output: [{ file_path, name, start_line, end_line, content, compressed_content? }]
"""

import os
import re
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
                chunks = _dedup_overlapping(chunks)
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
        # Use node id hash for line numbers so chunks don't all collide at L1-51
        node_idx = n.get("line_start") or (hash(n.get("id", "")) % 9000 + 1)
        chunks.append({
            "file_path": sf,
            "name": n.get("name") or n.get("label", ""),
            "start_line": node_idx,
            "end_line": n.get("line_end", node_idx + 50),
            "content": source[:MAX_SNIPPET_CHARS],
        })

    # Dedup FIRST, then cap — avoids losing whole files when chunks are file-ordered
    chunks = _dedup_overlapping(chunks)[:MAX_CHUNKS]
    return _apply_policy(chunks, task_policy)


def _dedup_overlapping(chunks: list) -> list:
    """Remove chunks whose line range is fully contained in another chunk.
    
    E.g. a class chunk at L1-100 contains a method chunk at L10-50 → drop the method.
    """
    if len(chunks) <= 1:
        return chunks
    # Sort by file, then by start_line ascending, then by (end_line - start_line) descending
    chunks_sorted = sorted(chunks, key=lambda c: (c.get("file_path", ""), c.get("start_line", 0), -(c.get("end_line", 0) - c.get("start_line", 0))))
    result = []
    for chunk in chunks_sorted:
        fp = chunk.get("file_path", "")
        start = chunk.get("start_line", 0)
        end = chunk.get("end_line", 0)
        contained = False
        for kept in result:
            if kept.get("file_path", "") != fp:
                continue
            k_start = kept.get("start_line", 0)
            k_end = kept.get("end_line", 0)
            if k_start <= start and end <= k_end:
                # This chunk is fully contained in an already-kept chunk
                contained = True
                break
        if not contained:
            result.append(chunk)
    return result


def _apply_policy(chunks: list, policy: dict) -> list:
    """Apply compression policy to chunks.
    
    - "none": keep full content (for exact-code tasks: debug, refactor, security, impact, what_is)
    - "partial": keep signature + docstring + first 15 lines of body (for how_works)
    - "full": keep full content (for architecture, explain — these need full context)
    """
    if not policy:
        return chunks
    
    compression = policy.get("compression", "none")
    if compression in ("none", "full"):
        return chunks
    
    # "partial" compression: keep signature + docstring + first N lines
    if compression == "partial":
        for c in chunks:
            content = c.get("content", "")
            if len(content) <= 800:
                continue
            c["raw_content"] = content
            c["content"] = _compress_partial(content)
    
    return chunks


def _compress_partial(content: str) -> str:
    """Compress code to signature + docstring + first 15 lines of body.
    
    Keeps enough context to understand the structure without dumping entire bodies.
    """
    lines = content.split("\n")
    if len(lines) <= 20:
        return content
    
    # Find signature (first line(s) with def/class/async def)
    sig_end = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.endswith(":") or stripped.endswith(")"):
            sig_end = i + 1
            break
    if sig_end == 0:
        sig_end = 1
    
    # Check for docstring after signature
    body_start = sig_end
    docstring_lines = []
    if body_start < len(lines):
        first_body = lines[body_start].strip()
        if first_body.startswith('"""') or first_body.startswith("'''") or first_body.startswith('"') or first_body.startswith("'"):
            # Collect docstring lines
            quote = first_body[:3] if first_body[:3] in ('"""', "'''") else first_body[:1]
            if first_body.count(quote) >= 2 and len(first_body) > 3:
                # Single-line docstring
                docstring_lines = [lines[body_start]]
                body_start += 1
            else:
                docstring_lines.append(lines[body_start])
                for j in range(body_start + 1, min(body_start + 20, len(lines))):
                    docstring_lines.append(lines[j])
                    if quote in lines[j]:
                        body_start = j + 1
                        break
    
    # Collect first 15 body lines after docstring
    body_lines = lines[body_start:body_start + 15]
    
    result = "\n".join(lines[:sig_end])
    if docstring_lines:
        result += "\n" + "\n".join(docstring_lines)
    result += "\n" + "\n".join(body_lines)
    if body_start + 15 < len(lines):
        result += f"\n# ... ({len(lines) - body_start - 15} more lines truncated)"
    return result
