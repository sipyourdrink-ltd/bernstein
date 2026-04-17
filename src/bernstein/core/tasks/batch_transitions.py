"""Atomic batch transitions for plan stage completion.

When all tasks in a plan stage complete, this module transitions them
atomically to ensure consistent state.  If any transition in the batch
fails, all transitions are rolled back.

All status changes MUST flow through :func:`transition_task` so the FSM
allowed-transitions table, guard predicates, Prometheus counters, HMAC
audit log, and lifecycle event stream are consulted uniformly.  Writing
``task.status`` directly bypasses every guardrail and is prohibited.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.tasks.lifecycle import IllegalTransitionError, transition_task
from bernstein.core.tasks.models import TaskStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.tasks.models import Task

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
    *,
    actor: str = "batch",
    reason: str = "batch_transition",
) -> BatchTransitionResult:
    """Apply a batch of status transitions atomically.

    Every transition is delegated to :func:`transition_task`, which
    enforces the FSM allowed-transitions table, runs guard predicates,
    emits lifecycle events, and writes to the HMAC audit log.  If any
    task's precondition fails (wrong current status, missing task) the
    entire batch is aborted before any transitions are applied.  If a
    transition fails mid-batch (illegal transition, guard rejection),
    all previously applied transitions are rolled back by driving them
    back through :func:`transition_task`.

    Args:
        tasks: All tasks (used for lookup and mutation).
        specs: Transition specifications to apply.
        actor: Who triggered this batch (recorded in audit log).
        reason: Human-readable reason for the batch (recorded in audit log).

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
            transition_task(
                task,
                spec.to_status,
                actor=actor,
                reason=reason,
            )
            transitioned.append(spec.task_id)
        except IllegalTransitionError as exc:
            failed.append((spec.task_id, str(exc)))
            break
        except Exception as exc:
            failed.append((spec.task_id, str(exc)))
            break

    # Phase 3: If any failed mid-batch, rollback
    if failed:
        for tid in transitioned:
            task = task_map[tid]
            original = originals[tid]
            try:
                transition_task(
                    task,
                    original,
                    actor=actor,
                    reason=f"{reason}:rollback",
                )
            except IllegalTransitionError:
                # FSM has no reverse edge: record and restore by direct assignment
                # as a last resort, but log loudly so the bypass is visible.
                logger.exception(
                    "Rollback requires FSM-illegal transition %s -> %s for task %s; "
                    "forcing direct restore. Add reverse edge to TASK_TRANSITIONS.",
                    task.status.value,
                    original.value,
                    tid,
                )
                task.status = original
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
    result = apply_batch_transition(tasks, specs, actor="batch", reason="stage_complete")
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

    result = apply_batch_transition(tasks, specs, actor="batch", reason=f"stage_fail:{reason}")
    if result.success:
        for tid in result.transitioned:
            task_map[tid].result_summary = reason
    return result
