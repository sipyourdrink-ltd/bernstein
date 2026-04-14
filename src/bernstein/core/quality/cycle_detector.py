"""Dependency cycle detection for task graphs before plan execution.

Runs DFS on the task dependency graph and reports all cycles clearly,
including the full path of each cycle found.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CycleReport:
    """Result of cycle detection on a task graph.

    Attributes:
        has_cycles: True if at least one cycle was found.
        cycles: List of cycles, each a list of task IDs forming the cycle.
        summary: Human-readable summary of all detected cycles.
    """

    has_cycles: bool
    cycles: list[list[str]] = field(default_factory=list[list[str]])
    summary: str = ""


def _dfs_visit(
    node: str,
    adjacency: dict[str, list[str]],
    colour: dict[str, int],
    parent: dict[str, str | None],
    cycles: list[list[str]],
    seen_cycle_sets: list[frozenset[str]],
) -> None:
    """DFS visit for cycle detection using three-colour algorithm.

    Args:
        node: Current node to visit.
        adjacency: Adjacency list for the graph.
        colour: Mutable colour map (0=WHITE, 1=GREY, 2=BLACK).
        parent: Mutable parent pointer map.
        cycles: Mutable list collecting discovered cycles.
        seen_cycle_sets: Mutable dedup list of cycle frozensets.
    """
    colour[node] = 1  # GREY
    for neighbour in adjacency.get(node, []):
        if colour[neighbour] == 1:  # GREY — back edge
            cycle = _extract_cycle(node, neighbour, parent)
            cycle_set = frozenset(cycle)
            if cycle_set not in seen_cycle_sets:
                seen_cycle_sets.append(cycle_set)
                cycles.append(cycle)
        elif colour[neighbour] == 0:  # WHITE
            parent[neighbour] = node
            _dfs_visit(
                node=neighbour,
                adjacency=adjacency,
                colour=colour,
                parent=parent,
                cycles=cycles,
                seen_cycle_sets=seen_cycle_sets,
            )
    colour[node] = 2  # BLACK


def _format_cycle_summary(cycles: list[list[str]]) -> str:
    """Format detected cycles into a human-readable summary string."""
    lines: list[str] = [f"Found {len(cycles)} dependency cycle(s):"]
    for i, cycle in enumerate(cycles, 1):
        path_str = " -> ".join(cycle) + f" -> {cycle[0]}"
        lines.append(f"  Cycle {i}: {path_str}")
    return "\n".join(lines)


def detect_cycles(tasks: Sequence[Task]) -> CycleReport:
    """Run DFS-based cycle detection on the task dependency graph.

    Finds all distinct cycles in the directed graph formed by task
    ``depends_on`` relationships.  Only considers edges whose target
    is present in the task set (dangling references are ignored).

    Args:
        tasks: Sequence of tasks to analyse.

    Returns:
        CycleReport with all detected cycles.
    """
    task_ids: set[str] = {t.id for t in tasks}
    adjacency: dict[str, list[str]] = {}
    for t in tasks:
        adjacency[t.id] = [dep for dep in t.depends_on if dep in task_ids]

    colour: dict[str, int] = dict.fromkeys(task_ids, 0)
    parent: dict[str, str | None] = dict.fromkeys(task_ids, None)
    cycles: list[list[str]] = []
    seen_cycle_sets: list[frozenset[str]] = []

    for tid in task_ids:
        if colour[tid] == 0:  # WHITE
            _dfs_visit(tid, adjacency, colour, parent, cycles, seen_cycle_sets)

    if not cycles:
        return CycleReport(has_cycles=False, summary="No dependency cycles detected.")

    summary = _format_cycle_summary(cycles)
    logger.warning(summary)
    return CycleReport(has_cycles=True, cycles=cycles, summary=summary)


def _extract_cycle(
    current: str,
    back_target: str,
    parent: dict[str, str | None],
) -> list[str]:
    """Extract the cycle path from *back_target* back to itself via parent pointers.

    Args:
        current: Node where the back edge originates.
        back_target: Node where the back edge points (ancestor in DFS tree).
        parent: Parent map from DFS traversal.

    Returns:
        List of task IDs forming the cycle.
    """
    cycle: list[str] = [back_target]
    node: str | None = current
    while node is not None and node != back_target:
        cycle.append(node)
        node = parent.get(node)
    cycle.reverse()
    return cycle


def validate_plan_acyclic(tasks: Sequence[Task]) -> CycleReport:
    """Validate that a plan's task graph has no dependency cycles.

    Intended to be called before plan execution begins.  Raises no
    exceptions -- the caller inspects ``CycleReport.has_cycles`` and
    decides whether to abort.

    Args:
        tasks: Tasks loaded from a plan file.

    Returns:
        CycleReport describing any cycles found.
    """
    report = detect_cycles(tasks)
    if report.has_cycles:
        logger.error("Plan has dependency cycles -- execution may deadlock.\n%s", report.summary)
    else:
        logger.info("Plan dependency graph is acyclic -- safe to execute.")
    return report
