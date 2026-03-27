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
    UpgradeProposal,
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
            loop._detector,
            "identify_opportunities",
            return_value=[opportunity],
        ),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(
            loop._proposal_generator,
            "create_proposal",
            return_value=proposal,
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
            loop._detector,
            "identify_opportunities",
            return_value=[opportunity],
        ),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(
            loop._proposal_generator,
            "create_proposal",
            return_value=proposal,
        ),
        patch.object(
            loop._breaker,
            "can_evolve",
            return_value=(False, "blocked"),
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
            loop._detector,
            "identify_opportunities",
            return_value=[opportunity],
        ),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(
            loop._proposal_generator,
            "create_proposal",
            return_value=proposal,
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
    proposal = _make_proposal(risk_level="code")  # code-level changes require sandbox
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
            loop._detector,
            "identify_opportunities",
            return_value=[opportunity],
        ),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(
            loop._proposal_generator,
            "create_proposal",
            return_value=proposal,
        ),
        patch.object(loop._gate, "route", return_value=decision),
        patch.object(loop._sandbox, "validate", return_value=failed_sandbox),
        patch.object(
            loop,
            "_breaker",
            breaker_mock,
        ),
        # Force standard risk route so sandbox is not bypassed
        patch.object(loop, "_classify_risk_route", return_value="standard"),
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
        state_dir,
        repo_root=tmp_path,
        cycle_seconds=0,
        max_proposals=2,
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
        state_dir,
        repo_root=tmp_path,
        cycle_seconds=0,
    )

    def slow_cycle() -> None:
        time.sleep(0.05)
        return None

    with patch.object(loop, "run_cycle", side_effect=slow_cycle):
        t = threading.Thread(
            target=loop.run,
            kwargs={"window_seconds": 300, "max_proposals": 100},
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
            loop._detector,
            "identify_opportunities",
            return_value=[opportunity],
        ),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(
            loop._proposal_generator,
            "create_proposal",
            return_value=proposal,
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


# ---------------------------------------------------------------------------
# Proposal generation failure tests
# ---------------------------------------------------------------------------


def test_run_cycle_proposal_generation_llm_timeout(tmp_path: Path) -> None:
    """RuntimeError (LLM timeout) from create_proposal propagates out of run_cycle."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)
    opportunity = _make_opportunity()

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(
            loop._proposal_generator,
            "create_proposal",
            side_effect=RuntimeError("LLM request timed out after 30s"),
        ),
        pytest.raises(RuntimeError, match="LLM request timed out"),
    ):
        loop.run_cycle()


def test_run_cycle_proposal_generation_malformed_response(tmp_path: Path) -> None:
    """ValueError (malformed LLM response) from create_proposal propagates out of run_cycle."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)
    opportunity = _make_opportunity()

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(
            loop._proposal_generator,
            "create_proposal",
            side_effect=ValueError("Malformed LLM response: missing 'title' field"),
        ),
        pytest.raises(ValueError, match="Malformed LLM response"),
    ):
        loop.run_cycle()


def test_run_logs_and_continues_after_run_cycle_exception(tmp_path: Path) -> None:
    """run() catches exceptions from run_cycle, logs them, and returns empty results."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path, cycle_seconds=0, max_proposals=2)

    call_count = 0

    def flaky_cycle() -> ExperimentResult:
        nonlocal call_count
        call_count += 1
        loop._proposals_generated += 1
        if call_count == 1:
            raise RuntimeError("LLM timeout")
        return ExperimentResult(
            proposal_id="P-2",
            title="ok",
            risk_level="config",
            baseline_score=1.0,
            candidate_score=1.0,
            delta=0.0,
            accepted=True,
            reason="ok",
        )

    with patch.object(loop, "run_cycle", side_effect=flaky_cycle):
        results = loop.run(window_seconds=60, max_proposals=2)

    # First call raised, second succeeded — only the second result is in the list
    assert len(results) == 1
    assert results[0].proposal_id == "P-2"
    assert loop._running is False


# ---------------------------------------------------------------------------
# Sandbox validation failure mid-pipeline
# ---------------------------------------------------------------------------


def test_run_cycle_sandbox_raises_worktree_error(tmp_path: Path) -> None:
    """RuntimeError from sandbox.validate (worktree creation failure) propagates."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)
    opportunity = _make_opportunity()
    proposal = _make_proposal()
    decision = _make_approval_decision(proposal_id=proposal.id)

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._breaker, "can_evolve", return_value=(True, "ok")),
        patch.object(loop._gate, "route", return_value=decision),
        patch.object(loop, "_classify_risk_route", return_value="standard"),
        patch.object(
            loop._sandbox,
            "validate",
            side_effect=RuntimeError("git worktree add failed: branch already exists"),
        ),
        pytest.raises(RuntimeError, match="git worktree add failed"),
    ):
        loop.run_cycle()


