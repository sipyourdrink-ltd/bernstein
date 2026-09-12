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

from bernstein.core.persistence.runs_report import (
    FIRST_RETRY_ATTEMPT,
    FinishedRun,
    RunOutcome,
    masked_failures,
)


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
