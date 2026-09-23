"""Branch script graph: validation (cycles / orphans / uncovered exits) and traversal."""
from __future__ import annotations

from collections import deque
from typing import Any


# ---- validation -----------------------------------------------------------

def validate(content: dict[str, Any]) -> dict[str, list]:
    """Return validation markers for a script.

    Markers:
      cycles  : ids of nodes that lie on / lead into a directed cycle
      orphans : ids of nodes unreachable from the start node
      uncovered: node ids that have no options and no explicit terminal flag,
                 or an option whose target is missing/empty
    Missing-target options are additionally reported under `errors`.
    """
    nodes: list[dict] = content.get("nodes") or []
    start_id = content.get("start_node_id")
    node_ids = [n.get("id") for n in nodes]
    node_map = {n.get("id"): n for n in nodes}

    adjacency: dict[Any, list[Any]] = {nid: [] for nid in node_ids}
    dangling_options: list[dict] = []  # {"node_id", "option_id"}

    for node in nodes:
        for opt in node.get("options", []) or []:
            target = opt.get("target_id")
            if target and target in node_map:
                adjacency[node["id"]].append(target)
            else:
                dangling_options.append({"node_id": node["id"], "option_id": opt.get("id")})

    cycles = _find_cycle_nodes(node_ids, adjacency)

    orphans: list = []
    if start_id in node_map:
        reachable = _bfs_reachable(start_id, adjacency)
        orphans = [nid for nid in node_ids if nid not in reachable]
    else:
        # No valid start: every node is effectively orphaned.
        orphans = list(node_ids)

    uncovered = [d["node_id"] for d in dangling_options]
    errors = [
        {
            "type": "missing_target",
            "node_id": d["node_id"],
            "option_id": d["option_id"],
            "message": "选项未连接到任何节点",
        }
        for d in dangling_options
    ]
    if nodes and start_id not in node_map:
        errors.append({"type": "missing_start", "message": "起始节点不存在"})

    for node in nodes:
        options = node.get("options", []) or []
        if not options and not node.get("terminal"):
            uncovered.append(node["id"])

    return {
        "cycles": sorted(set(cycles)),
        "orphans": sorted(set(orphans)),
        "uncovered": sorted(set(uncovered)),
        "errors": errors,
        "ok": not cycles and not orphans and not errors and not uncovered,
    }


def _bfs_reachable(start: Any, adjacency: dict[Any, list]) -> set:
    seen = {start}
    queue = deque([start])
    while queue:
        cur = queue.popleft()
        for nxt in adjacency.get(cur, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _find_cycle_nodes(node_ids: list, adjacency: dict[Any, list]) -> list:
    """Nodes that belong to a directed cycle (Tarjan SCC, size>1 or self-loop)."""
    index_counter = [0]
    stack: list = []
    on_stack: set = set()
    indices: dict = {}
    lowlink: dict = {}
    cycle_nodes: set = set()

    def strongconnect(v):
        indices[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in adjacency.get(v, []):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            component = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            if len(component) > 1:
                cycle_nodes.update(component)
            elif v in adjacency.get(v, []):
                cycle_nodes.add(v)

    for nid in node_ids:
        if nid not in indices:
            strongconnect(nid)
    return list(cycle_nodes)


# ---- runtime traversal ----------------------------------------------------

def step(content: dict, node_id: str, option_id: str | None = None) -> dict[str, Any]:
    """Resolve the next node from `node_id` via `option_id`.

    option_id None means "start": resolve from start_node_id.
    Returns {node, finished} or raises ValueError.
    """
    node_map = {n["id"]: n for n in content.get("nodes", [])}
    if option_id is None:
        start = content.get("start_node_id")
        if start not in node_map:
            raise ValueError("起始节点不存在")
        return {"node": node_map[start], "finished": False}

    current = node_map.get(node_id)
    if current is None:
        raise ValueError("当前节点不存在")
    opt = next((o for o in current.get("options", []) if o.get("id") == option_id), None)
    if opt is None:
        raise ValueError("该选项不存在")
    target = opt.get("target_id")
    if not target or target not in node_map:
        raise ValueError("选项未连接到目标节点")
    return {"node": node_map[target], "finished": False}
