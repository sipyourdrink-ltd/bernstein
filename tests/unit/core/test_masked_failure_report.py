"""Tests for the masked failure report projection (#5106)."""

from __future__ import annotations

from bernstein.core.persistence.masked_failure_report import (
    _NON_TERMINAL_KINDS,
    _TERMINAL_KINDS,
    AttemptRecord,
    count_masked_failures,
    scan_for_masked_failures,
    TaskAttemptMaskedFailureReport,
)
from bernstein.core.persistence.work_ledger import (
    KIND_TASK_ABANDONED,
    KIND_TASK_COMPLETED,
    KIND_TASK_FAILED,
    TASK_KINDS,
)

# ---------------------------------------------------------------------------
# Kind-set partition assertion
# ---------------------------------------------------------------------------


def test_terminal_and_non_terminal_partition_task_kinds() -> None:
    """_TERMINAL_KINDS and _NON_TERMINAL_KINDS together equal TASK_KINDS exactly."""
    assert _TERMINAL_KINDS | _NON_TERMINAL_KINDS == TASK_KINDS
    assert frozenset() == _TERMINAL_KINDS & _NON_TERMINAL_KINDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attempt(task_id: str, kind: str) -> AttemptRecord:
    return AttemptRecord(task_id=task_id, kind=kind)


# ---------------------------------------------------------------------------
# count_masked_failures -- single task
# ---------------------------------------------------------------------------


class TestCountMaskedFailures:
    """count_masked_failures over a single task's attempt sequence."""

    def test_no_attempts_returns_zero_report(self) -> None:
        report = count_masked_failures("t1", [])
        assert report.task_id == "t1"
        assert report.terminal_transition_count == 0
        assert report.failure_count_before_success == 0
        assert report.final_outcome == ""
        assert not report.is_masked

    def test_single_success_is_not_masked(self) -> None:
        attempts = [_attempt("t1", KIND_TASK_COMPLETED)]
        report = count_masked_failures("t1", attempts)
        assert report.terminal_transition_count == 1
        assert report.failure_count_before_success == 0
        assert report.final_outcome == KIND_TASK_COMPLETED
        assert not report.is_masked

    def test_single_failure_is_not_masked(self) -> None:
        attempts = [_attempt("t1", KIND_TASK_FAILED)]
        report = count_masked_failures("t1", attempts)
        assert report.terminal_transition_count == 1
        assert report.failure_count_before_success == 0
        assert report.final_outcome == KIND_TASK_FAILED
        assert not report.is_masked

    def test_one_failure_then_success_is_masked(self) -> None:
        attempts = [
            _attempt("t1", KIND_TASK_FAILED),
            _attempt("t1", KIND_TASK_COMPLETED),
        ]
        report = count_masked_failures("t1", attempts)
        assert report.failure_count_before_success == 1
        assert report.final_outcome == KIND_TASK_COMPLETED
        assert report.is_masked

    def test_two_failures_then_success_counts_both(self) -> None:
        attempts = [
            _attempt("t1", KIND_TASK_FAILED),
            _attempt("t1", KIND_TASK_FAILED),
            _attempt("t1", KIND_TASK_COMPLETED),
        ]
        report = count_masked_failures("t1", attempts)
        assert report.failure_count_before_success == 2
        assert report.terminal_transition_count == 3
        assert report.is_masked

    def test_abandoned_terminal_is_not_masked(self) -> None:
        attempts = [
            _attempt("t1", KIND_TASK_FAILED),
            _attempt("t1", KIND_TASK_ABANDONED),
        ]
        report = count_masked_failures("t1", attempts)
        assert report.final_outcome == KIND_TASK_ABANDONED
        assert report.failure_count_before_success == 0
        assert not report.is_masked

    def test_only_task_id_records_are_considered(self) -> None:
        attempts = [
            _attempt("other", KIND_TASK_FAILED),
            _attempt("other", KIND_TASK_FAILED),
            _attempt("t1", KIND_TASK_COMPLETED),
        ]
        report = count_masked_failures("t1", attempts)
        assert report.terminal_transition_count == 1
        assert report.failure_count_before_success == 0
        assert not report.is_masked

    def test_failure_count_only_counts_consecutive_failures_before_last_success(self) -> None:
        # success-failure-failure-success: only the last 2 failures count
        attempts = [
            _attempt("t1", KIND_TASK_COMPLETED),
            _attempt("t1", KIND_TASK_FAILED),
            _attempt("t1", KIND_TASK_FAILED),
            _attempt("t1", KIND_TASK_COMPLETED),
        ]
        report = count_masked_failures("t1", attempts)
        assert report.failure_count_before_success == 2
        assert report.is_masked

    def test_non_consecutive_failure_breaks_count(self) -> None:
        # failure-success-failure-success: the non-consecutive failure is not counted
        attempts = [
            _attempt("t1", KIND_TASK_FAILED),
            _attempt("t1", KIND_TASK_COMPLETED),
            _attempt("t1", KIND_TASK_FAILED),
            _attempt("t1", KIND_TASK_COMPLETED),
        ]
        report = count_masked_failures("t1", attempts)
        assert report.failure_count_before_success == 1
        assert report.is_masked