def test_run_cycle_sandbox_raises_test_crash(tmp_path: Path) -> None:
    """RuntimeError from sandbox.validate (test process crash/timeout) propagates."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)
    opportunity = _make_opportunity()
    proposal = _make_proposal()
    decision = _make_approval_decision(proposal_id=proposal.id)

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._breaker, "can_evolve", return_value=(True, "ok")),
        patch.object(loop._gate, "route", return_value=decision),
        patch.object(loop, "_classify_risk_route", return_value="standard"),
        patch.object(
            loop._sandbox,
            "validate",
            side_effect=RuntimeError("Tests timed out after 300s"),
        ),
        pytest.raises(RuntimeError, match="Tests timed out"),
    ):
        loop.run_cycle()


# ---------------------------------------------------------------------------
# Approval gate edge cases
# ---------------------------------------------------------------------------


def test_run_cycle_auto_approved_audit_still_applied(tmp_path: Path) -> None:
    """AUTO_APPROVED_AUDIT outcome proceeds to sandbox and applies if tests pass."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)
    opportunity = _make_opportunity(confidence=0.90)
    proposal = _make_proposal(confidence=0.90)
    sandbox_result = _make_sandbox_result(proposal_id=proposal.id)
    decision = ApprovalDecision(
        proposal_id=proposal.id,
        risk_level=RiskLevel.L0_CONFIG,
        confidence=0.90,
        outcome=ApprovalOutcome.AUTO_APPROVED_AUDIT,
        reason="auto with async audit",
        requires_human=False,
    )

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._breaker, "can_evolve", return_value=(True, "ok")),
        patch.object(loop._gate, "route", return_value=decision),
        patch.object(loop._sandbox, "validate", return_value=sandbox_result),
        patch.object(loop._executor, "execute_upgrade", return_value=True),
        patch.object(loop._breaker, "record_change"),
    ):
        result = loop.run_cycle()

    assert result is not None
    assert result.accepted is True


def test_run_cycle_confidence_exactly_at_auto_threshold(tmp_path: Path) -> None:
    """Confidence exactly at 0.95 results in AUTO_APPROVED and proposal is applied."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)
    opportunity = _make_opportunity(confidence=0.95)
    proposal = _make_proposal(confidence=0.95)
    sandbox_result = _make_sandbox_result(proposal_id=proposal.id)
    decision = ApprovalDecision(
        proposal_id=proposal.id,
        risk_level=RiskLevel.L0_CONFIG,
        confidence=0.95,
        outcome=ApprovalOutcome.AUTO_APPROVED,
        reason="confidence exactly at threshold",
        requires_human=False,
    )

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._breaker, "can_evolve", return_value=(True, "ok")),
        patch.object(loop._gate, "route", return_value=decision),
        patch.object(loop._sandbox, "validate", return_value=sandbox_result),
        patch.object(loop._executor, "execute_upgrade", return_value=True),
        patch.object(loop._breaker, "record_change"),
    ):
        result = loop.run_cycle()

    assert result is not None
    assert result.accepted is True


def test_run_cycle_blocked_outcome_skips_sandbox(tmp_path: Path) -> None:
    """BLOCKED gate outcome defers proposal without calling sandbox.validate."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)
    opportunity = _make_opportunity(confidence=0.50)
    proposal = _make_proposal(confidence=0.50)
    decision = ApprovalDecision(
        proposal_id=proposal.id,
        risk_level=RiskLevel.L2_LOGIC,
        confidence=0.50,
        outcome=ApprovalOutcome.BLOCKED,
        reason="confidence too low for automated loop",
        requires_human=True,
    )
    sandbox_mock = MagicMock()

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._breaker, "can_evolve", return_value=(True, "ok")),
        patch.object(loop._gate, "route", return_value=decision),
        patch.object(loop, "_sandbox", sandbox_mock),
    ):
        result = loop.run_cycle()

    assert result is not None
    assert result.accepted is False
    assert "Deferred" in result.reason
    sandbox_mock.validate.assert_not_called()


