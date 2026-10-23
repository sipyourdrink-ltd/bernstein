"""Tests for the upgrade-proposal admission gate."""

from __future__ import annotations

import pytest

from bernstein.core.quality.empirical_confidence import ConfidenceQuery
from bernstein.core.tasks.models import RiskAssessment, RollbackPlan
from bernstein.evolution.admission import (
    DEFAULT_MIN_CONFIDENCE,
    UNATTRIBUTED_PRODUCER,
    AdmissionMode,
    AdmissionPolicy,
    ColdStartMode,
    decision_key,
    producer_identity,
)
from bernstein.evolution.detector import UpgradeCategory
from bernstein.evolution.proposals import AnalysisTrigger, UpgradeProposal


def _proposal(
    *,
    produced_by: str = "detector",
    category: UpgradeCategory = UpgradeCategory.POLICY_UPDATE,
    trigger: AnalysisTrigger = AnalysisTrigger.SCHEDULED,
) -> UpgradeProposal:
    return UpgradeProposal(
        id="prop-1",
        title="Speed up the thing",
        category=category,
        description="desc",
        current_state="slow",
        proposed_change="fast",
        benefits=["speed"],
        risk_assessment=RiskAssessment(level="low", breaking_changes=False, affected_components=[], mitigation=""),
        rollback_plan=RollbackPlan(steps=["revert"], estimated_rollback_minutes=5),
        cost_estimate_usd=0.0,
        expected_improvement="faster",
        confidence=0.9,
        triggered_by=trigger,
        produced_by=produced_by,
    )


@pytest.fixture()
def query(tmp_path) -> ConfidenceQuery:
    return ConfidenceQuery(db_path=tmp_path / "confidence.db", min_samples=5)


def test_decision_key_is_category_and_trigger(tmp_path) -> None:
    proposal = _proposal(category=UpgradeCategory.POLICY_UPDATE, trigger=AnalysisTrigger.SCHEDULED)
    key = decision_key(proposal)
    assert key.startswith("category:")
    assert "|trigger:" in key
    # Stable enough to accumulate samples: two proposals differing only by id
    # share a key.
    assert key == decision_key(_proposal())


def test_gate_measures_the_proposer_not_the_executor() -> None:
    """``to_task`` hard-codes role="manager"; gating on that would score
    whoever applies the change rather than whoever proposed it."""
    proposal = _proposal(produced_by="opportunity-detector")
    assert producer_identity(proposal) == "opportunity-detector"
    assert proposal.to_task().role == "manager"
    assert producer_identity(proposal) != proposal.to_task().role


def test_unattributed_proposals_get_their_own_history() -> None:
    assert producer_identity(_proposal(produced_by="")) == UNATTRIBUTED_PRODUCER
    assert producer_identity(_proposal(produced_by="   ")) == UNATTRIBUTED_PRODUCER


def test_cold_start_is_fail_closed_by_default(query) -> None:
    policy = AdmissionPolicy(query=query, mode=AdmissionMode.ENFORCE)
    assert policy.cold_start is ColdStartMode.FAIL_CLOSED

    decision = policy.evaluate(_proposal())

    assert decision.admitted is False
    assert decision.confidence.insufficient_data is True
    assert "cold start" in decision.reason


def test_cold_start_can_be_opened_but_only_explicitly(query) -> None:
    policy = AdmissionPolicy(query=query, cold_start=ColdStartMode.FAIL_OPEN)

    decision = policy.evaluate(_proposal())

    assert decision.admitted is True
    assert decision.confidence.insufficient_data is True


def test_invalid_cold_start_env_falls_back_to_closed(query, monkeypatch) -> None:
    """A typo in configuration must not silently open the gate."""
    monkeypatch.setenv("BERNSTEIN_ADMISSION_COLD_START", "fail-open")

    policy = AdmissionPolicy(query=query, mode=AdmissionMode.ENFORCE)

    assert policy.cold_start is ColdStartMode.FAIL_CLOSED
    assert policy.evaluate(_proposal()).admitted is False


def test_a_reliable_producer_is_admitted(query) -> None:
    proposal = _proposal()
    key = decision_key(proposal)
    for _ in range(5):
        query.record("detector", key, True)

    decision = AdmissionPolicy(query=query, mode=AdmissionMode.ENFORCE).evaluate(proposal)

    assert decision.admitted is True
    assert decision.confidence.samples == 5
    assert decision.confidence.insufficient_data is False


def test_an_unreliable_producer_is_refused(query) -> None:
    proposal = _proposal()
    key = decision_key(proposal)
    for outcome in (True, False, False, False, False):
        query.record("detector", key, outcome)

    decision = AdmissionPolicy(query=query, mode=AdmissionMode.ENFORCE).evaluate(proposal)

    assert decision.admitted is False
    assert decision.confidence.value == pytest.approx(0.2)
    assert str(DEFAULT_MIN_CONFIDENCE) in decision.reason or "threshold" in decision.reason


def test_history_is_per_producer(query) -> None:
    proposal = _proposal(produced_by="detector")
    key = decision_key(proposal)
    for _ in range(5):
        query.record("detector", key, True)

    policy = AdmissionPolicy(query=query, mode=AdmissionMode.ENFORCE)

    assert policy.evaluate(proposal).admitted is True
    # A different producer inherits nothing from the reliable one.
    other = policy.evaluate(_proposal(produced_by="other-agent"))
    assert other.admitted is False
    assert other.confidence.samples == 0


