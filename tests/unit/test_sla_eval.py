"""Unit tests for the pure SLA evaluators, budget projection, and remediation."""

from __future__ import annotations

import json

from bernstein.core.observability.sla_eval import (
    evaluate_all,
    evaluate_freshness,
    gate_remediation,
    project_error_budget,
    select_remediation,
)
from bernstein.core.planning.sla_store import build_contract

_NOW = 1_000_000


def _sched(**kw: object) -> object:
    return build_contract(subject_type="schedule", subject_id="sched_abc", **kw)  # type: ignore[arg-type]


def test_freshness_pure_from_spine_no_filesystem() -> None:
    """Freshness is a pure function of spine rows; a stale artifact breaches."""
    contract = _sched(artifact_freshness_s=3600, artifact_path="report.md")
    fresh = evaluate_freshness(
        3600, "report.md", [{"artifact_path": "report.md", "timestamp": _NOW - 100, "entry_hash": "sha256:a"}], _NOW
    )
    stale = evaluate_freshness(
        3600, "report.md", [{"artifact_path": "report.md", "timestamp": _NOW - 99999, "entry_hash": "sha256:b"}], _NOW
    )
    assert not fresh.breached
    assert stale.breached
    # The verdict embeds exactly the spine entry hashes it judged.
    assert stale.evidence_hashes == ("sha256:b",)
    assert contract.artifact_freshness_s == 3600


def test_freshness_never_derived_is_a_breach() -> None:
    """An artifact with no matching spine row is maximally stale (a breach)."""
    v = evaluate_freshness(
        3600, "missing.md", [{"artifact_path": "other.md", "timestamp": _NOW, "entry_hash": "x"}], _NOW
    )
    assert v.breached
    assert v.evidence_hashes == ()


def test_evaluate_all_only_runs_declared_axes() -> None:
    contract = _sched(max_run_duration_s=1800, artifact_freshness_s=3600, artifact_path="r.md")
    verdicts = evaluate_all(contract, {}, _NOW)
    axes = {v.axis for v in verdicts}
    assert axes == {"max_run_duration", "artifact_freshness"}


def test_error_budget_projection_is_deterministic_and_pure() -> None:
    """Two projections over the same segment produce byte-identical JSON."""
    contract = _sched(max_run_duration_s=1800, budget_events=2)
    segment = [
        {"kind": "task.completed", "entry_hash": "a"},
        {"kind": "task.failed", "entry_hash": "b"},
        {"kind": "run.open", "entry_hash": "ignored"},
        {"kind": "task.abandoned", "entry_hash": "c"},
    ]
    p1 = project_error_budget(contract, segment).to_dict()
    p2 = project_error_budget(contract, segment).to_dict()
    assert json.dumps(p1, sort_keys=True) == json.dumps(p2, sort_keys=True)
    assert p1["total_events"] == 3
    assert p1["failed_events"] == 2
    assert p1["budget_remaining"] == 0
    assert p1["is_depleted"] is True
    assert p1["segment_head"] == "c"


def test_remediation_is_bidirectional() -> None:
    """A deadline breach spends more; a spend-rate breach throttles."""
    deadline = _sched(max_run_duration_s=1800)
    dur_ev = {"max_run_duration": [{"task_id": "t", "started": _NOW - 4000, "ended": _NOW, "entry_hash": "d"}]}
    plan_deadline = select_remediation(deadline, evaluate_all(deadline, dur_ev, _NOW))
    assert plan_deadline.requested_action == "upgrade_model"
    assert plan_deadline.spends_more is True

    spend = build_contract(subject_type="envelope", subject_id="subscription", spend_rate_usd_per_hour=1.0)
    spend_ev = {"spend_rate": [{"cost_usd": 10.0, "timestamp": _NOW - 3600, "entry_hash": "s"}]}
    plan_spend = select_remediation(spend, evaluate_all(spend, spend_ev, _NOW))
    assert plan_spend.requested_action == "reduce_agents"
    assert plan_spend.spends_more is False


def test_remediation_selection_is_deterministic() -> None:
    """Driving selection twice on the same verdicts yields identical actions."""
    contract = _sched(max_run_duration_s=1800)
    dur_ev = {"max_run_duration": [{"task_id": "t", "started": _NOW - 4000, "ended": _NOW, "entry_hash": "d"}]}
    verdicts = evaluate_all(contract, dur_ev, _NOW)
    a = select_remediation(contract, verdicts).to_dict()
    b = select_remediation(contract, verdicts).to_dict()
    assert a == b


def test_spend_more_remediation_blocked_falls_back_deterministically() -> None:
    """A model upgrade refused by the budget gate records the deterministic fallback."""
    contract = _sched(max_run_duration_s=1800, remediation_cost_usd=5.0)
    dur_ev = {"max_run_duration": [{"task_id": "t", "started": _NOW - 4000, "ended": _NOW, "entry_hash": "d"}]}
    plan = select_remediation(contract, evaluate_all(contract, dur_ev, _NOW))

    blocked = gate_remediation(
        plan, spend_rows=[], caps={"per_run_usd": 1.0}, remediation_cost_usd=5.0, tick_instant=_NOW
    )
    assert blocked.blocked is True
    assert blocked.admitted is False
    assert blocked.effective_action == "increase_review"
    assert blocked.breached_dimension == "run"
    assert blocked.decision_hash

    admitted = gate_remediation(
        plan, spend_rows=[], caps={"per_run_usd": 100.0}, remediation_cost_usd=5.0, tick_instant=_NOW
    )
    assert admitted.admitted is True
    assert admitted.effective_action == "upgrade_model"

    # Determinism: identical inputs reproduce the identical decision hash.
    again = gate_remediation(
        plan, spend_rows=[], caps={"per_run_usd": 1.0}, remediation_cost_usd=5.0, tick_instant=_NOW
    )
    assert again.to_dict() == blocked.to_dict()
