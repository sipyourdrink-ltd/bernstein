"""Tests for EvolutionLoop and ExperimentResult from bernstein.evolution.loop."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.models import RiskAssessment, RollbackPlan
from bernstein.evolution.detector import ImprovementOpportunity, UpgradeCategory
from bernstein.evolution.gate import ApprovalDecision, ApprovalOutcome
from bernstein.evolution.loop import EvolutionLoop, ExperimentResult
from bernstein.evolution.proposals import (
    AnalysisTrigger,
    ApprovalMode,
    UpgradeProposal,
    UpgradeStatus,
)
from bernstein.evolution.types import RiskLevel
from bernstein.evolution.types import SandboxResult as TypesSandboxResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proposal(
    *,
    id: str = "test-001",
    risk_level: str = "low",
    confidence: float = 0.95,
) -> UpgradeProposal:
    return UpgradeProposal(
        id=id,
        title="Test",
        category=UpgradeCategory.POLICY_UPDATE,
        description="desc",
        current_state="current",
        proposed_change="change",
        benefits=["benefit"],
        risk_assessment=RiskAssessment(level=risk_level),
        rollback_plan=RollbackPlan(steps=["revert"], estimated_rollback_minutes=5),
        cost_estimate_usd=0.0,
        expected_improvement="improve",
        confidence=confidence,
    )


def _make_opportunity(
    *,
    risk_level: str = "low",
    confidence: float = 0.9,
) -> ImprovementOpportunity:
    return ImprovementOpportunity(
        category=UpgradeCategory.POLICY_UPDATE,
        title="Test opp",
        description="desc",
        expected_improvement="improve",
        confidence=confidence,
        risk_level=risk_level,
    )


def _make_sandbox_result(
    *,
    proposal_id: str = "x",
    passed: bool = True,
    candidate_score: float = 1.0,
    delta: float = 0.0,
) -> TypesSandboxResult:
    return TypesSandboxResult(
        proposal_id=proposal_id,
        passed=passed,
        tests_passed=5 if passed else 3,
        tests_failed=0 if passed else 2,
        tests_total=5,
        baseline_score=1.0,
        candidate_score=candidate_score,
        delta=delta,
        duration_seconds=1.0,
        log_path="",
    )


def _make_approval_decision(
    *,
    proposal_id: str = "x",
    outcome: ApprovalOutcome = ApprovalOutcome.AUTO_APPROVED,
) -> ApprovalDecision:
    return ApprovalDecision(
        proposal_id=proposal_id,
        risk_level=RiskLevel.L0_CONFIG,
        confidence=0.95,
        outcome=outcome,
        reason="auto",
        requires_human=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_experiment_result_to_dict() -> None:
    """ExperimentResult.to_dict() includes all expected fields."""
    result = ExperimentResult(
        proposal_id="P-001",
        title="Test",
        risk_level="config",
        baseline_score=1.0,
        candidate_score=1.05,
        delta=0.05,
        accepted=True,
        reason="Applied successfully",
        cost_usd=0.05,
        duration_seconds=12.3,
        timestamp=1000000.0,
    )

    d = result.to_dict()

    assert d["proposal_id"] == "P-001"
    assert d["title"] == "Test"
    assert d["risk_level"] == "config"
    assert d["baseline_score"] == 1.0
    assert d["candidate_score"] == 1.05
    assert d["delta"] == 0.05
    assert d["accepted"] is True
    assert d["reason"] == "Applied successfully"
    assert d["cost_usd"] == 0.05
    assert d["duration_seconds"] == 12.3
    assert d["timestamp"] == 1000000.0
    # Exactly 11 keys
    assert len(d) == 11


def test_evolution_loop_init(tmp_path: Path) -> None:
    """EvolutionLoop creates state directories and initializes counters at 0."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()

    loop = EvolutionLoop(state_dir, repo_root=tmp_path)

    # Directories created
    assert (state_dir / "evolution").is_dir()

    # Counters at zero
    assert loop._proposals_generated == 0
    assert loop._proposals_accepted == 0
    assert loop._experiments == []
    assert loop._running is False