# ---------------------------------------------------------------------------
# scan_for_masked_failures -- multiple tasks
# ---------------------------------------------------------------------------


class TestScanForMaskedFailures:
    """scan_for_masked_failures over a mixed-task sequence."""

    def test_empty_sequence_returns_empty(self) -> None:
        assert scan_for_masked_failures([]) == []

    def test_single_task_success_returned(self) -> None:
        attempts = [_attempt("t1", KIND_TASK_COMPLETED)]
        reports = scan_for_masked_failures(attempts)
        assert len(reports) == 1
        assert reports[0].task_id == "t1"
        assert isinstance(reports[0], TaskAttemptMaskedFailureReport)

    def test_two_tasks_returned_in_first_attempt_order(self) -> None:
        attempts = [
            _attempt("t2", KIND_TASK_FAILED),
            _attempt("t1", KIND_TASK_COMPLETED),
            _attempt("t2", KIND_TASK_COMPLETED),
        ]
        reports = scan_for_masked_failures(attempts)
        assert [r.task_id for r in reports] == ["t2", "t1"]
        assert all(isinstance(r, TaskAttemptMaskedFailureReport) for r in reports)

    def test_masked_task_identified_among_others(self) -> None:
        attempts = [
            _attempt("clean", KIND_TASK_COMPLETED),
            _attempt("flaky", KIND_TASK_FAILED),
            _attempt("flaky", KIND_TASK_COMPLETED),
        ]
        reports = scan_for_masked_failures(attempts)
        by_id = {r.task_id: r for r in reports}
        assert not by_id["clean"].is_masked
        assert by_id["flaky"].is_masked
        assert by_id["flaky"].failure_count_before_success == 1

    def test_task_with_no_terminal_kind_excluded(self) -> None:
        # task.started is not a terminal kind and should not appear in output
        attempts = [
            AttemptRecord(task_id="pending", kind="task.started"),
            _attempt("done", KIND_TASK_COMPLETED),
        ]
        reports = scan_for_masked_failures(attempts)
        ids = [r.task_id for r in reports]
        assert "pending" not in ids
        assert "done" in ids

    def test_all_tasks_abandoned_reported(self) -> None:
        attempts = [
            _attempt("a", KIND_TASK_ABANDONED),
            _attempt("b", KIND_TASK_ABANDONED),
        ]
        reports = scan_for_masked_failures(attempts)
        assert len(reports) == 2
        assert all(r.final_outcome == KIND_TASK_ABANDONED for r in reports)
        assert all(not r.is_masked for r in reports)

# ---------------------------------------------------------------------------
# LedgerEntry compatibility
# ---------------------------------------------------------------------------


class TestLedgerEntryCompatibility:
    """Ensure the projection works with LedgerEntry from the work ledger."""

    def test_scan_for_masked_failures_with_ledger_entry(self) -> None:
        from bernstein.core.persistence.work_ledger import LedgerEntry

        # Create a few LedgerEntry objects for the same task.
        entry1 = LedgerEntry(
            seq=0,
            prev_hash="0" * 64,
            kind=KIND_TASK_FAILED,
            task_id="task1",
            payload={},
            entry_hash="1" * 64,
            ts=1000.0,
            redactions=0,
            schema_version=1,
        )
        entry2 = LedgerEntry(
            seq=1,
            prev_hash=entry1.entry_hash,
            kind=KIND_TASK_FAILED,
            task_id="task1",
            payload={},
            entry_hash="2" * 64,
            ts=1001.0,
            redactions=0,
            schema_version=1,
        )
        entry3 = LedgerEntry(
            seq=2,
            prev_hash=entry2.entry_hash,
            kind=KIND_TASK_COMPLETED,
            task_id="task1",
            payload={},
            entry_hash="3" * 64,
            ts=1002.0,
            redactions=0,
            schema_version=1,
        )
        # Another task that succeeds on first try.
        entry4 = LedgerEntry(
            seq=3,
            prev_hash=entry3.entry_hash,
            kind=KIND_TASK_COMPLETED,
            task_id="task2",
            payload={},
            entry_hash="4" * 64,
            ts=1003.0,
            redactions=0,
            schema_version=1,
        )

        attempts = [entry1, entry2, entry3, entry4]
        reports = scan_for_masked_failures(attempts)

        # We expect two reports: one for task1 (masked) and one for task2 (not masked).
        assert len(reports) == 2
        by_id = {r.task_id: r for r in reports}
        assert by_id["task1"].is_masked
        assert by_id["task1"].failure_count_before_success == 2
        assert not by_id["task2"].is_masked
        assert by_id["task2"].failure_count_before_success == 0
