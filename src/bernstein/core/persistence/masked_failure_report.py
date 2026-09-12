"""Masked failure report: count consecutive failures before a success (#5106).

A *masked failure* is a task run that eventually succeeds but has one or more
consecutive failed attempts before the final success.  The failure is
``masked`` because the run-level outcome is ``success`` even though the task
needed multiple tries to get there.

This module provides a pure projection over an ordered sequence of attempt
records (each carrying a ``task_id`` and a transition ``kind`` from the work
ledger vocabulary).  It never reads disk directly; callers supply the records
from wherever they retrieved them so the logic is independently testable.

Typical use::

    from bernstein.core.persistence.work_ledger import (
        KIND_TASK_COMPLETED, KIND_TASK_FAILED,
    )
    from bernstein.core.persistence.masked_failure_report import (
        AttemptRecord, scan_for_masked_failures,
    )

    attempts = [
        AttemptRecord(task_id="t1", kind=KIND_TASK_FAILED),
        AttemptRecord(task_id="t1", kind=KIND_TASK_FAILED),
        AttemptRecord(task_id="t1", kind=KIND_TASK_COMPLETED),
    ]
    reports = scan_for_masked_failures(attempts)
    # reports[0].failure_count_before_success == 2
    # reports[0].is_masked == True
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.persistence.work_ledger import (
    KIND_TASK_ABANDONED,
    KIND_TASK_COMPLETED,
    KIND_TASK_FAILED,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Terminal kinds that end an attempt sequence for a task.
_TERMINAL_KINDS: frozenset[str] = frozenset(
    {
        KIND_TASK_COMPLETED,
        KIND_TASK_FAILED,
        KIND_TASK_ABANDONED,
    }
)

#: The kind that counts as a successful terminal outcome.
_SUCCESS_KIND: str = KIND_TASK_COMPLETED

#: The kind that counts as a failed attempt.
_FAILURE_KIND: str = KIND_TASK_FAILED


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One attempt transition from the work ledger.

    Attributes:
        task_id: Identifier of the task this transition concerns.
        kind: Transition kind (e.g. ``task.failed``, ``task.completed``).
    """

    task_id: str
    kind: str


@dataclass(frozen=True, slots=True)
class MaskedFailureReport:
    """Summary of attempt history for one task.

    Attributes:
        task_id: The task these attempts belong to.
        attempt_count: Total number of terminal-kind transitions seen.
        failure_count_before_success: Consecutive ``task.failed`` transitions
            immediately before the final ``task.completed``.  Zero if the task
            succeeded on the first try or never succeeded.
        final_outcome: The terminal kind of the last attempt: ``task.completed``,
            ``task.failed``, or ``task.abandoned``.
        is_masked: ``True`` when the task eventually completed but had at least
            one failure attempt before it -- the outcome is a success that hid
            prior failures.
    """

    task_id: str
    attempt_count: int
    failure_count_before_success: int
    final_outcome: str
    is_masked: bool


def count_masked_failures(task_id: str, attempts: Sequence[AttemptRecord]) -> MaskedFailureReport:
    """Return a :class:`MaskedFailureReport` for *task_id* over *attempts*.

    Only records whose ``task_id`` equals *task_id* are considered; the rest
    are ignored so the caller may pass a mixed sequence and filter implicitly.

    The failure count is the number of consecutive ``task.failed`` transitions
    immediately before the last ``task.completed`` in the filtered sequence.
    Failures that appear *after* an earlier success (e.g. a task that was
    re-run) are not included in ``failure_count_before_success``.

    Args:
        task_id: Which task to report on.
        attempts: Ordered sequence of attempt records (oldest first).

    Returns:
        A :class:`MaskedFailureReport` for the task.
    """
    task_attempts = [a for a in attempts if a.task_id == task_id and a.kind in _TERMINAL_KINDS]

    if not task_attempts:
        return MaskedFailureReport(
            task_id=task_id,
            attempt_count=0,
            failure_count_before_success=0,
            final_outcome="",
            is_masked=False,
        )

    final = task_attempts[-1]
    attempt_count = len(task_attempts)

    if final.kind != _SUCCESS_KIND:
        return MaskedFailureReport(
            task_id=task_id,
            attempt_count=attempt_count,
            failure_count_before_success=0,
            final_outcome=final.kind,
            is_masked=False,
        )

    failure_count = 0
    for attempt in reversed(task_attempts[:-1]):
        if attempt.kind == _FAILURE_KIND:
            failure_count += 1
        else:
            break

    return MaskedFailureReport(
        task_id=task_id,
        attempt_count=attempt_count,
        failure_count_before_success=failure_count,
        final_outcome=final.kind,
        is_masked=failure_count > 0,
    )


def scan_for_masked_failures(attempts: Sequence[AttemptRecord]) -> list[MaskedFailureReport]:
    """Return one :class:`MaskedFailureReport` per distinct task id in *attempts*.

    Tasks are returned in the order their first attempt record appears.  Only
    tasks that have at least one terminal-kind transition are included.

    Args:
        attempts: Ordered sequence of attempt records (oldest first).

    Returns:
        List of reports, one per task id that has any terminal transition.
    """
    seen_order: list[str] = []
    task_ids: set[str] = set()
    for attempt in attempts:
        if attempt.kind in _TERMINAL_KINDS and attempt.task_id not in task_ids:
            seen_order.append(attempt.task_id)
            task_ids.add(attempt.task_id)

    return [count_masked_failures(tid, attempts) for tid in seen_order]
