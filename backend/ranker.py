"""
NeighborhoodRanker — Ranks expanded nodes by centrality, risk, and relevance.

Takes expanded node IDs and graph data, returns ranked file list.
Ranking factors: degree centrality, community cohesion, dependency risk.

Input:  expanded_node_ids + graphify_data
Output: { ranked_files: [{ file_path, score, reason }] }
"""


def rank_neighborhood(expanded_node_ids: list, graphify_data: dict, node_map: dict = None) -> list:
    """Rank expanded nodes and produce sorted file list.
    
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

        # Degree centrality
        deg = degree.get(nid, 0)
        if deg > 0:
            entry["score"] += min(deg * 2, 20)

        # Node is a hub (high degree relative to average)
        avg_deg = max(len(degree) // max(len(nodes), 1), 1)
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