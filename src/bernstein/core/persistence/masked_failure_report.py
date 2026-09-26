"""Masked failure report: count consecutive failures before a success (#5106).

A *masked failure* is a task run that eventually succeeds but has one or more
consecutive failed attempts before the final success.  The failure is
``masked`` because the run-level outcome is ``success`` even though the task
needed multiple tries to get there.

This module provides a pure projection over an ordered sequence of attempt
records (each carrying a ``task_id`` and a transition ``kind`` from the work
ledger vocabulary).  It never reads disk directly; callers supply the records
from wherever they retrieved them so the logic is independently testable.

Note: tasks that are in-flight (no terminal-kind transition yet) are excluded
from the report entirely -- they appear in the ledger but not in the output.

Typical use::

    from bernstein.core.persistence.work_ledger import (
        KIND_TASK_COMPLETED, KIND_TASK_FAILED,
        LedgerEntry,
    )
    from bernstein.core.persistence.masked_failure_report import (
        scan_for_masked_failures,
    )

    # Assuming `ledger` is a WorkLedger instance.
    entries = ledger.read_all()
    reports = scan_for_masked_failures(entries)
    # reports[0].failure_count_before_success == 2
    # reports[0].is_masked == True
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from bernstein.core.persistence.work_ledger import (
    KIND_TASK_ABANDONED,
    KIND_TASK_COMPLETED,
    KIND_TASK_FAILED,
    TASK_KINDS,
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

#: Non-terminal kinds that do not end an attempt sequence.
_NON_TERMINAL_KINDS: frozenset[str] = TASK_KINDS - _TERMINAL_KINDS

#: The kind that counts as a successful terminal outcome.
_SUCCESS_KIND: str = KIND_TASK_COMPLETED

#: The kind that counts as a failed attempt.
_FAILURE_KIND: str = KIND_TASK_FAILED


class AttemptLike(Protocol):
    """Protocol for an attempt record that has a task_id and a kind."""

    task_id: str
    kind: str


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
class TaskAttemptMaskedFailureReport:
    """Summary of attempt history for one task.

    Attributes:
        task_id: The task these attempts belong to.
        terminal_transition_count: Total number of terminal-kind transitions seen.
        failure_count_before_success: Consecutive ``task.failed`` transitions
            immediately before the final ``task.completed``.  Zero if the task
            succeeded on the first try or never succeeded.
        final_outcome: The terminal kind of the last attempt: ``task.completed``,
            ``task.failed``, ``task.abandoned``, or ``""`` when the task has no
            terminal transitions.
        is_masked: ``True`` when the task eventually completed but had at least
            one failure attempt before it -- the outcome is a success that hid
            prior failures.
    """

    task_id: str
    terminal_transition_count: int
    failure_count_before_success: int
    final_outcome: str
    is_masked: bool


def count_masked_failures(
    task_id: str, attempts: Sequence[AttemptLike]
) -> TaskAttemptMaskedFailureReport:
    """Return a :class:`TaskAttemptMaskedFailureReport` for *task_id* over *attempts*.

    Only records whose ``task_id`` equals *task_id* are considered; the rest
    are ignored so the caller may pass a mixed sequence and filter implicitly.

    The failure count is the number of consecutive ``task.failed`` transitions
    immediately before the last ``task.completed`` in the filtered sequence.
    Failures that appear *after* an earlier success (e.g. a task that was
    re-run) are not included in ``failure_count_before_success``.  A task
    that ends with ``task.abandoned`` is never considered masked: the abandon
    is itself a visible non-success outcome, not a hidden one.

    Args:
        task_id: Which task to report on.
        attempts: Ordered sequence of attempt records (oldest first).

    Returns:
        A :class:`TaskAttemptMaskedFailureReport` for the task.
    """
    task_attempts = [
        a for a in attempts if a.task_id == task_id and a.kind in _TERMINAL_KINDS
    ]

    if not task_attempts:
        return TaskAttemptMaskedFailureReport(
            task_id=task_id,
            terminal_transition_count=0,
            failure_count_before_success=0,
            final_outcome="",
            is_masked=False,
        )

    final = task_attempts[-1]
    terminal_transition_count = len(task_attempts)

    if final.kind != _SUCCESS_KIND:
        return TaskAttemptMaskedFailureReport(
            task_id=task_id,
            terminal_transition_count=terminal_transition_count,
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

    return TaskAttemptMaskedFailureReport(
        task_id=task_id,
        terminal_transition_count=terminal_transition_count,
        failure_count_before_success=failure_count,
        final_outcome=final.kind,
        is_masked=failure_count > 0,
    )


def scan_for_masked_failures(
    attempts: Sequence[AttemptLike]
) -> list[TaskAttemptMaskedFailureReport]:
    """Return one :class:`TaskAttemptMaskedFailureReport` per distinct task id in *attempts*.

    Tasks are returned in the order their first attempt record appears.  Only
    tasks that have at least one terminal-kind transition are included; in-flight
    tasks whose only records are non-terminal (e.g. ``task.started``) are omitted.

    Args:
        attempts: Ordered sequence of attempt records (oldest first).

    Returns:
        List of reports, one per task id that has any terminal transition.
    """
    bucketed: dict[str, list[AttemptLike]] = {}
    seen_order: list[str] = []
    for attempt in attempts:
        if attempt.task_id not in bucketed:
            bucketed[attempt.task_id] = []
            seen_order.append(attempt.task_id)
        bucketed[attempt.task_id].append(attempt)

    reports: list[TaskAttemptMaskedFailureReport] = []
    for tid in seen_order:
        task_attempts = bucketed[tid]
        # We only want to create a report if there is at least one terminal kind.
        # But count_masked_failures will return a report with is_masked=False and
        # failure_count_before_success=0 if there are no terminal kinds.
        # We can filter out those with terminal_transition_count==0.
        report = count_masked_failures(tid, task_attempts)
        if report.terminal_transition_count > 0:
            reports.append(report)
    return reports
