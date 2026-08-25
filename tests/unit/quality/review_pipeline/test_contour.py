"""Fix-until-green contour tests (issue #4481).

The contour is the loop an operator used to write in shell around
``bernstein review --pipeline``: settle the checks, review, feed the verdict
and the failing checks' logs to a fix pass, push, re-check.  These tests pin
its stop condition, its budget, and what reaches the fix prompt.

* AC1 -- the loop runs to ``--max-passes`` and exits non-zero with a
  ``needs-operator`` outcome rather than an approval.
* AC2 -- the fix prompt carries the failing checks' log excerpts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.core.quality.review_pipeline.contour import (
    CheckLogExcerpt,
    CheckRollup,
    CheckRun,
    FixOutcome,
    FixRequest,
    rollup_from_payload,
    run_review_contour,
    wait_for_checks,
)
from bernstein.core.quality.review_pipeline.ruleset import parse_ruleset
from bernstein.core.quality.review_pipeline.runner import DiffSource
from bernstein.core.quality.review_pipeline.schema import (
    AgentSpec,
    ReviewPipeline,
    StageSpec,
)
from bernstein.core.quality.review_pipeline.verdict import (
    AgentVerdict,
    PipelineVerdict,
    StageVerdict,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_PIPELINE = ReviewPipeline(stages=[StageSpec(name="s", agents=[AgentSpec(role="r")])])

_GREEN = CheckRollup(state="green", checks=(CheckRun(name="ci", status="COMPLETED", conclusion="SUCCESS"),))
_RED = CheckRollup(
    state="red",
    checks=(CheckRun(name="ci", status="COMPLETED", conclusion="FAILURE", run_id="991"),),
)


def _verdict(kind: str, *, issues: Sequence[str] = ()) -> PipelineVerdict:
    agent = AgentVerdict(role="r", model="m", verdict=kind, feedback="f", issues=list(issues))  # type: ignore[arg-type]
    stage = StageVerdict(
        stage="s",
        verdict=kind,  # type: ignore[arg-type]
        approve_count=1 if kind == "approve" else 0,
        total_count=1,
        pass_score=1.0 if kind == "approve" else 0.0,
        agents=[agent],
    )
    return PipelineVerdict(
        verdict=kind,  # type: ignore[arg-type]
        feedback="pipeline feedback",
        pass_score=stage.pass_score,
        stages=[stage],
    )


def _diff(text: str = "+ one line") -> DiffSource:
    return DiffSource(title="t", description="d", diff=text, pr_number=7)


def _scripted(values: Sequence[object]) -> tuple[object, list[int]]:
    """Return a zero-arg callable replaying ``values``, plus a call counter."""
    calls: list[int] = []

    def _next(*_args: object, **_kwargs: object) -> object:
        index = min(len(calls), len(values) - 1)
        calls.append(index)
        return values[index]

    return _next, calls


# ---------------------------------------------------------------------------
# Check rollup
# ---------------------------------------------------------------------------


def test_failing_conclusions_make_the_rollup_red() -> None:
    rollup = rollup_from_payload(
        {
            "statusCheckRollup": [
                {"name": "unit", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {
                    "name": "lint",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                    "detailsUrl": "https://x/runs/42/job/9",
                },
            ]
        }
    )

    assert rollup.state == "red"
    assert [c.name for c in rollup.failing] == ["lint"]
    assert rollup.failing[0].run_id == "42"


def test_queued_checks_make_the_rollup_pending() -> None:
    rollup = rollup_from_payload({"statusCheckRollup": [{"name": "unit", "status": "QUEUED", "conclusion": ""}]})

    assert rollup.state == "pending"


def test_rollup_wait_stops_polling_once_the_checks_settle() -> None:
    pending = CheckRollup(state="pending", checks=())
    read, calls = _scripted([pending, pending, _GREEN])
    slept: list[float] = []

    result = wait_for_checks(
        read,  # type: ignore[arg-type]
        timeout_s=100.0,
        poll_interval_s=5.0,
        sleep=slept.append,
        monotonic=lambda: 0.0,
    )

    assert result.state == "green"
    assert len(calls) == 3
    assert slept == [5.0, 5.0]


def test_rollup_wait_is_bounded_and_reports_pending_on_timeout() -> None:
    pending = CheckRollup(state="pending", checks=())
    read, calls = _scripted([pending])
    clock = iter([0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0])

    result = wait_for_checks(
        read,  # type: ignore[arg-type]
        timeout_s=10.0,
        poll_interval_s=5.0,
        sleep=lambda _s: None,
        monotonic=lambda: next(clock),
    )

    assert result.state == "pending"
    assert len(calls) <= 3


# ---------------------------------------------------------------------------
# AC1 -- the loop, its stop condition and its budget
# ---------------------------------------------------------------------------


def test_approved_and_green_stops_after_a_single_pass() -> None:
    review, calls = _scripted([_verdict("approve")])

    result = run_review_contour(
        _PIPELINE,
        fetch_diff=_diff,
        read_rollup=lambda: _GREEN,
        review=review,  # type: ignore[arg-type]
        fix_runner=lambda _r: FixOutcome(pushed=True),
        max_passes=3,
    )

    assert result.outcome == "approved"
    assert result.exit_code == 0
    assert len(result.passes) == 1
    assert len(calls) == 1


def test_red_checks_withhold_approval_when_until_checks_green() -> None:
    review, _ = _scripted([_verdict("approve")])

    result = run_review_contour(
        _PIPELINE,
        fetch_diff=_diff,
        read_rollup=lambda: _RED,
        review=review,  # type: ignore[arg-type]
        max_passes=1,
        until_checks_green=True,
    )

    assert result.outcome == "needs-operator"
    assert result.passes[-1].checks_state == "red"


def test_red_checks_do_not_withhold_approval_without_until_checks_green() -> None:
    review, _ = _scripted([_verdict("approve")])

    result = run_review_contour(
        _PIPELINE,
        fetch_diff=_diff,
        read_rollup=lambda: _RED,
        review=review,  # type: ignore[arg-type]
        max_passes=1,
        until_checks_green=False,
    )

    assert result.outcome == "approved"


def test_fix_pass_runs_between_review_passes_until_checks_go_green() -> None:
    review, review_calls = _scripted([_verdict("request_changes", issues=["boom"]), _verdict("approve")])
    read_rollup, _ = _scripted([_RED, _GREEN])
    seen: list[FixRequest] = []

    def _fix(request: FixRequest) -> FixOutcome:
        seen.append(request)
        return FixOutcome(pushed=True, summary="pushed abc123")

    result = run_review_contour(
        _PIPELINE,
        fetch_diff=_diff,
        read_rollup=read_rollup,  # type: ignore[arg-type]
        review=review,  # type: ignore[arg-type]
        fix_runner=_fix,
        max_passes=3,
    )

    assert result.outcome == "approved"
    assert len(review_calls) == 2
    assert [r.pass_index for r in seen] == [1]
    assert result.passes[0].fix_pushed is True


def test_budget_exhausted_exits_needs_operator_nonzero() -> None:
    review, review_calls = _scripted([_verdict("request_changes", issues=["still broken"])])
    fixes: list[FixRequest] = []

    result = run_review_contour(
        _PIPELINE,
        fetch_diff=_diff,
        read_rollup=lambda: _RED,
        review=review,  # type: ignore[arg-type]
        fix_runner=lambda r: (fixes.append(r), FixOutcome(pushed=True))[1],
        max_passes=3,
    )

    assert result.outcome == "needs-operator"
    assert result.exit_code != 0
    assert len(result.passes) == 3
    assert len(review_calls) == 3
    # The budget buys three reviews and the two fixes that sit between them -
    # no fix runs after the last review, that is what "exhausted" means.
    assert [f.pass_index for f in fixes] == [1, 2]
    assert "max_passes" in result.reason


def test_contour_without_a_fix_runner_stops_needs_operator_never_approved() -> None:
    review, review_calls = _scripted([_verdict("request_changes")])

    result = run_review_contour(
        _PIPELINE,
        fetch_diff=_diff,
        read_rollup=lambda: _RED,
        review=review,  # type: ignore[arg-type]
        max_passes=3,
    )

    assert result.outcome == "needs-operator"
    assert result.exit_code != 0
    assert len(review_calls) == 1
    assert "no fix runner" in result.reason


def test_fix_runner_that_did_not_push_stops_the_loop() -> None:
    review, review_calls = _scripted([_verdict("request_changes")])

    result = run_review_contour(
        _PIPELINE,
        fetch_diff=_diff,
        read_rollup=lambda: _RED,
        review=review,  # type: ignore[arg-type]
        fix_runner=lambda _r: FixOutcome(pushed=False, summary="adapter exited 1"),
        max_passes=3,
    )

    assert result.outcome == "needs-operator"
    assert len(review_calls) == 1
    assert "adapter exited 1" in result.reason


def test_max_passes_below_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_passes"):
        run_review_contour(
            _PIPELINE,
            fetch_diff=_diff,
            read_rollup=lambda: _GREEN,
            review=lambda _d: _verdict("approve"),
            max_passes=0,
        )


# ---------------------------------------------------------------------------
# AC2 -- what reaches the fix prompt
# ---------------------------------------------------------------------------


def test_fix_prompt_contains_the_failing_check_log_excerpts() -> None:
    seen: list[str] = []

    def _fix(request: FixRequest) -> FixOutcome:
        seen.append(request.to_prompt())
        return FixOutcome(pushed=False)

    run_review_contour(
        _PIPELINE,
        fetch_diff=_diff,
        read_rollup=lambda: _RED,
        review=lambda _d: _verdict("request_changes"),
        fetch_logs=lambda check: CheckLogExcerpt(check=check.name, body="E   assert 1 == 2\nFAILED tests/test_x.py"),
        fix_runner=_fix,
        max_passes=2,
    )

    assert seen, "the fix pass never ran"
    assert "E   assert 1 == 2" in seen[0]
    assert "FAILED tests/test_x.py" in seen[0]
    assert "ci" in seen[0]


def test_fix_prompt_contains_the_review_verdict_issues() -> None:
    seen: list[str] = []

    def _fix(request: FixRequest) -> FixOutcome:
        seen.append(request.to_prompt())
        return FixOutcome(pushed=False)

    run_review_contour(
        _PIPELINE,
        fetch_diff=_diff,
        read_rollup=lambda: _RED,
        review=lambda _d: _verdict("request_changes", issues=["drop the bare except"]),
        fix_runner=_fix,
        max_passes=2,
    )

    assert "drop the bare except" in seen[0]


def test_fix_prompt_lists_guard_rules_so_rejected_findings_are_not_refixed() -> None:
    seen: list[str] = []

    def _fix(request: FixRequest) -> FixOutcome:
        seen.append(request.to_prompt())
        return FixOutcome(pushed=False)

    run_review_contour(
        _PIPELINE,
        fetch_diff=_diff,
        read_rollup=lambda: _RED,
        review=lambda _d: _verdict("request_changes"),
        ruleset=parse_ruleset("## Guard\n\n- Never touch the vendored parser.\n"),
        fix_runner=_fix,
        max_passes=2,
    )

    assert "Never touch the vendored parser." in seen[0]
