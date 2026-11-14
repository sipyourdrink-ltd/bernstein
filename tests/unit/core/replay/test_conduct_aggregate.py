"""Regression tests for combining supplied per-run conduct projections."""

from __future__ import annotations

from dataclasses import replace

import pytest

from bernstein.core.replay.conduct_fold import (
    CompletionCoverageStatus,
    ConductProjection,
    aggregate_conduct,
    derive_conduct,
)

_PRINCIPAL = "agent-1"


def _run(
    run_id: str,
    event_types: tuple[str, ...],
    *,
    principal_id: str = _PRINCIPAL,
    start: float = 100.0,
    end: float = 150.0,
) -> ConductProjection:
    # Keep a non-attributable row so the ranges must retain journal indices,
    # rather than being renumbered in either the per-run fold or the aggregate.
    events = [{"event": "tool_call", "principal_id": "unrelated", "ts": start}]
    events.extend({"event": event, "principal_id": principal_id, "ts": start} for event in event_types)
    return derive_conduct(events, run_id=run_id, principal_id=principal_id, period_start=start, period_end=end)


@pytest.fixture
def two_runs() -> tuple[ConductProjection, ConductProjection]:
    return (
        _run(
            "run-a",
            (
                "tool_call",
                "task_completed",
                "task_completed",
                "approval_gate",
                "approval_honoured",
                "task_delegated",
                "zeta",
            ),
        ),
        _run(
            "run-b",
            (
                "task_completed",
                "tool_call",
                "task_verification_failed",
                "approval_overridden",
                "task_submitted",
                "alpha",
            ),
            start=150.0,
            end=190.0,
        ),
    )


def test_combines_counts_and_preserves_both_run_ranges(two_runs: tuple[ConductProjection, ConductProjection]) -> None:
    aggregate = aggregate_conduct(two_runs, principal_id=_PRINCIPAL, period_start=100.0, period_end=200.0)
    document = aggregate.to_dict()

    assert aggregate.event_count == 11
    assert aggregate.coverage == CompletionCoverageStatus.VERIFIED
    assert aggregate.counts == {
        "tool_calls": 2,
        "completed": 3,
        "failed": 1,
        "gated": 1,
        "gated_honoured": 1,
        "gated_overridden": 1,
        "delegated": 2,
    }
    assert document["counts"] == aggregate.counts
    assert document["principal_id"] == _PRINCIPAL
    assert document["period_start"] == 100.0
    assert document["period_end"] == 200.0
    assert document["ignored_event_types"] == ["alpha", "zeta"]
    assert document["runs"] == [run.to_dict() for run in two_runs]
    assert document["runs"][0]["completed"]["event_index_range"] == {"first": 2, "last": 3}
    assert document["runs"][1]["completed"]["event_index_range"] == {"first": 1, "last": 1}
    assert aggregate.runs[0] is two_runs[0]
    assert aggregate.runs[1] is two_runs[1]


def test_excludes_other_agent_and_wholly_outside_runs(two_runs: tuple[ConductProjection, ConductProjection]) -> None:
    excluded = (
        _run("other-agent", ("task_completed",), principal_id="agent-2"),
        _run("before", ("task_completed",), start=50.0, end=99.0),
        _run("at-end", ("task_completed",), start=200.0, end=200.0),
        _run("after", ("task_completed",), start=250.0, end=300.0),
    )
    expected = aggregate_conduct(two_runs, principal_id=_PRINCIPAL, period_start=100.0, period_end=200.0)
    actual = aggregate_conduct((*excluded, *two_runs), principal_id=_PRINCIPAL, period_start=100.0, period_end=200.0)

    assert actual.canonical_bytes() == expected.canonical_bytes()


def test_reversed_input_has_identical_canonical_bytes(two_runs: tuple[ConductProjection, ConductProjection]) -> None:
    forward = aggregate_conduct(two_runs, principal_id=_PRINCIPAL, period_start=100.0, period_end=200.0)
    backward = aggregate_conduct(
        tuple(reversed(two_runs)), principal_id=_PRINCIPAL, period_start=100.0, period_end=200.0
    )

    assert forward.canonical_bytes() == backward.canonical_bytes()


def test_order_is_stable_even_when_run_ids_tie(two_runs: tuple[ConductProjection, ConductProjection]) -> None:
    runs = (two_runs[0], replace(two_runs[1], run_id=two_runs[0].run_id))
    forward = aggregate_conduct(runs, principal_id=_PRINCIPAL, period_start=100.0, period_end=200.0)
    backward = aggregate_conduct(tuple(reversed(runs)), principal_id=_PRINCIPAL, period_start=100.0, period_end=200.0)

    assert forward.canonical_bytes() == backward.canonical_bytes()


@pytest.mark.parametrize("start,end", [(100.0, 100.0), (99.0, 100.0)])
def test_inclusive_source_end_overlaps_aggregate_start(start: float, end: float) -> None:
    run = derive_conduct(
        [{"event": "task_completed", "principal_id": _PRINCIPAL, "ts": 100.0}],
        run_id="at-start",
        principal_id=_PRINCIPAL,
        period_start=start,
        period_end=end,
    )
    aggregate = aggregate_conduct([run], principal_id=_PRINCIPAL, period_start=100.0, period_end=200.0)

    assert aggregate.runs == (run,)
    assert aggregate.counts["completed"] == 1


def test_overlapping_projection_is_retained_without_clipping() -> None:
    run = _run("straddles-start", ("task_completed",), start=90.0, end=110.0)
    aggregate = aggregate_conduct([run], principal_id=_PRINCIPAL, period_start=100.0, period_end=200.0)

    # Only projections are supplied, so there is no event timestamp information
    # with which to clip their counts; provenance and period remain unchanged.
    assert aggregate.runs == (run,)
    assert aggregate.to_dict()["runs"] == [run.to_dict()]


@pytest.mark.parametrize("start,end", [(100.0, 200.0), (125.0, 125.0)])
def test_no_selected_rows_is_unverified(start: float, end: float) -> None:
    runs = [] if start < end else [_run("run-a", ("task_completed",))]
    aggregate = aggregate_conduct(runs, principal_id=_PRINCIPAL, period_start=start, period_end=end)

    assert aggregate.runs == ()
    assert aggregate.event_count == 0
    assert aggregate.coverage == CompletionCoverageStatus.UNVERIFIED
    assert all(count == 0 for count in aggregate.counts.values())


def test_does_not_upgrade_unverified_source(two_runs: tuple[ConductProjection, ConductProjection]) -> None:
    runs = (two_runs[0], replace(two_runs[1], coverage=CompletionCoverageStatus.UNVERIFIED))
    aggregate = aggregate_conduct(runs, principal_id=_PRINCIPAL, period_start=100.0, period_end=200.0)

    assert aggregate.event_count == 11
    assert aggregate.coverage == CompletionCoverageStatus.UNVERIFIED


@pytest.mark.parametrize("start,end", [(200.0, 100.0), (float("nan"), 200.0), (100.0, float("inf"))])
def test_invalid_period_is_rejected(start: float, end: float) -> None:
    with pytest.raises(ValueError, match="period bounds must be finite"):
        aggregate_conduct([], principal_id=_PRINCIPAL, period_start=start, period_end=end)
