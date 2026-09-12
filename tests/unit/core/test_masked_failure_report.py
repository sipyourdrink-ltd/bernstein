"""Tests for the masked failure report projection (#5106)."""

from __future__ import annotations

from bernstein.core.persistence.masked_failure_report import (
    AttemptRecord,
    count_masked_failures,
    scan_for_masked_failures,
)
from bernstein.core.persistence.work_ledger import (
    KIND_TASK_ABANDONED,
    KIND_TASK_COMPLETED,
    KIND_TASK_FAILED,
)

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
        assert report.attempt_count == 0
        assert report.failure_count_before_success == 0
        assert report.final_outcome == ""
        assert not report.is_masked

    def test_single_success_is_not_masked(self) -> None:
        attempts = [_attempt("t1", KIND_TASK_COMPLETED)]
        report = count_masked_failures("t1", attempts)
        assert report.attempt_count == 1
        assert report.failure_count_before_success == 0
        assert report.final_outcome == KIND_TASK_COMPLETED
        assert not report.is_masked

    def test_single_failure_is_not_masked(self) -> None:
        attempts = [_attempt("t1", KIND_TASK_FAILED)]
        report = count_masked_failures("t1", attempts)
        assert report.attempt_count == 1
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
        assert report.attempt_count == 3
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
        assert report.attempt_count == 1
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

    def test_two_tasks_returned_in_first_attempt_order(self) -> None:
        attempts = [
            _attempt("t2", KIND_TASK_FAILED),
            _attempt("t1", KIND_TASK_COMPLETED),
            _attempt("t2", KIND_TASK_COMPLETED),
        ]
        reports = scan_for_masked_failures(attempts)
        assert [r.task_id for r in reports] == ["t2", "t1"]

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
