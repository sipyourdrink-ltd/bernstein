"""Dependency validation and scheduling helpers built on TaskGraph."""

from __future__ import annotations

from dataclasses import dataclass

from bernstein.core.graph import TaskGraph
from bernstein.core.models import Task, TaskStatus


@dataclass(frozen=True)
class DepValidationResult:
    """Result of validating a task dependency graph."""

    valid: bool
    cycles: list[list[str]]
    missing_deps: list[tuple[str, str]]
    stuck_deps: list[tuple[str, str, str]]
    warnings: list[str]


class DependencyValidator:
    """Validate task dependency graphs before scheduling."""

    _STUCK_STATUSES = frozenset({TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.BLOCKED})

    def validate(self, tasks: list[Task]) -> DepValidationResult:
        """Run full validation for the current task set."""
        task_map = {task.id: task for task in tasks}
        missing: list[tuple[str, str]] = []
        stuck: list[tuple[str, str, str]] = []
        warnings: list[str] = []

        for task in tasks:
            if len(task.depends_on) > 3:
                warnings.append(f"Task {task.id} has high fan-in ({len(task.depends_on)} dependencies)")
            for dep_id in task.depends_on:
                dep = task_map.get(dep_id)
                if dep is None:
                    missing.append((task.id, dep_id))
                    continue
                if dep.status in self._STUCK_STATUSES:
                    stuck.append((task.id, dep_id, dep.status.value))

        cycles = self._find_cycles(tasks)
        depths = self._depths(tasks)
        for task_id, depth in depths.items():
            if depth > 5:
                warnings.append(f"Task {task_id} sits on a deep dependency chain ({depth} levels)")

        return DepValidationResult(
            valid=not cycles and not missing and not stuck,
            cycles=cycles,
            missing_deps=missing,
            stuck_deps=stuck,
            warnings=warnings,
        )

    def topological_order(self, tasks: list[Task]) -> list[str]:
        """Return dependency-respecting order or raise on cycles."""
        order = TaskGraph(tasks).topological_order()
        if not order and tasks:
            raise ValueError("Task dependency graph contains a cycle")
        return order

    def critical_path(self, tasks: list[Task]) -> list[str]:
        """Return the longest dependency chain."""
        return TaskGraph(tasks).critical_path()

    def ready_tasks(self, tasks: list[Task]) -> list[Task]:
        """Return tasks whose blocking dependencies are all done."""
        graph = TaskGraph(tasks)
        ready_ids = set(graph.ready_tasks())
        return [task for task in tasks if task.id in ready_ids]

    def _find_cycles(self, tasks: list[Task]) -> list[list[str]]:
        """Detect simple cycles via DFS."""
        adjacency = {task.id: [dep for dep in task.depends_on if dep in {t.id for t in tasks}] for task in tasks}
        visited: set[str] = set()
        stack: list[str] = []
        on_stack: set[str] = set()
        cycles: list[list[str]] = []

        def _visit(node: str) -> None:
            visited.add(node)
            stack.append(node)
            on_stack.add(node)
            for dep in adjacency.get(node, []):
                if dep not in visited:
                    _visit(dep)
                elif dep in on_stack:
                    idx = stack.index(dep)
                    cycle = [*stack[idx:], dep]
                    if cycle not in cycles:
                        cycles.append(cycle)
            stack.pop()
            on_stack.discard(node)

        for task in tasks:
            if task.id not in visited:
                _visit(task.id)
        return cycles

    def _depths(self, tasks: list[Task]) -> dict[str, int]:
        """Compute dependency-chain depth per task."""
        task_map = {task.id: task for task in tasks}
        memo: dict[str, int] = {}
        visiting: set[str] = set()

        def _depth(task_id: str) -> int:
            if task_id in memo:
                return memo[task_id]
            if task_id in visiting:
                return 0
            visiting.add(task_id)
            task = task_map[task_id]
            if not task.depends_on:
                memo[task_id] = 1
                visiting.discard(task_id)
                return 1
            value = 1 + max((_depth(dep_id) for dep_id in task.depends_on if dep_id in task_map), default=0)
            memo[task_id] = value
            visiting.discard(task_id)
            return value

        for task in tasks:
            _depth(task.id)
        return memo
