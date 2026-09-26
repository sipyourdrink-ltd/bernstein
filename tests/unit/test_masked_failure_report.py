"""Issue #5106: a retry that succeeded is invisible in every final-state report.

`flaky_detector.py` closed exactly this blind spot for TESTS by keeping
per-execution history. At the run level a run that failed twice and closed on
the third attempt reports identically to one that closed on its first --
``outcome=pr-opened``, and nothing anywhere says it took three goes.

``FinishedRun.attempt_count`` already carried the number, from the run's own
``task.started`` transitions. Nothing read it as a signal.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.persistence.runs_report import (
    FIRST_RETRY_ATTEMPT,
    FinishedRun,
    RunOutcome,
    masked_failures,
    task_retry_sequences,
)
from bernstein.core.persistence.work_ledger import (
    KIND_TASK_COMPLETED,
    KIND_TASK_FAILED,
    KIND_TASK_SCHEDULED,
    KIND_TASK_STARTED,
    WorkLedger,
    run_ledger_dir,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.persistence.work_ledger import LedgerEntry


def _run(
    run_id: str,
    outcome: RunOutcome = RunOutcome.PR_OPENED,
    *,
    attempts: int = 1,
    host: str = "builder-1",
    started_at: float = 1000.0,
) -> FinishedRun:
    return FinishedRun(
        run_id,
        f"fix/{run_id}",
        outcome,
        "evidence",
        started_at,
        attempt_count=attempts,
        host=host,
    )


# ---------------------------------------------------------------------------
# What counts as masked
# ---------------------------------------------------------------------------


def test_a_run_that_succeeded_first_time_is_not_masked() -> None:
    report = masked_failures([_run("r1", attempts=1)])
    assert report.masked == 0
    assert report.finished == 1


def test_a_success_that_needed_a_retry_is_masked() -> None:
    report = masked_failures([_run("r1", attempts=3)])

    (row,) = report.rows
    assert row.run_id == "r1"
    assert row.attempt_count == 3
    assert row.retries == 2, "an operator asks for retries, not attempts"
    # The run's own classification is untouched: this report adds to it.
    assert row.outcome is RunOutcome.PR_OPENED


def test_the_second_attempt_is_the_first_retry() -> None:
    """One `task.started` is the run happening; the second is the retry."""
    assert FIRST_RETRY_ATTEMPT == 2
    assert masked_failures([_run("r1", attempts=1)]).masked == 0
    assert masked_failures([_run("r1", attempts=2)]).masked == 1


def test_a_run_that_retried_and_still_failed_is_not_masked() -> None:
    """It is already visible in every report -- which is the point.

    A retry only MASKS something when the ending it reached was a success.
    """
    report = masked_failures([_run("r1", RunOutcome.GATE_FAILED, attempts=4)])
    assert report.masked == 0
    assert report.finished == 1


def test_a_no_changes_run_counts_as_a_success() -> None:
    """It got where it was going; the retries are still hidden by the outcome."""
    assert masked_failures([_run("r1", RunOutcome.NO_CHANGES, attempts=2)]).masked == 1


def test_infra_error_and_wedged_are_not_successes() -> None:
    for outcome in (RunOutcome.INFRA_ERROR, RunOutcome.WEDGED):
        assert masked_failures([_run("r1", outcome, attempts=5)]).masked == 0


# ---------------------------------------------------------------------------
# The share, and the gate that reads it
# ---------------------------------------------------------------------------


def test_the_share_has_every_finished_run_as_its_denominator() -> None:
    """A count of 3 says nothing without the number it is out of."""
    report = masked_failures(
        [
            _run("r1", attempts=3),
            _run("r2", attempts=1),
            _run("r3", RunOutcome.GATE_FAILED, attempts=2),
            _run("r4", attempts=1),
        ]
    )
    assert report.masked == 1
    assert report.finished == 4
    assert report.share == 0.25


def test_an_empty_window_is_a_zero_share_not_a_division() -> None:
    """No data is not a clean bill of health, and it is not a failure either."""
    report = masked_failures([])
    assert report.share == 0.0
    assert report.exceeds(0.0) is False


def test_the_gate_admits_exactly_the_threshold() -> None:
    """Strictly greater, so a limit of 0.5 permits 50% rather than 50% minus one run."""
    report = masked_failures([_run("r1", attempts=2), _run("r2", attempts=1)])
    assert report.share == 0.5
    assert report.exceeds(0.5) is False
    assert report.exceeds(0.49) is True


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def test_rows_are_grouped_by_the_attribution_runs_actually_carry() -> None:
    report = masked_failures(
        [
            _run("r1", attempts=3, host="builder-1"),
            _run("r2", attempts=1, host="builder-1"),
            _run("r3", attempts=2, host="builder-2"),
        ]
    )
    assert report.by_owner["builder-1"] == (1, 2)
    assert report.by_owner["builder-2"] == (1, 1)


def test_a_run_with_no_host_is_named_not_dropped() -> None:
    """Silently omitting it would understate the very number being reported."""
    report = masked_failures([_run("r1", attempts=2, host="")])
    assert report.rows[0].owner == "unattributed"
    assert report.by_owner["unattributed"] == (1, 1)


# ---------------------------------------------------------------------------
# Determinism and the JSON contract
# ---------------------------------------------------------------------------


def test_the_report_is_byte_identical_for_the_same_input() -> None:
    """The ordering `list_finished_runs` established is inherited unchanged."""
    runs = [
        _run("r3", attempts=2, started_at=1020.0),
        _run("r1", attempts=4, started_at=1000.0),
        _run("r2", attempts=1, started_at=1010.0),
    ]
    first = json.dumps(masked_failures(runs).to_dict(), sort_keys=True)
    second = json.dumps(masked_failures(runs).to_dict(), sort_keys=True)
    assert first == second
    assert [row.run_id for row in masked_failures(runs).rows] == ["r3", "r1"]


def test_the_json_shape_mirrors_a_finished_run_row() -> None:
    document = masked_failures([_run("r1", attempts=3)]).to_dict()

    assert set(document) == {"finished", "masked", "share", "by_owner", "rows"}
    assert set(document["rows"][0]) == {
        "run_id",
        "branch",
        "outcome",
        "attempt_count",
        "retries",
        "owner",
        "started_at",
    }
    # Enums as their string values, the same contract FinishedRun.to_dict has.
    assert document["rows"][0]["outcome"] == "pr-opened"
    assert document["by_owner"]["builder-1"] == {"masked": 1, "finished": 1, "share": 1.0}
    json.dumps(document)  # serialises without a custom encoder


# ---------------------------------------------------------------------------
# Task-level retry sequences (#5106 slice 1)
# ---------------------------------------------------------------------------
#
# `masked_failures` above reports at run granularity, from the *sum* of every
# task's attempts in the run -- it cannot say which task actually failed and
# recovered. These tests exercise `task_retry_sequences`, which reads the raw
# ledger entries directly and reports per `task_id`, the natural key for "the
# same logical task retried" within one ledger root.


def _ledger(tmp_path: Path, run_id: str) -> Path:
    return run_ledger_dir(tmp_path / ".sdd", run_id)


def _append_task(tmp_path: Path, run_id: str, task_id: str, kinds: list[str]) -> list[LedgerEntry]:
    """Append one task's kind sequence to *run_id*'s ledger, opening/closing per call.

    Reopening per call (rather than holding one writer across a whole test)
    keeps each test's ledger construction linear and easy to read, and
    mirrors how independent processes would actually append over a run's
    lifetime. Returns the persisted entries, in append order, so a test can
    assert against a specific entry's own ``ts`` rather than reconstructing
    it from the ledger.
    """
    ledger = WorkLedger.open(_ledger(tmp_path, run_id))
    entries = [ledger.append(kind=kind, task_id=task_id) for kind in kinds]
    ledger.close()
    return entries


def test_failed_then_completed_is_reported(tmp_path: Path) -> None:
    """The load-bearing case: a task that failed once and then succeeded."""
    entries = _append_task(
        tmp_path,
        "run-a",
        "t1",
        [KIND_TASK_SCHEDULED, KIND_TASK_STARTED, KIND_TASK_FAILED, KIND_TASK_STARTED, KIND_TASK_COMPLETED],
    )

    sequences = task_retry_sequences(_ledger(tmp_path, "run-a"), run_id="run-a")

    (row,) = sequences
    assert row.run_id == "run-a"
    assert row.task_id == "t1"
    assert row.failed_attempts == 1
    # `succeeded` is always True for a row this function returns (see the
    # attribute's own docstring) -- pinned here for the JSON shape, not
    # because the value could ever come out False.
    assert row.succeeded is True
    assert row.started_at == entries[1].ts


def test_started_at_is_the_first_task_started_entry_not_the_scheduled_one(tmp_path: Path) -> None:
    """``started_at`` names ``task.started`` explicitly in its docstring.

    ``task.scheduled`` is ordered before ``task.started`` in a normal
    lifecycle, so picking the group's first entry of any kind silently
    reports the scheduled instant instead -- this pins the field to the
    entry its own docstring promises.
    """
    entries = _append_task(
        tmp_path,
        "run-a",
        "t1",
        [KIND_TASK_SCHEDULED, KIND_TASK_STARTED, KIND_TASK_FAILED, KIND_TASK_STARTED, KIND_TASK_COMPLETED],
    )
    scheduled_entry, started_entry = entries[0], entries[1]

    (row,) = task_retry_sequences(_ledger(tmp_path, "run-a"), run_id="run-a")

    assert row.started_at == started_entry.ts
    assert row.started_at != scheduled_entry.ts


def test_started_at_is_none_when_no_task_started_entry_exists(tmp_path: Path) -> None:
    """A ledger missing ``task.started`` gets ``None``, not a substitute instant (#6179 review).

    An earlier version fell back to the task id's first entry of any kind
    (``task.scheduled`` here), which silently asserted a fact -- "this is
    when the task started running" -- the ledger never actually recorded.
    A row still needs to be producible for this anomalous-but-otherwise-
    valid task id (it has a real failed-then-completed history), so this
    is ``None``, not a raised exception that would take down the whole
    report over one task.
    """
    _append_task(tmp_path, "run-a", "t1", [KIND_TASK_SCHEDULED, KIND_TASK_FAILED, KIND_TASK_COMPLETED])

    (row,) = task_retry_sequences(_ledger(tmp_path, "run-a"), run_id="run-a")

    assert row.started_at is None


def test_succeeded_first_try_is_excluded(tmp_path: Path) -> None:
    _append_task(tmp_path, "run-a", "t1", [KIND_TASK_SCHEDULED, KIND_TASK_STARTED, KIND_TASK_COMPLETED])

    assert task_retry_sequences(_ledger(tmp_path, "run-a"), run_id="run-a") == []


def test_failed_only_is_excluded(tmp_path: Path) -> None:
    """It is already visible as a failed task -- there is no success to mask it."""
    _append_task(tmp_path, "run-a", "t1", [KIND_TASK_SCHEDULED, KIND_TASK_STARTED, KIND_TASK_FAILED])

    assert task_retry_sequences(_ledger(tmp_path, "run-a"), run_id="run-a") == []


def test_three_failed_attempts_before_success(tmp_path: Path) -> None:
    """Also proves the count is a total, not a run of adjacent failures.

    Each ``task.failed`` here is separated from the next by a
    ``task.started``, and the last one is separated from the completion the
    same way -- so this is the case that would read as 0 or 1 under a
    strictly-adjacent "consecutive, immediately before completion" rule.
    """
    _append_task(
        tmp_path,
        "run-a",
        "t1",
        [
            KIND_TASK_SCHEDULED,
            KIND_TASK_STARTED,
            KIND_TASK_FAILED,
            KIND_TASK_STARTED,
            KIND_TASK_FAILED,
            KIND_TASK_STARTED,
            KIND_TASK_FAILED,
            KIND_TASK_STARTED,
            KIND_TASK_COMPLETED,
        ],
    )

    (row,) = task_retry_sequences(_ledger(tmp_path, "run-a"), run_id="run-a")
    assert row.failed_attempts == 3


def test_output_is_deterministic_over_the_same_ledger(tmp_path: Path) -> None:
    _append_task(tmp_path, "run-a", "t1", [KIND_TASK_STARTED, KIND_TASK_FAILED, KIND_TASK_STARTED, KIND_TASK_COMPLETED])
    _append_task(tmp_path, "run-a", "t2", [KIND_TASK_STARTED, KIND_TASK_COMPLETED])

    ledger_dir = _ledger(tmp_path, "run-a")
    first = [row.to_dict() for row in task_retry_sequences(ledger_dir, run_id="run-a")]
    second = [row.to_dict() for row in task_retry_sequences(ledger_dir, run_id="run-a")]
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_only_the_retried_task_is_reported_alongside_a_clean_one(tmp_path: Path) -> None:
    """Two task ids in one ledger root -- grouping is per task id, not per run."""
    _append_task(tmp_path, "run-a", "t1", [KIND_TASK_STARTED, KIND_TASK_FAILED, KIND_TASK_STARTED, KIND_TASK_COMPLETED])
    _append_task(tmp_path, "run-a", "t2", [KIND_TASK_STARTED, KIND_TASK_COMPLETED])

    sequences = task_retry_sequences(_ledger(tmp_path, "run-a"), run_id="run-a")

    assert [row.task_id for row in sequences] == ["t1"]


def test_the_same_task_id_in_different_ledger_roots_is_never_merged(tmp_path: Path) -> None:
    """`task_id` is only unique within one ledger root -- two runs reusing
    the same task id string are two independent sequences, not one merged
    history."""
    _append_task(tmp_path, "run-a", "t1", [KIND_TASK_STARTED, KIND_TASK_FAILED, KIND_TASK_STARTED, KIND_TASK_COMPLETED])
    _append_task(tmp_path, "run-b", "t1", [KIND_TASK_STARTED, KIND_TASK_COMPLETED])

    run_a = task_retry_sequences(_ledger(tmp_path, "run-a"), run_id="run-a")
    run_b = task_retry_sequences(_ledger(tmp_path, "run-b"), run_id="run-b")

    assert [row.task_id for row in run_a] == ["t1"]
    assert run_a[0].failed_attempts == 1
    assert run_b == []