def test_run_cycle_no_opportunities(tmp_path: Path) -> None:
    """run_cycle() returns None when no opportunities are detected."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[]),
        patch.object(loop, "_run_baseline", return_value=1.0),
    ):
        result = loop.run_cycle()

    assert result is None


def test_run_cycle_with_opportunity_auto_approved(tmp_path: Path) -> None:
    """Full happy-path cycle: opportunity -> proposal -> sandbox pass -> applied."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)

    opportunity = _make_opportunity()
    proposal = _make_proposal()
    sandbox_result = _make_sandbox_result(proposal_id=proposal.id)
    decision = _make_approval_decision(proposal_id=proposal.id)

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(
            loop._detector, "identify_opportunities", return_value=[opportunity],
        ),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(
            loop._proposal_generator, "create_proposal", return_value=proposal,
        ),
        patch.object(loop._breaker, "can_evolve", return_value=(True, "ok")),
        patch.object(loop._gate, "route", return_value=decision),
        patch.object(loop._sandbox, "validate", return_value=sandbox_result),
        patch.object(loop._executor, "execute_upgrade", return_value=True),
        patch.object(loop._breaker, "record_change"),
    ):
        result = loop.run_cycle()

    assert result is not None
    assert result.accepted is True
    assert result.proposal_id == "test-001"
    assert result.reason == "Applied successfully"

    # Experiment logged to JSONL
    experiments_path = state_dir / "evolution" / "experiments.jsonl"
    assert experiments_path.exists()
    data = json.loads(experiments_path.read_text().strip())
    assert data["accepted"] is True


def test_run_cycle_circuit_breaker_blocks(tmp_path: Path) -> None:
    """Circuit breaker blocking yields accepted=False with breaker reason."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)

    opportunity = _make_opportunity()
    proposal = _make_proposal()

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(
            loop._detector, "identify_opportunities", return_value=[opportunity],
        ),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(
            loop._proposal_generator, "create_proposal", return_value=proposal,
        ),
        patch.object(
            loop._breaker, "can_evolve", return_value=(False, "blocked"),
        ),
    ):
        result = loop.run_cycle()

    assert result is not None
    assert result.accepted is False
    assert "Circuit breaker" in result.reason


def test_run_cycle_deferred_for_human_review(tmp_path: Path) -> None:
    """Non-auto-approved proposals get written to deferred.jsonl."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)

    opportunity = _make_opportunity()
    proposal = _make_proposal()
    decision = ApprovalDecision(
        proposal_id=proposal.id,
        risk_level=RiskLevel.L2_LOGIC,
        confidence=0.75,
        outcome=ApprovalOutcome.HUMAN_REVIEW_4H,
        reason="needs human review",
        requires_human=True,
    )

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(
            loop._detector, "identify_opportunities", return_value=[opportunity],
        ),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(
            loop._proposal_generator, "create_proposal", return_value=proposal,
        ),
        patch.object(loop._breaker, "can_evolve", return_value=(True, "ok")),
        patch.object(loop._gate, "route", return_value=decision),
    ):
        result = loop.run_cycle()

    assert result is not None
    assert result.accepted is False
    assert "Deferred for human review" in result.reason

    deferred_path = state_dir / "evolution" / "deferred.jsonl"
    assert deferred_path.exists()
    data = json.loads(deferred_path.read_text().strip())
    assert data["proposal_id"] == proposal.id


def test_run_cycle_sandbox_failure(tmp_path: Path) -> None:
    """Sandbox failure -> not accepted, record_sandbox_failure called."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)

    opportunity = _make_opportunity()
    proposal = _make_proposal()
    decision = _make_approval_decision(proposal_id=proposal.id)
    failed_sandbox = _make_sandbox_result(
        proposal_id=proposal.id,
        passed=False,
        candidate_score=0.6,
        delta=-0.4,
    )

    breaker_mock = MagicMock()
    breaker_mock.can_evolve.return_value = (True, "ok")

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(
            loop._detector, "identify_opportunities", return_value=[opportunity],
        ),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(
            loop._proposal_generator, "create_proposal", return_value=proposal,
        ),
        patch.object(loop._gate, "route", return_value=decision),
        patch.object(loop._sandbox, "validate", return_value=failed_sandbox),
        patch.object(
            loop, "_breaker", breaker_mock,
        ),
    ):
        result = loop.run_cycle()

    assert result is not None
    assert result.accepted is False
    assert "Sandbox failed" in result.reason
    breaker_mock.record_sandbox_failure.assert_called_once_with(proposal.id)


def test_run_stops_at_max_proposals(tmp_path: Path) -> None:
    """run() stops after generating max_proposals proposals."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(
        state_dir, repo_root=tmp_path, cycle_seconds=0, max_proposals=2,
    )

    call_count = 0

    def fake_run_cycle() -> ExperimentResult:
        nonlocal call_count
        call_count += 1
        loop._proposals_generated += 1
        return ExperimentResult(
            proposal_id=f"P-{call_count}",
            title="test",
            risk_level="config",
            baseline_score=1.0,
            candidate_score=1.0,
            delta=0.0,
            accepted=True,
            reason="ok",
        )

    with patch.object(loop, "run_cycle", side_effect=fake_run_cycle):
        results = loop.run(window_seconds=60, max_proposals=2)

    assert len(results) == 2
    assert call_count == 2