def test_run_cycle_selects_highest_confidence_opportunity(tmp_path: Path) -> None:
    """With multiple eligible opportunities, the highest-confidence one is chosen."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)

    low_conf = _make_opportunity(confidence=0.60)
    high_conf = _make_opportunity(confidence=0.92)
    med_conf = _make_opportunity(confidence=0.75)

    selected_opportunity: list[ImprovementOpportunity] = []

    def capture_create_proposal(opp: ImprovementOpportunity, trigger: object) -> UpgradeProposal:
        selected_opportunity.append(opp)
        return _make_proposal()

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(
            loop._detector,
            "identify_opportunities",
            return_value=[low_conf, high_conf, med_conf],
        ),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", side_effect=capture_create_proposal),
        patch.object(loop._breaker, "can_evolve", return_value=(False, "blocked")),
    ):
        loop.run_cycle()

    assert len(selected_opportunity) == 1
    assert selected_opportunity[0].confidence == 0.92


# ---------------------------------------------------------------------------
# Circuit breaker tripping during evolution cycle
# ---------------------------------------------------------------------------


def test_circuit_breaker_trips_after_sandbox_failure_blocks_next_cycle(
    tmp_path: Path,
) -> None:
    """After sandbox failure, breaker records it; next cycle sees breaker open."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)
    opportunity = _make_opportunity()
    proposal = _make_proposal()
    decision = _make_approval_decision(proposal_id=proposal.id)
    failed_sandbox = _make_sandbox_result(
        proposal_id=proposal.id,
        passed=False,
        candidate_score=0.3,
        delta=-0.7,
    )

    breaker_mock = MagicMock()
    breaker_mock.can_evolve.return_value = (True, "ok")

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._gate, "route", return_value=decision),
        patch.object(loop, "_classify_risk_route", return_value="standard"),
        patch.object(loop._sandbox, "validate", return_value=failed_sandbox),
        patch.object(loop, "_breaker", breaker_mock),
    ):
        result1 = loop.run_cycle()

    assert result1 is not None
    assert result1.accepted is False
    assert "Sandbox failed" in result1.reason
    breaker_mock.record_sandbox_failure.assert_called_once_with(proposal.id)

    # Simulate breaker now open after recording failure
    breaker_mock.can_evolve.return_value = (False, "too many sandbox failures")

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop, "_breaker", breaker_mock),
    ):
        result2 = loop.run_cycle()

    assert result2 is not None
    assert result2.accepted is False
    assert "Circuit breaker" in result2.reason


def test_proposals_generated_increments_even_when_breaker_blocks(tmp_path: Path) -> None:
    """_proposals_generated counter increments even if circuit breaker blocks execution."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)
    opportunity = _make_opportunity()
    proposal = _make_proposal()

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._breaker, "can_evolve", return_value=(False, "open")),
    ):
        loop.run_cycle()

    assert loop._proposals_generated == 1
    assert loop._proposals_accepted == 0


# ---------------------------------------------------------------------------
# State transitions and recovery after partial failure
# ---------------------------------------------------------------------------


def test_run_cycle_application_fails_triggers_rollback(tmp_path: Path) -> None:
    """When executor.execute_upgrade returns False, rollback is called and accepted=False."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)
    opportunity = _make_opportunity()
    proposal = _make_proposal()
    sandbox_result = _make_sandbox_result(proposal_id=proposal.id)
    decision = _make_approval_decision(proposal_id=proposal.id)

    executor_mock = MagicMock()
    executor_mock.execute_upgrade.return_value = False
    breaker_mock = MagicMock()
    breaker_mock.can_evolve.return_value = (True, "ok")

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._gate, "route", return_value=decision),
        patch.object(loop._sandbox, "validate", return_value=sandbox_result),
        patch.object(loop, "_executor", executor_mock),
        patch.object(loop, "_breaker", breaker_mock),
    ):
        result = loop.run_cycle()

    assert result is not None
    assert result.accepted is False
    assert "Application failed" in result.reason
    executor_mock.execute_upgrade.assert_called_once_with(proposal)
    executor_mock.rollback_upgrade.assert_called_once_with(proposal)
    breaker_mock.record_rollback.assert_called_once_with(proposal.id)
    # proposals_accepted should NOT have incremented
    assert loop._proposals_accepted == 0