def test_history_is_per_decision_key(query) -> None:
    reliable = _proposal(category=UpgradeCategory.POLICY_UPDATE)
    for _ in range(5):
        query.record("detector", decision_key(reliable), True)

    policy = AdmissionPolicy(query=query, mode=AdmissionMode.ENFORCE)
    assert policy.evaluate(reliable).admitted is True

    different_trigger = _proposal(trigger=AnalysisTrigger.MANUAL)
    assert policy.evaluate(different_trigger).confidence.samples == 0


def test_outcome_records_against_the_admitting_key(query) -> None:
    """Recording against a recomputed key would let a proposal mutated between
    admission and apply poison a different history than the one that admitted
    it."""
    proposal = _proposal()
    policy = AdmissionPolicy(query=query, cold_start=ColdStartMode.FAIL_OPEN)
    decision = policy.evaluate(proposal)

    policy.record_outcome(decision, True, evidence_uri="file://receipt")

    recorded = query.get(decision.agent_type, decision.decision_key)
    assert recorded.samples == 1


def test_recording_survives_a_mutated_proposal(query) -> None:
    proposal = _proposal(produced_by="detector")
    policy = AdmissionPolicy(query=query, cold_start=ColdStartMode.FAIL_OPEN)
    decision = policy.evaluate(proposal)

    # Same object, different identity by the time the outcome is known.
    mutated = _proposal(produced_by="someone-else")
    assert producer_identity(mutated) != decision.agent_type

    policy.record_outcome(decision, False)

    assert query.get("detector", decision.decision_key).samples == 1
    assert query.get("someone-else", decision.decision_key).samples == 0


def test_enforcing_on_a_cold_database_would_deadlock_so_observe_is_default(query) -> None:
    """Fail-closed cold start plus record-after-apply cannot bootstrap: nothing
    is admitted, so nothing applies, so no outcome is recorded, so the sample
    count never reaches the threshold. Observe mode is the way out, and is
    therefore the default."""
    assert AdmissionPolicy(query=query).mode is AdmissionMode.OBSERVE

    observing = AdmissionPolicy(query=query)
    decision = observing.evaluate(_proposal())
    assert decision.admitted is True
    assert "would refuse" in decision.reason

    enforcing = AdmissionPolicy(query=query, mode=AdmissionMode.ENFORCE)
    assert enforcing.evaluate(_proposal()).admitted is False


def test_observe_mode_still_records_so_the_gate_can_open(query) -> None:
    policy = AdmissionPolicy(query=query)
    proposal = _proposal()
    for _ in range(5):
        policy.record_outcome(policy.evaluate(proposal), True)

    enforcing = AdmissionPolicy(query=query, mode=AdmissionMode.ENFORCE)
    decision = enforcing.evaluate(proposal)
    assert decision.confidence.samples == 5
    assert decision.admitted is True


# ---------------------------------------------------------------------------
# Issue #5407 – fast-track bypass removed; routing thresholds defined once
# ---------------------------------------------------------------------------


def test_classify_risk_route_high_risk_returns_sandbox_verify() -> None:
    """composite_risk > 0.6 must always route to sandbox_verify."""
    from bernstein.evolution.loop import EvolutionLoop

    assert EvolutionLoop._classify_risk_route(0.7) == "sandbox_verify"
    assert EvolutionLoop._classify_risk_route(1.0) == "sandbox_verify"


def test_classify_risk_route_low_risk_returns_standard_not_fast_track() -> None:
    """After #5407 there is no fast_track route; low risk still goes to standard."""
    from bernstein.evolution.loop import EvolutionLoop

    assert EvolutionLoop._classify_risk_route(0.0) == "standard"
    assert EvolutionLoop._classify_risk_route(0.29) == "standard"


def test_classify_risk_route_mid_risk_returns_standard() -> None:
    from bernstein.evolution.loop import EvolutionLoop

    assert EvolutionLoop._classify_risk_route(0.5) == "standard"


def test_proposal_scorer_classify_delegates_to_loop() -> None:
    """ProposalScorer.classify_risk_route must return the same value as EvolutionLoop._classify_risk_route."""
    from bernstein.evolution.loop import EvolutionLoop
    from bernstein.evolution.proposal_scorer import ProposalScorer

    scorer = ProposalScorer()
    for risk in (0.0, 0.29, 0.31, 0.5, 0.61, 1.0):
        assert scorer.classify_risk_route(risk) == EvolutionLoop._classify_risk_route(risk)


def test_make_fast_track_sandbox_result_is_gone() -> None:
    """make_fast_track_sandbox_result must no longer exist in _shared or loop."""
    import importlib

    shared = importlib.import_module("bernstein.evolution._shared")
    assert not hasattr(shared, "make_fast_track_sandbox_result"), (
        "make_fast_track_sandbox_result was removed by #5407 but is still present in _shared"
    )

    loop_mod = importlib.import_module("bernstein.evolution.loop")
    # The private method was removed too.
    assert not hasattr(loop_mod.EvolutionLoop, "_make_fast_track_sandbox_result"), (
        "_make_fast_track_sandbox_result was removed by #5407 but is still on EvolutionLoop"
    )