def test_stop_halts_loop(tmp_path: Path) -> None:
    """Calling stop() causes run() to exit promptly."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(
        state_dir, repo_root=tmp_path, cycle_seconds=0,
    )

    def slow_cycle() -> None:
        time.sleep(0.05)
        return None

    with patch.object(loop, "run_cycle", side_effect=slow_cycle):
        t = threading.Thread(
            target=loop.run, kwargs={"window_seconds": 300, "max_proposals": 100},
        )
        t.start()
        time.sleep(0.1)
        loop.stop()
        t.join(timeout=2.0)

    assert not t.is_alive()
    assert loop._running is False


def test_get_summary(tmp_path: Path) -> None:
    """get_summary() returns expected keys after some experiments."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)

    loop._start_time = time.time() - 60
    loop._proposals_generated = 3
    loop._proposals_accepted = 1
    loop._experiments = [
        ExperimentResult(
            proposal_id="P-1",
            title="a",
            risk_level="config",
            baseline_score=1.0,
            candidate_score=1.0,
            delta=0.0,
            accepted=True,
            reason="ok",
            cost_usd=0.05,
        ),
    ]
    loop._running = True

    summary = loop.get_summary()

    assert summary["experiments_run"] == 1
    assert summary["proposals_generated"] == 3
    assert summary["proposals_accepted"] == 1
    assert summary["acceptance_rate"] == pytest.approx(1 / 3)
    assert summary["elapsed_seconds"] > 0
    assert summary["experiments_per_hour"] > 0
    assert summary["total_cost_usd"] == 0.05
    assert summary["running"] is True


def test_acceptance_rate_zero_generated(tmp_path: Path) -> None:
    """acceptance_rate is 0.0 when no proposals have been generated."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)

    assert loop.acceptance_rate == 0.0


def test_acceptance_rate(tmp_path: Path) -> None:
    """acceptance_rate reflects generated vs accepted."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)
    loop._proposals_generated = 4
    loop._proposals_accepted = 1

    assert loop.acceptance_rate == pytest.approx(0.25)


def test_infer_risk_level() -> None:
    """_infer_risk_level maps proposal risk strings to RiskLevel enums."""
    cases = {
        "low": RiskLevel.L0_CONFIG,
        "medium": RiskLevel.L1_TEMPLATE,
        "high": RiskLevel.L2_LOGIC,
        "critical": RiskLevel.L3_STRUCTURAL,
    }

    for level_str, expected_risk in cases.items():
        proposal = _make_proposal(risk_level=level_str)
        assert EvolutionLoop._infer_risk_level(proposal) == expected_risk

    # Unknown falls back to L2_LOGIC
    proposal = _make_proposal(risk_level="unknown")
    assert EvolutionLoop._infer_risk_level(proposal) == RiskLevel.L2_LOGIC


def test_log_experiment_creates_file(tmp_path: Path) -> None:
    """Running a cycle writes a valid JSON line to experiments.jsonl."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)

    opportunity = _make_opportunity()
    proposal = _make_proposal()
    decision = _make_approval_decision(proposal_id=proposal.id)
    sandbox_result = _make_sandbox_result(proposal_id=proposal.id)

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(
            loop._detector, "identify_opportunities", return_value=[opportunity],
        ),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(
            loop._proposal_generator, "create_proposal", return_value=proposal,
        ),
        patch.object(loop._breaker, "can_evolve", return_value=(True, "ok")),
        patch.object(loop._gate, "route", return_value=decision),
        patch.object(loop._sandbox, "validate", return_value=sandbox_result),
        patch.object(loop._executor, "execute_upgrade", return_value=True),
        patch.object(loop._breaker, "record_change"),
    ):
        loop.run_cycle()

    experiments_path = state_dir / "evolution" / "experiments.jsonl"
    assert experiments_path.exists()

    lines = experiments_path.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert "proposal_id" in data
    assert "accepted" in data


def test_run_baseline_no_benchmarks(tmp_path: Path) -> None:
    """_run_baseline returns 1.0 when tests/benchmarks/ does not exist."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)

    # Ensure no benchmarks directory exists
    assert not (tmp_path / "tests" / "benchmarks").exists()

    score = loop._run_baseline()

    assert score == 1.0


def test_generate_proposal_filters_high_risk(tmp_path: Path) -> None:
    """_generate_proposal returns None for only high-risk opportunities."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)

    high_risk_opps = [
        _make_opportunity(risk_level="high", confidence=0.95),
        _make_opportunity(risk_level="high", confidence=0.80),
    ]

    result = loop._generate_proposal(high_risk_opps)

    assert result is None
