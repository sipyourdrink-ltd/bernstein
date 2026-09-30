"""A rollback after a partial apply trips the circuit breaker (#6317).

#5913 fed the breaker only when `was_applied` was true, but nothing wrote an
`applied` history row, so a failed apply that had already mutated a file and was
then reverted left the breaker CLOSED. The signal is now the backup manifest
`_backup_file` writes before it mutates anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from bernstein.core.models import RiskAssessment, RollbackPlan

from bernstein.evolution.admission import AdmissionPolicy, ColdStartMode
from bernstein.evolution.applicator import FileUpgradeExecutor
from bernstein.evolution.circuit import CircuitState
from bernstein.evolution.detector import UpgradeCategory
from bernstein.evolution.loop import EvolutionLoop
from bernstein.evolution.proposals import UpgradeProposal

if TYPE_CHECKING:
    from pathlib import Path

ORIGINAL = "policies:\n  max_retries: 3\n"


def _proposal(proposal_id: str = "p-partial") -> UpgradeProposal:
    return UpgradeProposal(
        id=proposal_id,
        title="Raise max_retries",
        category=UpgradeCategory.POLICY_UPDATE,
        description="desc",
        current_state="current",
        proposed_change="change",
        benefits=["benefit"],
        risk_assessment=RiskAssessment(level="low"),
        rollback_plan=RollbackPlan(steps=["revert"], estimated_rollback_minutes=5),
        cost_estimate_usd=0.0,
        expected_improvement="improve",
        confidence=0.9,
    )


def _loop(tmp_path: Path) -> EvolutionLoop:
    loop = EvolutionLoop(state_dir=tmp_path)
    # Cold start fails open so the proposal is admitted and reaches the sink.
    loop._executor = FileUpgradeExecutor(tmp_path, admission=AdmissionPolicy(cold_start=ColdStartMode.FAIL_OPEN))
    return loop


def test_rollback_after_a_partial_apply_trips_the_breaker(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    executor = loop._executor
    target = executor.config_dir / "policies.yaml"
    target.write_text(ORIGINAL, encoding="utf-8")
    proposal = _proposal()

    def half_apply(p: UpgradeProposal) -> bool:
        executor._backup_file("policies.yaml", p.id)
        target.write_text("half-written", encoding="utf-8")
        raise OSError("disk full")

    executor._apply_policy_update = half_apply  # type: ignore[method-assign]

    assert loop._apply_proposal(proposal, MagicMock()) is False
    assert target.read_text(encoding="utf-8") == ORIGINAL
    assert loop._breaker.state == CircuitState.OPEN
    assert len(loop._breaker.recent_rollbacks) == 1


def test_rollback_that_restored_nothing_does_not_trip_the_breaker(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    executor = loop._executor
    target = executor.config_dir / "policies.yaml"
    target.write_text(ORIGINAL, encoding="utf-8")

    # Fails before touching or backing up anything.
    executor._apply_policy_update = MagicMock(side_effect=OSError("nothing written"))  # type: ignore[method-assign]

    assert loop._apply_proposal(_proposal("p-noop"), MagicMock()) is False
    assert target.read_text(encoding="utf-8") == ORIGINAL
    assert loop._breaker.state == CircuitState.CLOSED
    assert loop._breaker.recent_rollbacks == []


def test_was_applied_is_false_again_once_rolled_back(tmp_path: Path) -> None:
    executor = FileUpgradeExecutor(tmp_path)
    (executor.config_dir / "policies.yaml").write_text(ORIGINAL, encoding="utf-8")
    proposal = _proposal()
    executor._backup_file("policies.yaml", proposal.id)
    assert executor.was_applied(proposal.id) is True
    executor.rollback_upgrade(proposal)
    assert executor.was_applied(proposal.id) is False
