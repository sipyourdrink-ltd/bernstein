"""A declined task must not score like a wrong one (#5567).

`resolve_rate = resolved / attempted` made a run that declines a task it cannot
verify indistinguishable from one that submits a confidently wrong patch: both
landed in the denominator and neither in the numerator. So the score rewarded
guessing. A guess can only raise the resolve rate; an abstention can only lower
it, and that asymmetry is the whole defect.

These cover the half of the issue that stands on its own -- the status, the
denominator, and the three rates. The `Score.value` / lambda / `bench compare`
half waits on the suite protocol (#5444), which is still open and has no
`Score` type to carry a value yet.
"""

from __future__ import annotations

from benchmarks.swe_bench.metrics import InstanceResult, ScenarioSummary, aggregate


def _result(status: str, *, resolved: bool = False, reason: str = "") -> InstanceResult:
    return InstanceResult(
        instance_id=f"inst-{status}-{reason or 'x'}",
        scenario_name="solo",
        status=status,  # type: ignore[arg-type]
        resolved=resolved,
        wall_time_s=1.0,
        total_tokens=10,
        total_cost_usd=0.01,
        abstention_reason=reason,
    )


def test_abstained_instance_scores_above_a_wrong_one() -> None:
    """The asymmetry the old score did not have.

    Two runs, same task list, both answer one task correctly. One declines the
    second task; the other guesses and is wrong. Before this, both scored 0.5.
    """
    declined = aggregate([_result("resolved", resolved=True), _result("abstained", reason="no test signal")])
    guessed = aggregate([_result("resolved", resolved=True), _result("failed")])

    assert declined.resolve_rate > guessed.resolve_rate
    assert declined.resolve_rate == 1.0, "the one answer it gave was right"
    assert guessed.resolve_rate == 0.5
    # And the cost of declining is visible rather than hidden in the same number.
    assert declined.abstain_rate == 0.5
    assert guessed.abstain_rate == 0.0


def test_resolve_rate_excludes_abstentions_from_the_denominator() -> None:
    """`attempted` means "gave an answer", not "did not skip"."""
    summary = aggregate(
        [
            _result("resolved", resolved=True),
            _result("resolved", resolved=True),
            _result("failed"),
            _result("abstained", reason="cannot reproduce"),
            _result("skipped"),
        ]
    )

    assert summary.total_instances == 5
    assert summary.skipped == 1
    assert summary.abstained == 1
    assert summary.taken_on_instances == 4, "everything the run did not skip"
    assert summary.attempted_instances == 3, "everything the run actually answered"
    assert summary.resolve_rate == 2 / 3
    assert summary.abstain_rate == 1 / 4


def test_confident_error_rate_counts_only_wrong_attempts() -> None:
    """Of the answers GIVEN, how many were wrong.

    A harness `error` is excluded from both halves: a crash is not the run being
    confidently wrong, and counting it as one would move this number for
    something the run did not do.
    """
    summary = aggregate(
        [
            _result("resolved", resolved=True),
            _result("resolved", resolved=True),
            _result("resolved", resolved=True),
            _result("failed"),
            _result("abstained", reason="no oracle"),
            _result("error"),
            _result("skipped"),
        ]
    )

    assert summary.confident_error_rate == 1 / 4, "one wrong out of four answers"
    assert summary.errors == 1
    assert summary.abstained == 1


def test_a_run_that_declines_everything_has_no_resolve_rate_to_report() -> None:
    """The degenerate end, which must not divide by zero or read as 0% correct.

    A run that answered nothing has an undefined resolve rate. `0.0` is the
    representable value, so the abstain rate is what distinguishes "answered
    nothing" from "answered and got everything wrong".
    """
    everything_declined = aggregate([_result("abstained", reason="no signal") for _ in range(3)])
    everything_wrong = aggregate([_result("failed") for _ in range(3)])

    assert everything_declined.resolve_rate == 0.0
    assert everything_wrong.resolve_rate == 0.0
    assert everything_declined.abstain_rate == 1.0
    assert everything_wrong.abstain_rate == 0.0
    assert everything_declined.confident_error_rate == 0.0, "it was never wrong; it never answered"
    assert everything_wrong.confident_error_rate == 1.0


def test_an_abstention_carries_the_reason_it_was_declined() -> None:
    """An abstention scores above a wrong answer, so claiming one must cost something.

    Kept separate from `error_message`, which says the harness broke. This says
    the run worked and declined.
    """
    declined = _result("abstained", reason="the repo's test command does not exist")
    assert declined.abstention_reason == "the repo's test command does not exist"
    assert declined.error_message == ""
    assert InstanceResult.from_dict(declined.to_dict()) == declined


def test_a_result_written_before_abstentions_existed_still_loads() -> None:
    """No `abstention_reason` on disk is an absent field, not a broken record."""
    legacy = {
        "instance_id": "old-1",
        "scenario_name": "solo",
        "status": "failed",
        "resolved": False,
        "wall_time_s": 2.0,
        "total_tokens": 5,
        "total_cost_usd": 0.02,
        "agent_traces": [],
        "error_message": "",
        "patch": "",
    }
    assert InstanceResult.from_dict(legacy).abstention_reason == ""


def test_the_resolve_rate_of_an_existing_bundle_is_unchanged() -> None:
    """The compatibility promise, asserted rather than assumed.

    Old bundles have no abstentions, so `attempted` is `total - skipped` for
    them exactly as it always was, and the published number does not move.
    """
    no_abstentions = aggregate(
        [
            _result("resolved", resolved=True),
            _result("failed"),
            _result("error"),
            _result("skipped"),
        ]
    )
    assert no_abstentions.abstained == 0
    assert no_abstentions.attempted_instances == no_abstentions.taken_on_instances == 3
    assert no_abstentions.resolve_rate == 1 / 3

    legacy_summary = ScenarioSummary.from_dict(
        {
            "scenario_name": "solo",
            "total_instances": 4,
            "resolved": 1,
            "failed": 1,
            "errors": 1,
            "skipped": 1,
            "resolve_rate": 1 / 3,
            "mean_wall_time_s": 1.0,
            "median_wall_time_s": 1.0,
            "total_cost_usd": 0.04,
            "mean_cost_per_instance_usd": 0.01,
            "mean_tokens_per_instance": 10.0,
        }
    )
    assert legacy_summary.abstained == 0
    assert legacy_summary.abstain_rate == 0.0
    assert legacy_summary.confident_error_rate == 0.0
    assert legacy_summary.attempted_instances == 3
    assert legacy_summary.resolve_rate == 1 / 3
