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


def _compute_in_degrees(
    nodes: list[str],
    forward: dict[str, list[str]],
) -> dict[str, int]:
    """Compute in-degree for each node based on forward edges."""
    in_degree: dict[str, int] = dict.fromkeys(nodes, 0)
    for node in nodes:
        for child in forward.get(node, []):
            if child in in_degree:
                in_degree[child] += 1
    return in_degree


def _topological_sort(
    nodes: list[str],
    forward: dict[str, list[str]],
    _reverse: dict[str, list[str]],
) -> list[str]:
    """Kahn's algorithm. Returns nodes in dependency order."""
    in_degree = _compute_in_degrees(nodes, forward)

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


def _build_adjacency(
    tasks: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Build forward (parent->children) and reverse (child->parents) adjacency."""
    forward: dict[str, list[str]] = defaultdict(list)
    reverse: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        raw_deps: list[str] = task.get("depends_on") or []
        task_id: str = task["id"]
        for dep_id in raw_deps:
            if dep_id in by_id:
                forward[dep_id].append(task_id)
                reverse[task_id].append(dep_id)
    return forward, reverse


def _render_task_line(task: dict[str, Any]) -> str:
    """Format a standalone task as '[STATUS] title'."""
    return f"{_status_tag(task['status'])} {task['title']}"


def _render_convergence_group(
    parent_entries: list[tuple[str, str, str]],
    child_tag: str,
    child_title: str,
) -> list[str]:
    """Render a group of parents converging into a single child."""
    if len(parent_entries) == 1:
        ptag, ptitle, _ = parent_entries[0]
        return [f"{ptag} {ptitle} --> {child_tag} {child_title}"]

    # Multiple dependencies converging
    formatted = [f"{ptag} {ptitle}" for ptag, ptitle, _ in parent_entries]
    max_width = max(len(f) for f in formatted)
    lines: list[str] = []
    for fmt in formatted:
        padding = " " * (max_width - len(fmt))
        lines.append(f"{fmt}{padding} --+")
    indent = " " * max_width
    lines.append(f"{indent}   +-- {child_tag} {child_title}")
    return lines


def _render_children_group(
    children: list[str],
    by_id: dict[str, dict[str, Any]],
    reverse: dict[str, list[str]],
    rendered: set[str],
) -> list[str]:
    """Render convergence groups for child tasks that have parents."""
    lines: list[str] = []
    for child_id in children:
        if child_id in rendered:
            continue
        parents = reverse.get(child_id, [])
        if not parents:
            continue

        child_task = by_id[child_id]
        child_tag = _status_tag(child_task["status"])

        parent_entries: list[tuple[str, str, str]] = []
        for pid in parents:
            ptask = by_id[pid]
            parent_entries.append((_status_tag(str(ptask["status"])), str(ptask["title"]), pid))
            rendered.add(pid)

        lines.extend(_render_convergence_group(parent_entries, child_tag, child_task["title"]))
        rendered.add(child_id)
    return lines


def _render_unrendered(
    topo_order: list[str],
    by_id: dict[str, dict[str, Any]],
    rendered: set[str],
) -> list[str]:
    """Render any tasks not yet in the rendered set."""
    lines: list[str] = []
    for tid in topo_order:
        if tid not in rendered:
            lines.append(_render_task_line(by_id[tid]))
            rendered.add(tid)
    return lines


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

    by_id: dict[str, dict[str, Any]] = {t["id"]: t for t in tasks}
    forward, reverse = _build_adjacency(tasks, by_id)
    topo_order = _topological_sort(list(by_id.keys()), forward, reverse)

    lines: list[str] = []
    rendered: set[str] = set()

    for tid in topo_order:
        if tid in rendered:
            continue
        children = forward.get(tid, [])
        if children:
            lines.extend(_render_children_group(children, by_id, reverse, rendered))
        if tid not in rendered:
            lines.append(_render_task_line(by_id[tid]))
            rendered.add(tid)

    lines.extend(_render_unrendered(topo_order, by_id, rendered))
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
