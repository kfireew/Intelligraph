"""CodeChunker — tree-sitter-based function/class extraction for LLM context.

Ported from Hackathon-2026-BIU Ingestor/code_chunker.py.
Surgical: only parses files requested by the code-chunks endpoint.
Supports Python, JavaScript, TypeScript, Java.
"""

import logging
from pathlib import Path

from tree_sitter import Language, Parser

log = logging.getLogger(__name__)

CHUNKING_MODE = "unknown"
_TS_LANGUAGES = None
MAX_CHUNK_CHARS = 2000


def get_available_languages():
    """Return loaded language map or None if not yet detected."""
    return _TS_LANGUAGES


def _detect_mode() -> str:
    """Detect whether AST chunking is available and which languages are loaded."""
    global _TS_LANGUAGES, CHUNKING_MODE
    if _TS_LANGUAGES is not None:
        return CHUNKING_MODE
    try:
        import tree_sitter_python
        import tree_sitter_javascript
        import tree_sitter_typescript
        import tree_sitter_java
        _TS_LANGUAGES = {
            ".py": Language(tree_sitter_python.language()),
            ".js": Language(tree_sitter_javascript.language()),
            ".ts": Language(tree_sitter_typescript.language_typescript()),
            ".tsx": Language(tree_sitter_typescript.language_tsx()),
            ".java": Language(tree_sitter_java.language()),
        }
        loaded = list(_TS_LANGUAGES.keys())
        CHUNKING_MODE = "ast"
        log.info("Code chunking mode: AST. Loaded languages: %s", ", ".join(loaded))
        return CHUNKING_MODE
    except ImportError as e:
        log.warning("Code chunking mode: RAW FALLBACK. Reason: missing tree-sitter module: %s", e)
        CHUNKING_MODE = "raw_fallback"
        return CHUNKING_MODE


# Call on import so startup logs show mode
_detect_mode()


# ── Extraction helpers ──

def _extract_python_chunks(node, source_bytes, file_path, depth=0):
    """Walk Python AST, yield {name, type, content, start_line, end_line, file_path}."""
    chunks = []
    if depth > 3:
        return chunks
    if node.type in ("class_definition", "function_definition", "decorated_definition"):
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            name = source_bytes[name_node.start_byte:name_node.end_byte].decode()
            kind = "class" if node.type == "class_definition" else "function"
            content = source_bytes[node.start_byte:node.end_byte].decode()
            if len(content) > MAX_CHUNK_CHARS:
                content = content[:MAX_CHUNK_CHARS] + "\n# ... (truncated)"
            chunks.append({
                "file_path": file_path,
                "name": name,
                "type": kind,
                "content": content,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
            })
    for child in node.children:
        chunks.extend(_extract_python_chunks(child, source_bytes, file_path, depth + 1))
    return chunks


def _extract_generic_chunks(node, source_bytes, file_path, depth=0,
                            class_types=("class_declaration",),
                            func_types=("function_declaration", "method_definition",
                                        "function", "arrow_function",
                                        "generator_function_declaration")):
    """Walk JS/TS/Java AST."""
    chunks = []
    if depth > 5:
        return chunks
    node_type = node.type
    if node_type in func_types or node_type in class_types:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            # Try to find first identifier child for anonymous patterns
            for child in node.children:
                if child.type == "identifier":
                    name_node = child
                    break
        if name_node is not None:
            name = source_bytes[name_node.start_byte:name_node.end_byte].decode()
            kind = "class" if node_type in class_types else "function"
            content = source_bytes[node.start_byte:node.end_byte].decode()
            if len(content) > MAX_CHUNK_CHARS:
                content = content[:MAX_CHUNK_CHARS] + "\n// ... (truncated)"
            chunks.append({
                "file_path": file_path,
                "name": name,
                "type": kind,
                "content": content,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
            })
    for child in node.children:
        chunks.extend(_extract_generic_chunks(child, source_bytes, file_path, depth + 1,
                                              class_types, func_types))
    return chunks


# ── Public API ──

def chunk_file(file_path, repo_dir=None):
    """Parse a single file and return its code chunks.
    
    Returns AST chunks when possible, or degraded raw fallback chunks.
    """
    if CHUNKING_MODE == "raw_fallback":
        return _raw_fallback(file_path, repo_dir)
    
    languages = get_available_languages()
    if languages is None:
        _detect_mode()
        languages = get_available_languages()
        if not languages:
            return _raw_fallback(file_path, repo_dir)
    if repo_dir:
        full_path = Path(repo_dir) / file_path
    else:
        full_path = Path(file_path)

    if not full_path.is_file():
        return []

    suffix = full_path.suffix.lower()
    lang = languages.get(suffix)
    if lang is None:
        return []

    try:
        source_bytes = full_path.read_bytes()
    except (OSError, PermissionError) as e:
        log.warning("Cannot read %s: %s", file_path, e)
        return []

    parser = Parser()
    parser.language = lang
    tree = parser.parse(source_bytes)
    root = tree.root_node

    if suffix == ".py":
        return _extract_python_chunks(root, source_bytes, str(file_path))
    else:
        if suffix == ".java":
            class_types = ("class_declaration", "interface_declaration", "enum_declaration")
            func_types = ("method_declaration", "constructor_declaration")
        else:
            class_types = ("class_declaration",)
            func_types = ("function_declaration", "method_definition",
                          "function", "arrow_function",
                          "generator_function_declaration")
        return _extract_generic_chunks(root, source_bytes, str(file_path),
                                       class_types=class_types, func_types=func_types)



def _raw_fallback(file_path, repo_dir=None):
    """Degraded fallback when AST chunking is unavailable.
    
    Reads the entire file as a single chunk up to 3000 chars.
    Returns chunks marked as degraded so the LLM knows this is not full source.
    """
    if repo_dir:
        full_path = Path(repo_dir) / file_path
    else:
        full_path = Path(file_path)
    if not full_path.is_file():
        return []
    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
        truncated = len(content) > 3000
        snippet = content[:3000]
        name = full_path.stem
        return [{
            "file_path": file_path,
            "name": name,
            "type": "file",
            "chunking_mode": "raw_fallback",
            "degraded": True,
            "truncated": truncated,
            "content": snippet,
            "start_line": 1,
            "end_line": min(content.count("\n") + 1, 9999),
        }]
    except Exception as e:
        log.warning("raw_fallback failed for %s: %s", file_path, e)
        return []

def chunk_files(file_paths, repo_dir=None, max_chunks=50):
    """Parse multiple files and return combined code chunks.
    
    Args:
        file_paths: list of relative paths
        repo_dir: absolute repo root
        max_chunks: limit total chunks returned
    
    Returns:
        list of {file_path, name, type, content, start_line, end_line}
    """
    all_chunks = []
    for fp in file_paths:
        chunks = chunk_file(fp, repo_dir=repo_dir)
        all_chunks.extend(chunks)
        if len(all_chunks) >= max_chunks:
            break
    return all_chunks[:max_chunks]

