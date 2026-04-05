"""ASCII-art dependency graph renderer for tasks.

Renders task dependencies as a visual graph in the terminal,
color-coded by status.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

# Status tag labels
_STATUS_LABEL: dict[str, str] = {
    "done": "DONE",
    "in_progress": "IN_PROGRESS",
    "failed": "FAILED",
    "open": "OPEN",
    "claimed": "CLAIMED",
    "blocked": "BLOCKED",
    "cancelled": "CANCELLED",
    "planned": "PLANNED",
    "closed": "CLOSED",
}


def _status_tag(status: str) -> str:
    """Return a bracketed status label like [DONE]."""
    label = _STATUS_LABEL.get(status, status.upper())
    return f"[{label}]"


def _topological_sort(
    nodes: list[str],
    forward: dict[str, list[str]],
    reverse: dict[str, list[str]],
) -> list[str]:
    """Kahn's algorithm. Returns nodes in dependency order."""
    in_degree: dict[str, int] = dict.fromkeys(nodes, 0)
    for node in nodes:
        for child in forward.get(node, []):
            if child in in_degree:
                in_degree[child] += 1

    queue: deque[str] = deque(n for n, d in in_degree.items() if d == 0)
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for child in forward.get(node, []):
            if child in in_degree:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

    # If cycle detected, return whatever we have plus remaining
    if len(order) != len(nodes):
        remaining = [n for n in nodes if n not in set(order)]
        order.extend(remaining)
    return order


def render_dependency_graph(tasks: list[dict[str, Any]]) -> str:
    """Render an ASCII dependency graph for a list of tasks.

    Each task dict should have:
        - id: str
        - title: str
        - status: str (e.g. "done", "in_progress", "failed", "open")
        - depends_on: list[str] (task IDs this task depends on)

    Returns a multi-line string with ASCII art showing dependency arrows.
    """
    if not tasks:
        return "(no tasks)"

    # Index tasks
    by_id: dict[str, dict[str, Any]] = {t["id"]: t for t in tasks}

    # Build adjacency: forward = dependency -> dependents
    forward: dict[str, list[str]] = defaultdict(list)
    reverse: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        raw_deps: list[str] = task.get("depends_on") or []
        task_id: str = task["id"]
        for dep_id in raw_deps:
            if dep_id in by_id:
                forward[dep_id].append(task_id)
                reverse[task_id].append(dep_id)

    # Topological sort
    all_ids = list(by_id.keys())
    topo_order = _topological_sort(all_ids, forward, reverse)

    # Group tasks by their dependents (which task they feed into)
    # Find tasks that are "targets" (have dependencies feeding into them)
    lines: list[str] = []

    # Track which tasks have been rendered
    rendered: set[str] = set()

    for tid in topo_order:
        if tid in rendered:
            continue

        children = forward.get(tid, [])
        if not children:
            # Leaf or standalone task -- render solo
            task = by_id[tid]
            tag = _status_tag(task["status"])
            title = task["title"]
            lines.append(f"{tag} {title}")
            rendered.add(tid)
            continue

        # For each child, collect all parents that feed into it
        for child_id in children:
            if child_id in rendered:
                continue
            parents = reverse.get(child_id, [])
            if not parents:
                continue

            # Only render this group if we haven't rendered the child yet
            child_task = by_id[child_id]
            child_tag = _status_tag(child_task["status"])
            child_title = child_task["title"]

            # Render parent lines with connectors
            parent_entries: list[tuple[str, str, str]] = []
            for pid in parents:
                ptask = by_id[pid]
                ptag = _status_tag(str(ptask["status"]))
                ptitle = str(ptask["title"])
                parent_entries.append((ptag, ptitle, pid))
                rendered.add(pid)

            if len(parent_entries) == 1:
                # Single dependency: simple arrow
                ptag, ptitle, _ = parent_entries[0]
                lines.append(f"{ptag} {ptitle} --> {child_tag} {child_title}")
            else:
                # Multiple dependencies converging
                # Find max width for alignment
                formatted = [f"{ptag} {ptitle}" for ptag, ptitle, _ in parent_entries]
                max_width = max(len(f) for f in formatted)

                for fmt in formatted:
                    padding = " " * (max_width - len(fmt))
                    lines.append(f"{fmt}{padding} --+")

                # Add the connector line pointing to child
                indent = " " * max_width
                lines.append(f"{indent}   +-- {child_tag} {child_title}")

            rendered.add(child_id)

        # Render the current task if not yet rendered (standalone)
        if tid not in rendered:
            task = by_id[tid]
            tag = _status_tag(task["status"])
            title = task["title"]
            lines.append(f"{tag} {title}")
            rendered.add(tid)

    # Render any remaining unrendered tasks
    for tid in topo_order:
        if tid not in rendered:
            task = by_id[tid]
            tag = _status_tag(task["status"])
            title = task["title"]
            lines.append(f"{tag} {title}")
            rendered.add(tid)

    return "\n".join(lines)


def render_dependency_graph_rich(tasks: list[dict[str, Any]]) -> str:
    """Render the dependency graph with Rich color markup on status tags."""
    plain = render_dependency_graph(tasks)

    _COLOR_MAP: dict[str, str] = {
        "[DONE]": "[green][DONE][/green]",
        "[CLOSED]": "[green][CLOSED][/green]",
        "[IN_PROGRESS]": "[yellow][IN_PROGRESS][/yellow]",
        "[CLAIMED]": "[yellow][CLAIMED][/yellow]",
        "[FAILED]": "[red][FAILED][/red]",
        "[OPEN]": "[white][OPEN][/white]",
        "[PLANNED]": "[white][PLANNED][/white]",
        "[BLOCKED]": "[white][BLOCKED][/white]",
        "[CANCELLED]": "[dim][CANCELLED][/dim]",
    }

    result = plain
    for tag, colored in _COLOR_MAP.items():
        result = result.replace(tag, colored)
    return result
