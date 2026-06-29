"""
NeighborhoodRanker — Ranks expanded nodes by centrality, risk, and relevance.

Takes expanded node IDs and graph data, returns ranked file list.
Ranking factors: degree centrality, community cohesion, dependency risk,
and query-term relevance.

Input:  expanded_node_ids + graphify_data
Output: { ranked_files: [{ file_path, score, reason }] }
"""

import re


def rank_neighborhood(expanded_node_ids: list, graphify_data: dict, node_map: dict = None,
                      query: str = "") -> list:
    """Rank expanded nodes and produce sorted file list.
    
    Args:
        expanded_node_ids: node IDs from traversal
        graphify_data: full graph data
        node_map: optional pre-built node lookup
        query: original user query for relevance scoring
    
    Returns [{ file_path, score, reason }] sorted by score descending.
    """
    if not expanded_node_ids or not graphify_data:
        return []

    nodes = graphify_data.get("nodes", [])
    links = graphify_data.get("links", [])

    if node_map is None:
        node_map = {}
        for n in nodes:
            for key in (n.get("id"), n.get("label"), n.get("qualified_name")):
                if key:
                    node_map[key] = n
                    node_map[key.lower()] = n

    # Build degree scores per node
    degree = {}
    for l in links:
        for key in (l.get("source"), l.get("target"), l.get("from"), l.get("to")):
            if key:
                degree[key] = degree.get(key, 0) + 1

    # Extract query tokens for relevance scoring
    query_tokens = set()
    if query:
        lower = query.lower()
        for t in re.split(r"[\s_\-\./]", lower):
            if len(t) > 1:
                query_tokens.add(t)

    # Score each expanded node
    scored_files = {}  # file_path → { score, reason, count }
    for nid in expanded_node_ids:
        node = node_map.get(nid) or node_map.get(nid.lower())
        if not node:
            continue
        sf = node.get("source_file")
        if not sf:
            continue

        if sf not in scored_files:
            scored_files[sf] = {"score": 0, "reasons": [], "count": 0}
        entry = scored_files[sf]
        entry["count"] += 1

        # Degree centrality (logarithmic to avoid flattening)
        deg = degree.get(nid, 0)
        if deg > 0:
            import math
            entry["score"] += min(math.log2(deg + 1) * 5, 25)

        # Node is a hub (high degree relative to average)
        avg_deg = max(len(degree) / max(len(nodes), 1), 1.0)
        if deg > avg_deg * 3:
            entry["score"] += 10
            entry["reasons"].append("hub_node")

        # Node from a community (prefer community nodes)
        if node.get("community") is not None:
            entry["score"] += 5
            entry["reasons"].append(f"community_{node['community']}")

        # File with many matched nodes (dense file)
        if entry["count"] > 2:
            entry["score"] += entry["count"] * 2

        # Query-term relevance scoring (multiplicative boost for strong matches)
        relevance = 0
        if query_tokens:
            label = (node.get("label") or "").lower()
            source = (node.get("source_file") or "").lower()
            content = (node.get("content") or node.get("text") or "").lower()[:500]
            for t in query_tokens:
                if t in label:
                    relevance += 4
                if t in source:
                    relevance += 3
                if t in content:
                    relevance += 2
        
        # Apply relevance: multiplicative boost so relevant files outrank high-degree irrelevant ones
        if relevance > 0:
            entry["score"] += min(relevance, 30)
            entry["reasons"].append("query_relevant")
        elif query_tokens and entry["score"] > 20:
            # Penalize irrelevant high-degree nodes when we have a specific query
            entry["score"] = int(entry["score"] * 0.5)

    # Sort by score descending
    ranked = [
        {"file_path": fp, "score": data["score"], "reason": data["reasons"][:3] if data["reasons"] else ["matched"]}
        for fp, data in sorted(scored_files.items(), key=lambda x: -x[1]["score"])
    ]

    return ranked


def build_degree_scores(links: list) -> dict:
    """Build node_id → degree. Used by multiple modules."""
    scores = {}
    for l in links:
        for key in (l.get("source"), l.get("target"), l.get("from"), l.get("to")):
            if key:
                scores[key] = scores.get(key, 0) + 1
    return scores
