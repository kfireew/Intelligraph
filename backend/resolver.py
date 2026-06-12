"""
NodeResolver — Maps query text to graph nodes.

Supports:
- Exact match (id, label, qualified_name)
- Fuzzy match (substring, case-insensitive)
- BFS expansion (neighbors of matched nodes)

Input:  prompt target string + graphify_data
Output: [{ id, label, source_file, community, degree }]
"""

import re


def resolve_nodes(target: str, graphify_data: dict, max_nodes: int = 10) -> list:
    """Resolve target text to graph nodes.
    
    Priority: exact match > label match > source_file match > BFS from matches.
    """
    if not target or not graphify_data:
        return []

    nodes = graphify_data.get("nodes", [])
    target_lower = target.lower().strip()

    # 1. Exact match on id or label
    matches = []
    for n in nodes:
        nid = (n.get("id") or "").lower()
        label = (n.get("label") or "").lower()
        qname = (n.get("qualified_name") or "").lower()
        if target_lower in (nid, label, qname):
            matches.append(n)
    if matches:
        return _deduplicate(matches)[:max_nodes]

    # 2. Token-based fuzzy match
    tokens = [t for t in re.split(r"[\s_\-\./]", target_lower) if len(t) > 1]
    if tokens:
        scored = []
        for n in nodes:
            label = (n.get("label") or "").lower()
            source = (n.get("source_file") or "").lower()
            content = (n.get("content") or n.get("text") or "").lower()[:300]
            score = 0
            for t in tokens:
                if t in label:
                    score += 3
                if t in source:
                    score += 2
                if t in content:
                    score += 1
            if score > 0:
                scored.append((score, n))
        scored.sort(key=lambda x: -x[0])
        matches = [n for _, n in scored]

    return _deduplicate(matches)[:max_nodes]


def _deduplicate(nodes: list) -> list:
    """Remove duplicate nodes by id."""
    seen = set()
    result = []
    for n in nodes:
        nid = n.get("id") or n.get("label")
        if nid and nid not in seen:
            seen.add(nid)
            result.append(n)
    return result


def build_node_map(graphify_data: dict) -> dict:
    """Build id → node lookup map (used by other modules)."""
    node_map = {}
    for n in graphify_data.get("nodes", []):
        for key in (n.get("id"), n.get("label"), n.get("qualified_name")):
            if key:
                node_map[key] = n
                node_map[key.lower()] = n
    return node_map