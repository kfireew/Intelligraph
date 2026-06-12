"""
TraversalPlanner — Plans graph traversal per task.

Defines direction (incoming/outgoing/both), depth, and node-type filters
for each task's graph walk. Produces an expanded set of neighbor nodes.

Input:  task plan + matched nodes + graphify_data
Output: { expanded: [node_ids], by_depth: {1: [...], 2: [...], ...} }
"""


def plan_traversal(task: dict, matched_nodes: list, links: list) -> dict:
    """Expand matched nodes by following links per task operations.
    
    Args:
        task:     { type, target, depth, compression, operations }
        matched_nodes: [{ id, label, ... }] from NodeResolver
        links:   [{ source, target, from, to, relation }, ...]
    
    Returns:
        { expanded: [node_id_set], by_depth: { depth: [node_id_set] } }
    """
    if not matched_nodes or not links:
        return {"expanded": [], "by_depth": {}}

    ops = task.get("operations", [])
    max_depth = task.get("depth", 2)

    # Build adjacency maps
    outgoing = {}  # node_id → [target_ids]
    incoming = {}  # node_id → [source_ids]

    for l in links:
        src = l.get("source") or l.get("from") or ""
        tgt = l.get("target") or l.get("to") or ""
        if src:
            outgoing.setdefault(src, []).append(tgt) if tgt else None
        if tgt:
            incoming.setdefault(tgt, []).append(src) if src else None

    start_ids = set()
    for n in matched_nodes:
        nid = n.get("id") or n.get("label")
        if nid:
            start_ids.add(nid)

    # Determine traversal directions from operations
    directions = set()
    if any(op in ("expand_callers", "incoming_callers") for op in ops):
        directions.add("incoming")
    if any(op in ("expand_callees", "outgoing_callers") for op in ops):
        directions.add("outgoing")
    if any(op in ("expand_neighbors", "find_community_hubs") for op in ops):
        directions.add("both")
    if not directions:
        directions.add("both")

    # BFS expansion
    expanded = set()
    by_depth = {}
    visited = set(start_ids)

    current = set(start_ids)
    for depth in range(1, max_depth + 1):
        next_ids = set()
        for nid in current:
            if "outgoing" in directions or "both" in directions:
                for neighbor in outgoing.get(nid, []):
                    if neighbor and neighbor not in visited:
                        next_ids.add(neighbor)
                        visited.add(neighbor)
            if "incoming" in directions or "both" in directions:
                for neighbor in incoming.get(nid, []):
                    if neighbor and neighbor not in visited:
                        next_ids.add(neighbor)
                        visited.add(neighbor)
        if next_ids:
            by_depth[depth] = list(next_ids)
            expanded.update(next_ids)
        current = next_ids
        if not current:
            break

    return {
        "expanded": list(visited),
        "by_depth": by_depth,
        "start_ids": list(start_ids),
    }