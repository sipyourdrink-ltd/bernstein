"""Read-only conduct comparison from supplied authority and activity evidence."""

from dataclasses import replace

import pytest

from bernstein.core.identity.delegation_scope import DelegationScope
from bernstein.core.replay.conduct_fold import ConductScopeActivity, aggregate_conduct, derive_conduct


def _run(run_id: str = "run-a"):
    return derive_conduct(
        [
            {"event": "tool_call", "principal_id": "other", "ts": 1},
            {"event": "tool_call", "principal_id": "agent", "ts": 1},
        ],
        principal_id="agent",
        period_start=0,
        period_end=2,
        run_id=run_id,
    )


def _activity(run_id: str = "run-a", scope: DelegationScope | None = None) -> ConductScopeActivity:
    return ConductScopeActivity(
        run_id=run_id, principal_id="agent", activity="recorded file read", first_index=1, last_index=1, scope=scope
    )


def _aggregate(runs, activities=(), scope=None):
    return aggregate_conduct(
        runs, principal_id="agent", period_start=0, period_end=3, recorded_activities=activities, scope_projection=scope
    )


def test_in_scope_recorded_activity_has_no_divergence() -> None:
    allowed = DelegationScope(permissions=frozenset({"read"}), path_prefixes=frozenset({"src"}))
    actual = DelegationScope(permissions=frozenset({"read"}), path_prefixes=frozenset({"src/core"}))
    result = _aggregate([_run()], [_activity(scope=actual)], allowed).to_dict()["scope_comparison"]
    assert result["status"] == "available"
    assert result["divergences"] == []
    assert result["compared_activity_count"] == 1
    assert result["scope_ref"] == allowed.scope_ref()


def test_out_of_scope_activity_names_axis_run_and_original_event_range() -> None:
    allowed = DelegationScope(permissions=frozenset({"read"}), allowed_files=frozenset({"src/*"}))
    actual = DelegationScope(permissions=frozenset({"write"}), allowed_files=frozenset({"src/core/file.py"}))
    result = _aggregate([_run()], [_activity(scope=actual)], allowed).to_dict()["scope_comparison"]
    row = result["divergences"][0]
    assert row["run_id"] == "run-a"
    assert row["recorded_activity"] == "recorded file read"
    assert row["event_index_range"] == {"first": 1, "last": 1}
    assert row["divergent_axes"] == ["permissions", "allowed_files"]
    assert row["recorded_scope"] == actual.to_body()


def test_missing_projection_is_unavailable() -> None:
    result = _aggregate([_run()], [_activity(scope=DelegationScope())]).to_dict()["scope_comparison"]
    assert result["status"] == "unavailable"
    assert result["reason"] == "scope_projection_unavailable"
    assert result["divergences"] == []


def test_missing_activity_evidence_is_not_clean() -> None:
    result = _aggregate([_run()], scope=DelegationScope()).to_dict()["scope_comparison"]
    assert result["status"] == "unavailable"
    assert result["unavailable"][0]["run_id"] == "run-a"


def test_unknown_activity_scope_keeps_other_divergence_and_partial_status() -> None:
    result = _aggregate(
        [_run(), _run("run-b")],
        [_activity(scope=DelegationScope(permissions=frozenset({"write"}))), _activity("run-b")],
        DelegationScope(),
    ).to_dict()["scope_comparison"]
    assert result["status"] == "partial"
    assert len(result["divergences"]) == 1
    assert result["unavailable"][0]["run_id"] == "run-b"
    assert result["unavailable"][0]["reason"] == "recorded_scope_unavailable"


def test_input_order_is_stable_and_original_projections_are_preserved() -> None:
    runs = [_run(), _run("run-b")]
    activities = [_activity(scope=DelegationScope()), _activity("run-b", DelegationScope())]
    a = _aggregate(runs, activities, DelegationScope())
    b = _aggregate(list(reversed(runs)), list(reversed(activities)), DelegationScope())
    assert a.canonical_bytes() == b.canonical_bytes()
    assert a.to_dict()["runs"] == [run.to_dict() for run in runs]
    assert (
        a.counts
        == b.counts
        == {
            "tool_calls": 2,
            "completed": 0,
            "failed": 0,
            "gated": 0,
            "gated_honoured": 0,
            "gated_overridden": 0,
            "delegated": 0,
        }
    )


def test_activity_for_another_principal_or_run_does_not_enter_comparison() -> None:
    actual = _activity(scope=DelegationScope())
    other = replace(actual, principal_id="other")
    result = _aggregate([_run()], [actual, other, replace(actual, run_id="outside")], DelegationScope())
    assert result.to_dict()["scope_comparison"]["compared_activity_count"] == 1


@pytest.mark.parametrize("first,last", [(-1, 1), (2, 1), (True, 1)])
def test_invalid_source_ranges_are_rejected(first, last) -> None:
    with pytest.raises(ValueError):
        ConductScopeActivity("run-a", "agent", "read", first, last, DelegationScope())


def test_empty_unverified_source_remains_an_explicit_comparison_gap() -> None:
    empty = derive_conduct([], run_id="missing", principal_id="agent", period_start=0, period_end=2)
    result = _aggregate([_run(), empty], [_activity(scope=DelegationScope())], DelegationScope()).to_dict()
    assert result["coverage"] == "unverified"
    assert result["scope_comparison"]["status"] == "partial"
    assert result["scope_comparison"]["unavailable"] == [{"run_id": "missing", "reason": "source_run_unverified"}]


def test_serialization_cannot_mutate_saved_comparison_or_canonical_bytes() -> None:
    aggregate = _aggregate(
        [_run()], [_activity(scope=DelegationScope(permissions=frozenset({"write"})))], DelegationScope()
    )
    before = aggregate.canonical_bytes()
    exported = aggregate.to_dict()
    exported["scope_comparison"]["divergences"].clear()
    exported["scope_comparison"]["scope_projection"]["permissions"].append("write")
    assert aggregate.canonical_bytes() == before
    assert len(aggregate.to_dict()["scope_comparison"]["divergences"]) == 1


def test_equivalent_scope_references_have_stable_emitted_body_order() -> None:
    scopes = [DelegationScope.from_body({"permissions": ["write"], "not_after": expiry}) for expiry in (-0.0, 0.0)]
    assert scopes[0].scope_ref() == scopes[1].scope_ref()
    activities = [_activity(scope=scope) for scope in scopes]
    forward = _aggregate([_run()], activities, DelegationScope())
    reversed_input = _aggregate([_run()], list(reversed(activities)), DelegationScope())
    assert len(forward.to_dict()["scope_comparison"]["divergences"]) == 2
    assert forward.canonical_bytes() == reversed_input.canonical_bytes()
