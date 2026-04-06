"""Atomic batch transitions for plan stage completion.

When all tasks in a plan stage complete, this module transitions them
atomically to ensure consistent state.  If any transition in the batch
fails, all transitions are rolled back.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.models import TaskStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransitionSpec:
    """Specification for a single task transition within a batch.

    Attributes:
        task_id: The task to transition.
        from_status: Expected current status.
        to_status: Target status.
    """

    task_id: str
    from_status: TaskStatus
    to_status: TaskStatus


@dataclass(frozen=True)
class BatchTransitionResult:
    """Result of an atomic batch transition.

    Attributes:
        success: True if all transitions completed successfully.
        transitioned: List of task IDs that were transitioned.
        failed: List of (task_id, reason) for tasks that failed.
        rolled_back: True if a rollback was performed due to partial failure.
    """

    success: bool
    transitioned: list[str] = field(default_factory=list[str])
    failed: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    rolled_back: bool = False


def _check_preconditions(
    tasks: dict[str, Task],
    specs: Sequence[TransitionSpec],
) -> list[tuple[str, str]]:
    """Check that all tasks are in their expected status before transition.

    Args:
        tasks: Mapping of task_id -> Task.
        specs: Transition specifications to validate.

    Returns:
        List of (task_id, error_message) for any precondition failures.
    """
    errors: list[tuple[str, str]] = []
    for spec in specs:
        task = tasks.get(spec.task_id)
        if task is None:
            errors.append((spec.task_id, f"Task {spec.task_id} not found"))
        elif task.status != spec.from_status:
            errors.append(
                (
                    spec.task_id,
                    f"Expected status {spec.from_status.value}, got {task.status.value}",
                )
            )
    return errors


def apply_batch_transition(
    tasks: Sequence[Task],
    specs: Sequence[TransitionSpec],
) -> BatchTransitionResult:
    """Apply a batch of status transitions atomically.

    If any task's precondition fails (wrong current status, missing task),
    the entire batch is aborted.  If a transition fails mid-batch, all
    previously applied transitions are rolled back.

    Args:
        tasks: All tasks (used for lookup and mutation).
        specs: Transition specifications to apply.

    Returns:
        BatchTransitionResult with outcome details.
    """
    task_map: dict[str, Task] = {t.id: t for t in tasks}

    # Phase 1: Precondition check
    errors = _check_preconditions(task_map, specs)
    if errors:
        return BatchTransitionResult(success=False, failed=errors)

    # Phase 2: Apply transitions, tracking originals for rollback
    originals: dict[str, TaskStatus] = {}
    transitioned: list[str] = []
    failed: list[tuple[str, str]] = []

    for spec in specs:
        task = task_map[spec.task_id]
        originals[spec.task_id] = task.status
        try:
            task.status = spec.to_status
            transitioned.append(spec.task_id)
        except Exception as exc:
            failed.append((spec.task_id, str(exc)))
            break

    # Phase 3: If any failed mid-batch, rollback
    if failed:
        for tid in transitioned:
            task_map[tid].status = originals[tid]
        logger.warning(
            "Batch transition rolled back: %d transitioned, %d failed",
            len(transitioned),
            len(failed),
        )
        return BatchTransitionResult(
            success=False,
            transitioned=[],
            failed=failed,
            rolled_back=True,
        )

    logger.info("Batch transition complete: %d task(s) transitioned", len(transitioned))
    return BatchTransitionResult(success=True, transitioned=transitioned)


def complete_stage(
    tasks: Sequence[Task],
    stage_task_ids: Sequence[str],
) -> BatchTransitionResult:
    """Atomically transition all tasks in a plan stage to DONE.

    Only transitions tasks that are currently in CLAIMED or IN_PROGRESS status.
    Tasks already DONE are skipped (not counted as failures).

    Args:
        tasks: All tasks in the plan.
        stage_task_ids: IDs of tasks belonging to the stage to complete.

    Returns:
        BatchTransitionResult with outcome details.
    """
    task_map: dict[str, Task] = {t.id: t for t in tasks}
    completable_statuses = {TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS}

    specs: list[TransitionSpec] = []
    for tid in stage_task_ids:
        task = task_map.get(tid)
        if task is None:
            continue
        if task.status in completable_statuses:
            specs.append(
                TransitionSpec(
                    task_id=tid,
                    from_status=task.status,
                    to_status=TaskStatus.DONE,
                )
            )

    if not specs:
        return BatchTransitionResult(success=True, transitioned=[])

    now = time.time()
    result = apply_batch_transition(tasks, specs)
    if result.success:
        for tid in result.transitioned:
            task_map[tid].completed_at = now
    return result


def fail_stage(
    tasks: Sequence[Task],
    stage_task_ids: Sequence[str],
    *,
    reason: str = "stage failure",
) -> BatchTransitionResult:
    """Atomically transition all non-terminal tasks in a stage to FAILED.

    Args:
        tasks: All tasks in the plan.
        stage_task_ids: IDs of tasks belonging to the failing stage.
        reason: Reason for the failure.

    Returns:
        BatchTransitionResult with outcome details.
    """
    task_map: dict[str, Task] = {t.id: t for t in tasks}
    terminal_statuses = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CLOSED, TaskStatus.CANCELLED}

    specs: list[TransitionSpec] = []
    for tid in stage_task_ids:
        task = task_map.get(tid)
        if task is None:
            continue
        if task.status not in terminal_statuses:
            specs.append(
                TransitionSpec(
                    task_id=tid,
                    from_status=task.status,
                    to_status=TaskStatus.FAILED,
                )
            )

    if not specs:
        return BatchTransitionResult(success=True, transitioned=[])

    result = apply_batch_transition(tasks, specs)
    if result.success:
        for tid in result.transitioned:
            task_map[tid].result_summary = reason
    return result
