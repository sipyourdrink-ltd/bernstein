"""Derived unreachable-task projection (#3452).

A task whose dependency ended without delivering can never become claimable,
but nothing in the record used to say so: the dependent kept its ``OPEN``
status and was pushed back onto the claim heap every tick. This module is the
predicate that answers "which tasks could never have run, and because of
what", computed from the recorded task graph alone.

Everything here is pure. The same task set produces the same answer in the
same order regardless of insertion order, so two operators reading the same
journal derive an identical set and can compare them directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.tasks.lifecycle import (
    DEPENDENCY_BLOCKED_STATUSES,
    SUCCESSFUL_TASK_STATUSES,
    UNSUCCESSFUL_TERMINAL_STATUSES,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from bernstein.core.tasks.models import Task

__all__ = ["blocking_dependency", "unreachable_tasks"]


def dependency_can_never_satisfy(dependency: Task) -> bool:
    """Can ``dependency``, in its recorded status, ever satisfy a dependent?

    The single place this question is answered about a status. Three callers
    ask it - ``TaskStore._dependencies_satisfied`` when deciding whether a
    claim may proceed, ``DAGExecutor.resolve_edge`` when resolving an edge,
    and ``blocking_dependency`` below - and before this they each carried
    their own copy of the status logic with nothing keeping the three in step.

    Status is all this answers. Whether a *retry* of the dependency is still
    in flight is a question about the task table rather than about one status,
    so ``blocking_dependency`` asks that separately.
    """
    return dependency.status in UNSUCCESSFUL_TERMINAL_STATUSES


def _is_task_succeeded_or_retrying(dep_id: str, tasks: Mapping[str, Task]) -> bool:
    """Return True if dep_id or a retry of dep_id is active or succeeded."""
    for t in tasks.values():
        if t.id == dep_id and t.status not in UNSUCCESSFUL_TERMINAL_STATUSES:
            return True
        if isinstance(t.metadata, dict):
            orig = t.metadata.get("original_task_id")
            retry_of = t.metadata.get("retry_of")
            if (orig == dep_id or retry_of == dep_id) and t.status not in UNSUCCESSFUL_TERMINAL_STATUSES:
                return True
    return False


def blocking_dependency(task: Task, tasks: Mapping[str, Task]) -> str | None:
    """Return the id of the dependency that strands *task*, if any.

    Only direct dependencies are considered, and only those already recorded
    in an unsuccessful terminal status with no active or succeeded retry. A
    task stranded transitively is found through its own direct dependency once
    that dependency has itself been moved to a dependency-blocked status - which
    is what makes the recorded cause of a transitive strand the nearest link
    in the chain rather than the original failure.

    With several stranded dependencies the lowest id wins, so the recorded
    cause does not depend on ``depends_on`` ordering.
    """
    blockers = sorted(
        dep_id
        for dep_id in task.depends_on
        if (dep := tasks.get(dep_id)) is not None
        and dependency_can_never_satisfy(dep)
        and not _is_task_succeeded_or_retrying(dep_id, tasks)
    )
    return blockers[0] if blockers else None


def unreachable_tasks(tasks: Iterable[Task]) -> list[tuple[str, str]]:
    """Return ``(task_id, blocking_task_id)`` for every task that cannot run.

    A task is unreachable when it has not reached a successful status, has not
    itself ended by running, and at least one of its dependencies is either in
    an unsuccessful terminal status or is itself unreachable.

    The result is sorted by task id, and each blocking id is the lowest
    qualifying dependency id, so the projection is a function of the graph and
    not of traversal order.
    """
    by_id = {task.id: task for task in tasks}

    # A task that ran and ended - failed, was cancelled, refused, orphaned or
    # abandoned - is a root cause, not a casualty. The dependency-blocked
    # statuses are the casualties already on record, and stay in the answer.
    self_ended = UNSUCCESSFUL_TERMINAL_STATUSES - DEPENDENCY_BLOCKED_STATUSES
    reportable = {
        task_id
        for task_id, task in by_id.items()
        if task.status not in SUCCESSFUL_TASK_STATUSES and task.status not in self_ended
    }

    def _strands(dep_id: str, stranded: set[str]) -> bool:
        dep = by_id.get(dep_id)
        return dep is not None and (dep.status in UNSUCCESSFUL_TERMINAL_STATUSES or dep_id in stranded)

    # Phase 1 - fixpoint over the set. Iterating to convergence rather than
    # walking edges once means the answer does not depend on the order tasks
    # were inserted, only on which edges exist.
    stranded: set[str] = set()
    changed = True
    while changed:
        changed = False
        for task_id in sorted(reportable - stranded):
            if any(_strands(dep_id, stranded) for dep_id in by_id[task_id].depends_on):
                stranded.add(task_id)
                changed = True

    # Phase 2 - name the cause once the set is final, so a task whose cause
    # only becomes stranded on a later pass still reports the lowest id.
    return sorted(
        (task_id, min(dep_id for dep_id in by_id[task_id].depends_on if _strands(dep_id, stranded)))
        for task_id in stranded
    )
