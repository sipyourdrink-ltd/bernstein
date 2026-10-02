"""The executor is the single admission point for both evolution paths."""

from __future__ import annotations

import pytest

from bernstein.core.quality.empirical_confidence import ConfidenceQuery
from bernstein.core.tasks.models import RiskAssessment, RollbackPlan
from bernstein.evolution.admission import (
    AdmissionMode,
    AdmissionPolicy,
    ColdStartMode,
    decision_key,
)
from bernstein.evolution.applicator import FileUpgradeExecutor
from bernstein.evolution.detector import UpgradeCategory
from bernstein.evolution.proposals import AnalysisTrigger, UpgradeProposal


def _proposal(produced_by: str = "detector") -> UpgradeProposal:
    return UpgradeProposal(
        id="prop-exec",
        title="Tune policy",
        category=UpgradeCategory.POLICY_UPDATE,
        description="desc",
        current_state="a",
        proposed_change="b",
        benefits=["x"],
        risk_assessment=RiskAssessment(level="low", breaking_changes=False, affected_components=[], mitigation=""),
        rollback_plan=RollbackPlan(steps=["revert"], estimated_rollback_minutes=5),
        cost_estimate_usd=0.0,
        expected_improvement="better",
        confidence=0.9,
        triggered_by=AnalysisTrigger.SCHEDULED,
        produced_by=produced_by,
    )


@pytest.fixture()
def query(tmp_path) -> ConfidenceQuery:
    return ConfidenceQuery(db_path=tmp_path / "confidence.db", min_samples=5)


def test_unmeasured_producer_never_touches_the_filesystem(tmp_path, query) -> None:
    """Cold start is fail-closed, so nothing is written before the gate."""
    state_dir = tmp_path / "state"
    executor = FileUpgradeExecutor(state_dir, admission=AdmissionPolicy(query=query, mode=AdmissionMode.ENFORCE))
    config_before = sorted(p.name for p in (state_dir / "config").iterdir())

    assert executor.execute_upgrade(_proposal()) is False
    assert sorted(p.name for p in (state_dir / "config").iterdir()) == config_before


def test_refusal_is_not_recorded_as_an_outcome(tmp_path, query) -> None:
    """A proposal that was never applied says nothing about the producer."""
    executor = FileUpgradeExecutor(
        tmp_path / "state",
        admission=AdmissionPolicy(query=query, mode=AdmissionMode.ENFORCE),
    )
    proposal = _proposal()

    executor.execute_upgrade(proposal)

    assert query.get("detector", decision_key(proposal)).samples == 0


def test_an_admitted_apply_records_its_outcome(tmp_path, query) -> None:
    executor = FileUpgradeExecutor(
        tmp_path / "state",
        admission=AdmissionPolicy(query=query, cold_start=ColdStartMode.FAIL_OPEN),
    )
    proposal = _proposal()

    executor.execute_upgrade(proposal)

    assert query.get("detector", decision_key(proposal)).samples == 1


def test_failures_are_recorded_too(tmp_path, query, monkeypatch) -> None:
    """A gate that only learns from successes cannot lower its opinion of a
    producer that keeps breaking things."""
    executor = FileUpgradeExecutor(
        tmp_path / "state",
        admission=AdmissionPolicy(query=query, cold_start=ColdStartMode.FAIL_OPEN),
    )
    proposal = _proposal()
    monkeypatch.setattr(executor, "_apply_policy_update", lambda _p: (_ for _ in ()).throw(RuntimeError("boom")))

    assert executor.execute_upgrade(proposal) is False

    confidence = query.get("detector", decision_key(proposal))
    assert confidence.samples == 1


def test_a_bad_producer_gates_itself_out_over_time(tmp_path, query, monkeypatch) -> None:
    """The loop the issue asks for: history accumulates, then closes the gate."""
    executor = FileUpgradeExecutor(
        tmp_path / "state",
        admission=AdmissionPolicy(query=query, cold_start=ColdStartMode.FAIL_OPEN),
    )
    proposal = _proposal()
    monkeypatch.setattr(executor, "_apply_policy_update", lambda _p: (_ for _ in ()).throw(RuntimeError("boom")))

    for _ in range(5):
        executor.execute_upgrade(proposal)

    assert query.get("detector", decision_key(proposal)).samples == 5

    # Now measured and bad: even a fail-open policy refuses it, because the key
    # is no longer cold.
    strict = FileUpgradeExecutor(
        tmp_path / "state2",
        admission=AdmissionPolicy(
            query=query,
            cold_start=ColdStartMode.FAIL_OPEN,
            mode=AdmissionMode.ENFORCE,
        ),
    )
    assert strict.execute_upgrade(proposal) is False


def test_default_construction_still_works(tmp_path) -> None:
    """Existing callers construct with a state dir only; that must keep working
    and must get the gate rather than bypassing it."""
    executor = FileUpgradeExecutor(tmp_path / "state")
    assert executor._admission is not None


# ---------------------------------------------------------------------------
# Issue #5408 – rollback_upgrade uses proposal and persists a receipt
# ---------------------------------------------------------------------------


def test_rollback_of_unapplied_proposal_persists_receipt(tmp_path) -> None:
    """Nothing applied: rollback succeeds and leaves a hash-anchored receipt."""
    import json

    state_dir = tmp_path / "state"
    executor = FileUpgradeExecutor(state_dir)
    proposal = _proposal()

    assert executor.rollback_upgrade(proposal) is True

    receipt = json.loads((state_dir / "upgrades" / "rollbacks" / f"{proposal.id}.json").read_text(encoding="utf-8"))
    assert receipt["proposal_id"] == proposal.id
    assert receipt["restored_files"] == []
    assert receipt["receipt_hash"]


def test_rollback_does_not_use_ignored_proposal_argument() -> None:
    """The _proposal rename to proposal means the signature now accepts and uses it."""
    import inspect

    from bernstein.evolution.applicator import FileUpgradeExecutor

    sig = inspect.signature(FileUpgradeExecutor.rollback_upgrade)
    params = list(sig.parameters)
    # First param is 'self'; second must be 'proposal', not '_proposal'.
    assert params[1] == "proposal", f"parameter is still named {params[1]!r}"


def test_rollback_restores_backup_files(tmp_path) -> None:
    """After rollback the backed-up file contents are back in config/."""
    state_dir = tmp_path / "state"
    proposal = _proposal()
    applying = FileUpgradeExecutor(state_dir)
    config_file = state_dir / "config" / "policies.yaml"
    config_file.write_text("original: true\n", encoding="utf-8")
    applying._backup_file("policies.yaml", proposal.id)
    config_file.write_text("broken: yes\n", encoding="utf-8")
    applying._record_history(proposal, "applied")

    assert FileUpgradeExecutor(state_dir).rollback_upgrade(proposal) is True
    assert config_file.read_text(encoding="utf-8") == "original: true\n"