def test_run_cycle_partial_failure_proposal_applied_but_failed(tmp_path: Path) -> None:
    """Sandbox passes but executor fails: result is not accepted, rollback runs, log written."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)
    opportunity = _make_opportunity()
    proposal = _make_proposal()
    # Sandbox shows improvement
    sandbox_result = _make_sandbox_result(
        proposal_id=proposal.id,
        passed=True,
        candidate_score=1.1,
        delta=0.1,
    )
    decision = _make_approval_decision(proposal_id=proposal.id)

    executor_mock = MagicMock()
    executor_mock.execute_upgrade.return_value = False
    breaker_mock = MagicMock()
    breaker_mock.can_evolve.return_value = (True, "ok")

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._gate, "route", return_value=decision),
        patch.object(loop._sandbox, "validate", return_value=sandbox_result),
        patch.object(loop, "_executor", executor_mock),
        patch.object(loop, "_breaker", breaker_mock),
    ):
        result = loop.run_cycle()

    # Despite sandbox showing improvement, application failed → not accepted
    assert result is not None
    assert result.accepted is False
    assert result.candidate_score == 1.0  # falls back to baseline on failure
    assert result.delta == 0.0

    # Rollback called to recover
    executor_mock.rollback_upgrade.assert_called_once_with(proposal)
    breaker_mock.record_rollback.assert_called_once_with(proposal.id)

    # Experiment still logged to JSONL
    experiments_path = state_dir / "evolution" / "experiments.jsonl"
    assert experiments_path.exists()
    data = json.loads(experiments_path.read_text().strip())
    assert data["accepted"] is False


def test_run_cycle_full_state_transition_pending_to_applied(tmp_path: Path) -> None:
    """Full state transition: pending → gate_approved → sandbox_passed → applied → logged."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    loop = EvolutionLoop(state_dir, repo_root=tmp_path)

    opportunity = _make_opportunity()
    proposal = _make_proposal()
    sandbox_result = _make_sandbox_result(
        proposal_id=proposal.id,
        passed=True,
        candidate_score=1.05,
        delta=0.05,
    )
    decision = _make_approval_decision(proposal_id=proposal.id)

    executor_mock = MagicMock()
    executor_mock.execute_upgrade.return_value = True
    breaker_mock = MagicMock()
    breaker_mock.can_evolve.return_value = (True, "ok")

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._gate, "route", return_value=decision),
        patch.object(loop, "_classify_risk_route", return_value="standard"),
        patch.object(loop._sandbox, "validate", return_value=sandbox_result),
        patch.object(loop, "_executor", executor_mock),
        patch.object(loop, "_breaker", breaker_mock),
    ):
        result = loop.run_cycle()

    # Proposal accepted
    assert result is not None
    assert result.accepted is True
    assert result.candidate_score == pytest.approx(1.05)
    assert result.delta == pytest.approx(0.05)
    assert loop._proposals_generated == 1
    assert loop._proposals_accepted == 1

    # Gate routed the proposal
    breaker_mock.can_evolve.assert_called_once()

    # Executor applied it
    executor_mock.execute_upgrade.assert_called_once_with(proposal)
    executor_mock.rollback_upgrade.assert_not_called()

    # Breaker recorded the change, not a rollback
    breaker_mock.record_change.assert_called_once()
    breaker_mock.record_rollback.assert_not_called()

    # Result logged to JSONL
    experiments_path = state_dir / "evolution" / "experiments.jsonl"
    assert experiments_path.exists()
    data = json.loads(experiments_path.read_text().strip())
    assert data["accepted"] is True
    assert data["delta"] == pytest.approx(0.05)
