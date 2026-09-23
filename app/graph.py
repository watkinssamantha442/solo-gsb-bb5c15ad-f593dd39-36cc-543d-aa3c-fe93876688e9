"""Branch script graph: validation, traversal and branch coverage aggregation."""
from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable
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


# ---- branch coverage ------------------------------------------------------

def coverage_stats(content: dict[str, Any], runs: Iterable[dict]) -> dict[str, Any]:
    """Aggregate how often each current option was chosen across saved runs.

    `runs` is an iterable of run rows (dicts) carrying a JSON `steps` list;
    every step but the last one answers the node it is attached to via
    `option_id` (the last step has option_id None — the interview stopped there).

    Counts are aligned to the *current* script: only (node_id, option_id)
    pairs that still exist are reported; choices pointing at options deleted
    since the run was recorded are counted under `stale_choices`. With no runs
    (or no choices) every option simply has count 0 and `never_taken` is an
    empty list — zero observations must not be read as "branch never taken".
    """
    nodes: list[dict] = content.get("nodes") or []
    node_ids = {n.get("id") for n in nodes}
    option_ids: dict[Any, set] = {}
    for node in nodes:
        option_ids[node.get("id")] = {
            o.get("id") for o in node.get("options", []) or []
        }

    counts: dict[Any, dict[Any, int]] = {
        nid: {oid: 0 for oid in oids} for nid, oids in option_ids.items()
    }
    run_count = 0
    choice_count = 0
    stale_choices = 0

    for run in runs:
        run_count += 1
        steps = run.get("steps") or []
        if isinstance(steps, str):
            # tolerate raw DB rows where steps is still a JSON string
            try:
                steps = json.loads(steps)
            except (ValueError, TypeError):
                steps = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            option_id = step.get("option_id")
            if not option_id:
                continue  # the final step of a run has no answer
            choice_count += 1
            node_id = step.get("node_id")
            if node_id in node_ids and option_id in option_ids.get(node_id, ()):
                counts[node_id][option_id] += 1
            else:
                stale_choices += 1

    per_node: list[dict] = []
    total_options = 0
    taken_options = 0
    for node in nodes:
        options = []
        for opt in node.get("options", []) or []:
            c = counts.get(node.get("id"), {}).get(opt.get("id"), 0)
            total_options += 1
            if c > 0:
                taken_options += 1
            options.append({"id": opt.get("id"), "label": opt.get("label", ""),
                            "count": c, "taken": c > 0})
        per_node.append({"id": node.get("id"), "title": node.get("title", ""),
                         "options": options})

    never_taken = (
        [
            {"node_id": nid, "option_id": oid}
            for nid, oids in counts.items()
            for oid, c in oids.items()
            if c == 0
        ]
        if run_count else []
    )
    return {
        "run_count": run_count,
        "choice_count": choice_count,
        "total_options": total_options,
        "taken_options": taken_options,
        "never_taken_count": total_options - taken_options,
        "coverage_ratio": round(taken_options / total_options, 4) if total_options else 0.0,
        "stale_choices": stale_choices,
        "nodes": per_node,
        "never_taken": never_taken,
    }
