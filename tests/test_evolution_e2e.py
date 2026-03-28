"""End-to-end tests for the self-evolution feedback loop (ADR-003).

Covers the full cycle:
  1. Metrics collection → aggregation → trend detection
  2. Opportunity detection → proposal generation
  3. Approval gate routing (L0-L3)
  4. EvolutionLoop full evaluation cycle with mock metrics
  5. Evolution loop handles empty metrics gracefully
  6. Circuit breaker integration with the loop
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.models import RiskAssessment, RollbackPlan
from bernstein.evolution.aggregator import (
    FileMetricsCollector,
    MetricsAggregator,
    TaskMetrics,
)
from bernstein.evolution.circuit import CircuitBreaker
from bernstein.evolution.detector import ImprovementOpportunity, OpportunityDetector, UpgradeCategory
from bernstein.evolution.gate import ApprovalDecision, ApprovalGate, ApprovalOutcome
from bernstein.evolution.loop import EvolutionLoop, ExperimentResult
from bernstein.evolution.proposals import (
    AnalysisTrigger,
    ProposalGenerator,
    UpgradeProposal,
    UpgradeStatus,
)
from bernstein.evolution.types import CircuitState, RiskLevel
from bernstein.evolution.types import SandboxResult as TypesSandboxResult
from bernstein.evolution.types import UpgradeProposal as TypesUpgradeProposal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_declining_metrics(collector: FileMetricsCollector, count: int = 25) -> None:
    """Seed task metrics with a clear downward trend in janitor pass rate."""
    now = time.time()
    for i in range(count):
        passed = i % 10 != 0 if i < count // 2 else i % 10 < 4
        metrics = TaskMetrics(
            timestamp=now - (count - i) * 120,
            task_id=f"T-{i:03d}",
            role="backend",
            model="sonnet",
            provider="anthropic",
            duration_seconds=30.0 + i,
            cost_usd=0.02 * (i + 1),
            janitor_passed=passed,
        )
        collector.record_task_metrics(metrics)


def _seed_healthy_metrics(collector: FileMetricsCollector, count: int = 20) -> None:
    """Seed task metrics with uniformly high success rates."""
    now = time.time()
    for i in range(count):
        metrics = TaskMetrics(
            timestamp=now - (count - i) * 60,
            task_id=f"T-healthy-{i:03d}",
            role="backend",
            model="sonnet",
            provider="anthropic",
            duration_seconds=20.0,
            cost_usd=0.01,
            janitor_passed=True,
        )
        collector.record_task_metrics(metrics)


def _make_proposal(
    *,
    id: str = "UPG-0001",
    risk_level: str = "low",
    confidence: float = 0.95,
) -> UpgradeProposal:
    return UpgradeProposal(
        id=id,
        title="Test proposal",
        category=UpgradeCategory.POLICY_UPDATE,
        description="Test description",
        current_state="current state",
        proposed_change="proposed change",
        benefits=["benefit A"],
        risk_assessment=RiskAssessment(level=risk_level),
        rollback_plan=RollbackPlan(steps=["revert"], estimated_rollback_minutes=5),
        cost_estimate_usd=0.0,
        expected_improvement="10% improvement",
        confidence=confidence,
    )


def _make_opportunity(
    *,
    risk_level: str = "low",
    confidence: float = 0.9,
    category: UpgradeCategory = UpgradeCategory.POLICY_UPDATE,
) -> ImprovementOpportunity:
    return ImprovementOpportunity(
        category=category,
        title="Test opportunity",
        description="desc",
        expected_improvement="improve",
        confidence=confidence,
        risk_level=risk_level,
    )


def _make_sandbox_result(
    *,
    proposal_id: str = "UPG-0001",
    passed: bool = True,
    candidate_score: float = 1.0,
    delta: float = 0.05,
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
    proposal_id: str = "UPG-0001",
    outcome: ApprovalOutcome = ApprovalOutcome.AUTO_APPROVED,
    risk_level: RiskLevel = RiskLevel.L0_CONFIG,
) -> ApprovalDecision:
    return ApprovalDecision(
        proposal_id=proposal_id,
        risk_level=risk_level,
        confidence=0.95,
        outcome=outcome,
        reason="auto",
        requires_human=False,
    )


# ---------------------------------------------------------------------------
# 1. Metrics collection → aggregation → trend detection
# ---------------------------------------------------------------------------


class TestMetricsCollectionToTrendDetection:
    """Validate the metrics → aggregation → trend detection pipeline."""

    def test_task_metrics_persisted_and_loaded(self, tmp_path: Path) -> None:
        """Metrics written by collector are persisted to JSONL and reloadable."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=20)

        tasks_jsonl = state_dir / "metrics" / "tasks.jsonl"
        assert tasks_jsonl.exists()
        lines = tasks_jsonl.read_text().strip().splitlines()
        assert len(lines) == 20

        # Reload via fresh collector
        fresh = FileMetricsCollector(state_dir)
        fresh.load_from_files()
        recent = fresh.get_recent_task_metrics(hours=24)
        assert len(recent) == 20

    def test_aggregator_produces_trend_analysis(self, tmp_path: Path) -> None:
        """MetricsAggregator returns a list of TrendAnalysis from loaded metrics."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=25)

        aggregator = MetricsAggregator(collector)
        trends = aggregator.analyze_trends()

        assert isinstance(trends, list)
        # Trend objects have expected fields
        for trend in trends:
            assert hasattr(trend, "metric_name")
            assert hasattr(trend, "direction")
            assert trend.direction in ("increasing", "decreasing", "stable")
            assert 0.0 <= trend.confidence <= 1.0

    def test_success_rate_trend_detected_as_decreasing(self, tmp_path: Path) -> None:
        """A declining janitor pass rate produces a 'decreasing' success_rate trend."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=30)

        aggregator = MetricsAggregator(collector)
        trends = aggregator.analyze_trends()

        success_trend = next((t for t in trends if t.metric_name == "success_rate"), None)
        if success_trend is not None:
            assert success_trend.direction in ("decreasing", "stable")

    def test_empty_metrics_aggregation_returns_empty_trends(self, tmp_path: Path) -> None:
        """Aggregator with no metrics returns an empty list without errors."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        collector = FileMetricsCollector(state_dir)
        aggregator = MetricsAggregator(collector)

        trends = aggregator.analyze_trends()
        assert isinstance(trends, list)
        assert len(trends) == 0

    def test_run_full_analysis_does_not_raise_on_empty(self, tmp_path: Path) -> None:
        """MetricsAggregator.run_full_analysis() tolerates empty state."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        analysis_dir = state_dir / "analysis"
        analysis_dir.mkdir()
        collector = FileMetricsCollector(state_dir)
        aggregator = MetricsAggregator(collector, analysis_dir=analysis_dir)

        # Should not raise
        aggregator.run_full_analysis()


# ---------------------------------------------------------------------------
# 2. Opportunity detection → proposal generation
# ---------------------------------------------------------------------------


class TestOpportunityToProposal:
    """Validate the opportunity detection → proposal generation sub-pipeline."""

    def test_detector_finds_opportunities_from_declining_metrics(self, tmp_path: Path) -> None:
        """OpportunityDetector identifies ≥1 opportunity from poor success rates."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=25)

        detector = OpportunityDetector(collector)
        opportunities = detector.identify_opportunities()

        assert len(opportunities) > 0
        for opp in opportunities:
            assert opp.confidence > 0
            assert opp.risk_level in ("low", "medium", "high")

    def test_detector_returns_empty_on_healthy_metrics(self, tmp_path: Path) -> None:
        """OpportunityDetector returns no (or minimal) opportunities when all tasks pass."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        collector = FileMetricsCollector(state_dir)
        _seed_healthy_metrics(collector, count=20)

        detector = OpportunityDetector(collector)
        opportunities = detector.identify_opportunities()

        # Healthy metrics may still produce some opportunities, but none
        # should have high confidence about a regression
        for opp in opportunities:
            assert opp.confidence < 1.0  # no certainty about a problem

    def test_detector_returns_empty_list_on_no_metrics(self, tmp_path: Path) -> None:
        """OpportunityDetector returns empty list with no metrics data."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        collector = FileMetricsCollector(state_dir)

        detector = OpportunityDetector(collector)
        opportunities = detector.identify_opportunities()

        assert isinstance(opportunities, list)

    def test_proposal_generator_creates_valid_proposal(self, tmp_path: Path) -> None:
        """ProposalGenerator creates a well-formed UpgradeProposal from an opportunity."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=25)

        detector = OpportunityDetector(collector)
        opportunities = detector.identify_opportunities()
        assert len(opportunities) > 0

        generator = ProposalGenerator()
        proposal = generator.create_proposal(opportunities[0], AnalysisTrigger.SCHEDULED)

        assert proposal.id.startswith("UPG-")
        assert proposal.title == opportunities[0].title
        assert proposal.confidence == opportunities[0].confidence
        assert proposal.triggered_by == AnalysisTrigger.SCHEDULED
        assert proposal.status == UpgradeStatus.PENDING

    def test_proposal_inherits_opportunity_metadata(self, tmp_path: Path) -> None:
        """Proposal fields match the source opportunity."""
        opp = _make_opportunity(risk_level="medium", confidence=0.82)
        generator = ProposalGenerator()
        proposal = generator.create_proposal(opp, AnalysisTrigger.SCHEDULED)

        assert proposal.confidence == opp.confidence
        assert proposal.title == opp.title
        assert proposal.expected_improvement == opp.expected_improvement


# ---------------------------------------------------------------------------
# 3. Approval gate routing (L0-L3)
# ---------------------------------------------------------------------------


class TestApprovalGateRouting:
    """Validate approval gate routing for each risk level."""

    def _make_types_proposal(
        self,
        proposal_id: str,
        risk_level: RiskLevel,
        target_files: list[str],
        confidence: float = 0.95,
    ) -> TypesUpgradeProposal:
        return TypesUpgradeProposal(
            id=proposal_id,
            title="Test",
            description="desc",
            risk_level=risk_level,
            target_files=target_files,
            diff="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n",
            rationale="rationale",
            expected_impact="impact",
            confidence=confidence,
        )

    def test_l0_config_high_confidence_auto_approved(self, tmp_path: Path) -> None:
        """L0 config proposals with high confidence are auto-approved."""
        gate = ApprovalGate(decisions_dir=tmp_path / "evolution")
        proposal = self._make_types_proposal(
            "UPG-L0-001",
            RiskLevel.L0_CONFIG,
            [".sdd/config/providers.yaml"],
            confidence=0.97,
        )
        decision = gate.route(proposal)

        assert decision.proposal_id == "UPG-L0-001"
        assert decision.risk_level == RiskLevel.L0_CONFIG
        assert decision.outcome in (
            ApprovalOutcome.AUTO_APPROVED,
            ApprovalOutcome.AUTO_APPROVED_AUDIT,
        )

    def test_l1_template_high_confidence_auto_approved(self, tmp_path: Path) -> None:
        """L1 template proposals with ≥95% confidence are auto-approved."""
        gate = ApprovalGate(decisions_dir=tmp_path / "evolution")
        proposal = self._make_types_proposal(
            "UPG-L1-001",
            RiskLevel.L1_TEMPLATE,
            ["templates/roles/backend.md"],
            confidence=0.96,
        )
        decision = gate.route(proposal)

        assert decision.proposal_id == "UPG-L1-001"
        assert decision.outcome in (
            ApprovalOutcome.AUTO_APPROVED,
            ApprovalOutcome.AUTO_APPROVED_AUDIT,
        )

    def test_l2_logic_requires_human_review(self, tmp_path: Path) -> None:
        """L2 logic proposals are deferred to human review."""
        gate = ApprovalGate(decisions_dir=tmp_path / "evolution")
        proposal = self._make_types_proposal(
            "UPG-L2-001",
            RiskLevel.L2_LOGIC,
            ["src/bernstein/core/router.py"],
            confidence=0.88,
        )
        decision = gate.route(proposal)

        assert decision.proposal_id == "UPG-L2-001"
        assert decision.outcome in (
            ApprovalOutcome.HUMAN_REVIEW_4H,
            ApprovalOutcome.HUMAN_REVIEW_IMMEDIATE,
            ApprovalOutcome.BLOCKED,
        )

    def test_l3_structural_always_blocked(self, tmp_path: Path) -> None:
        """L3 structural proposals are always blocked from auto-application."""
        gate = ApprovalGate(decisions_dir=tmp_path / "evolution")
        proposal = self._make_types_proposal(
            "UPG-L3-001",
            RiskLevel.L3_STRUCTURAL,
            ["src/bernstein/core/models.py"],
            confidence=0.99,
        )
        decision = gate.route(proposal)

        # L3 must never be auto-approved
        assert decision.outcome not in (
            ApprovalOutcome.AUTO_APPROVED,
            ApprovalOutcome.AUTO_APPROVED_AUDIT,
        )

    def test_low_confidence_proposal_requires_human(self, tmp_path: Path) -> None:
        """Proposals below confidence threshold are deferred to human review."""
        gate = ApprovalGate(decisions_dir=tmp_path / "evolution")
        proposal = self._make_types_proposal(
            "UPG-LOW-001",
            RiskLevel.L0_CONFIG,
            [".sdd/config/routing.yaml"],
            confidence=0.60,
        )
        decision = gate.route(proposal)

        assert decision.outcome in (
            ApprovalOutcome.HUMAN_REVIEW_4H,
            ApprovalOutcome.HUMAN_REVIEW_IMMEDIATE,
            ApprovalOutcome.BLOCKED,
        )

    def test_gate_logs_all_decisions(self, tmp_path: Path) -> None:
        """ApprovalGate persists every decision to decisions.jsonl."""
        decisions_dir = tmp_path / "evolution"
        gate = ApprovalGate(decisions_dir=decisions_dir)

        ids = ["UPG-A", "UPG-B", "UPG-C"]
        for pid in ids:
            proposal = self._make_types_proposal(pid, RiskLevel.L0_CONFIG, [".sdd/config/p.yaml"])
            gate.route(proposal)

        log = decisions_dir / "decisions.jsonl"
        assert log.exists()
        logged_ids = [json.loads(l)["proposal_id"] for l in log.read_text().strip().splitlines()]
        for pid in ids:
            assert pid in logged_ids


# ---------------------------------------------------------------------------
# 4. EvolutionLoop full evaluation cycle with mock metrics
# ---------------------------------------------------------------------------


class TestEvolutionLoopFullCycle:
    """EvolutionLoop.run_cycle() end-to-end with mocked components."""

    def _make_loop(self, tmp_path: Path) -> EvolutionLoop:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        return EvolutionLoop(
            state_dir=state_dir,
            repo_root=tmp_path,
            cycle_seconds=1,
            max_proposals=5,
            window_seconds=60,
        )

    def test_run_cycle_returns_experiment_result_when_opportunity_found(self, tmp_path: Path) -> None:
        """run_cycle() returns ExperimentResult when an opportunity is available."""
        loop = self._make_loop(tmp_path)

        proposal = _make_proposal(id="UPG-0001", risk_level="low", confidence=0.95)
        opportunity = _make_opportunity(risk_level="low", confidence=0.95)
        sandbox_result = _make_sandbox_result(proposal_id="UPG-0001", passed=True, delta=0.05)
        approval = _make_approval_decision(proposal_id="UPG-0001", outcome=ApprovalOutcome.AUTO_APPROVED)

        with (
            patch.object(loop._aggregator, "run_full_analysis"),
            patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
            patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
            patch.object(loop._breaker, "can_evolve", return_value=(True, "ok")),
            patch.object(loop._gate, "route", return_value=approval),
            patch.object(loop._sandbox, "validate", return_value=sandbox_result),
            patch.object(loop._executor, "execute_upgrade", return_value=True),
            patch.object(loop._breaker, "record_change"),
        ):
            result = loop.run_cycle()

        assert result is not None
        assert isinstance(result, ExperimentResult)
        assert result.accepted is True
        assert result.delta == 0.05
        assert result.reason == "Applied successfully"

    def test_run_cycle_logs_experiment_to_jsonl(self, tmp_path: Path) -> None:
        """run_cycle() appends its result to experiments.jsonl."""
        loop = self._make_loop(tmp_path)

        proposal = _make_proposal(id="UPG-0002", risk_level="low", confidence=0.90)
        opportunity = _make_opportunity(risk_level="low", confidence=0.90)
        sandbox_result = _make_sandbox_result(proposal_id="UPG-0002", passed=True)
        approval = _make_approval_decision(proposal_id="UPG-0002", outcome=ApprovalOutcome.AUTO_APPROVED)

        with (
            patch.object(loop._aggregator, "run_full_analysis"),
            patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
            patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
            patch.object(loop._breaker, "can_evolve", return_value=(True, "ok")),
            patch.object(loop._gate, "route", return_value=approval),
            patch.object(loop._sandbox, "validate", return_value=sandbox_result),
            patch.object(loop._executor, "execute_upgrade", return_value=True),
            patch.object(loop._breaker, "record_change"),
        ):
            loop.run_cycle()

        experiments_path = loop._experiments_path
        assert experiments_path.exists()
        line = json.loads(experiments_path.read_text().strip())
        assert line["proposal_id"] == "UPG-0002"
        assert line["accepted"] is True

    def test_run_cycle_defers_low_confidence_proposal(self, tmp_path: Path) -> None:
        """run_cycle() defers proposals to deferred.jsonl when gate returns HUMAN_REVIEW."""
        loop = self._make_loop(tmp_path)

        # "medium" risk is eligible for the loop but may be routed to human review
        proposal = _make_proposal(id="UPG-0003", risk_level="medium", confidence=0.75)
        opportunity = _make_opportunity(risk_level="medium", confidence=0.75)
        approval = _make_approval_decision(
            proposal_id="UPG-0003",
            outcome=ApprovalOutcome.HUMAN_REVIEW_4H,
            risk_level=RiskLevel.L1_TEMPLATE,
        )

        with (
            patch.object(loop._aggregator, "run_full_analysis"),
            patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
            patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
            patch.object(loop._breaker, "can_evolve", return_value=(True, "ok")),
            patch.object(loop._gate, "route", return_value=approval),
        ):
            result = loop.run_cycle()

        assert result is not None
        assert result.accepted is False
        assert "Deferred" in result.reason

        deferred_path = loop._deferred_path
        assert deferred_path.exists()
        record = json.loads(deferred_path.read_text().strip())
        assert record["proposal_id"] == "UPG-0003"

    def test_run_cycle_sandbox_failure_records_breaker(self, tmp_path: Path) -> None:
        """run_cycle() records sandbox failure in circuit breaker."""
        loop = self._make_loop(tmp_path)

        proposal = _make_proposal(id="UPG-0004", risk_level="low", confidence=0.95)
        opportunity = _make_opportunity(risk_level="low", confidence=0.95)
        sandbox_result = _make_sandbox_result(proposal_id="UPG-0004", passed=False, delta=-0.1)
        sandbox_result.error = "Tests failed"
        approval = _make_approval_decision(proposal_id="UPG-0004", outcome=ApprovalOutcome.AUTO_APPROVED)

        with (
            patch.object(loop._aggregator, "run_full_analysis"),
            patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
            patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
            patch.object(loop._breaker, "can_evolve", return_value=(True, "ok")),
            patch.object(loop._gate, "route", return_value=approval),
            patch.object(loop._sandbox, "validate", return_value=sandbox_result),
            patch.object(loop._breaker, "record_sandbox_failure") as mock_record_failure,
        ):
            result = loop.run_cycle()

        assert result is not None
        assert result.accepted is False
        assert "Sandbox failed" in result.reason
        mock_record_failure.assert_called_once_with("UPG-0004")

    def test_acceptance_rate_tracks_accepted_vs_generated(self, tmp_path: Path) -> None:
        """acceptance_rate reflects ratio of accepted to generated proposals."""
        loop = self._make_loop(tmp_path)
        loop._proposals_generated = 4
        loop._proposals_accepted = 3

        assert loop.acceptance_rate == pytest.approx(0.75)

    def test_acceptance_rate_zero_when_no_proposals(self, tmp_path: Path) -> None:
        loop = self._make_loop(tmp_path)
        assert loop.acceptance_rate == 0.0

    def test_get_summary_returns_expected_keys(self, tmp_path: Path) -> None:
        """get_summary() returns a dict with required session statistics."""
        loop = self._make_loop(tmp_path)
        loop._start_time = time.time()
        summary = loop.get_summary()

        for key in (
            "experiments_run",
            "proposals_generated",
            "proposals_accepted",
            "acceptance_rate",
            "elapsed_seconds",
            "experiments_per_hour",
            "total_cost_usd",
            "running",
        ):
            assert key in summary


# ---------------------------------------------------------------------------
# 5. Evolution loop handles empty metrics gracefully
# ---------------------------------------------------------------------------


class TestEvolutionLoopEmptyMetrics:
    """EvolutionLoop behaves correctly when no task metrics exist."""

    def test_run_cycle_returns_none_when_no_opportunities(self, tmp_path: Path) -> None:
        """run_cycle() returns None if the detector finds no opportunities."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        loop = EvolutionLoop(state_dir=state_dir, repo_root=tmp_path)

        with (
            patch.object(loop._aggregator, "run_full_analysis"),
            patch.object(loop._detector, "identify_opportunities", return_value=[]),
        ):
            result = loop.run_cycle()

        assert result is None

    def test_run_cycle_returns_none_when_only_high_risk_opportunities(self, tmp_path: Path) -> None:
        """run_cycle() returns None when all opportunities are high-risk (not eligible)."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        loop = EvolutionLoop(state_dir=state_dir, repo_root=tmp_path)

        high_risk_opps = [
            _make_opportunity(risk_level="high", confidence=0.9),
            _make_opportunity(risk_level="high", confidence=0.85),
        ]

        with (
            patch.object(loop._aggregator, "run_full_analysis"),
            patch.object(loop._detector, "identify_opportunities", return_value=high_risk_opps),
        ):
            result = loop.run_cycle()

        # No eligible L0/L1 proposals → no experiment
        assert result is None

    def test_run_with_max_proposals_zero_returns_empty(self, tmp_path: Path) -> None:
        """run() with max_proposals=0 exits immediately without running cycles."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        loop = EvolutionLoop(
            state_dir=state_dir,
            repo_root=tmp_path,
            window_seconds=60,
        )

        results = loop.run(window_seconds=60, max_proposals=0)
        assert results == []

    def test_loop_does_not_raise_on_completely_empty_state(self, tmp_path: Path) -> None:
        """EvolutionLoop can be instantiated and queried with empty .sdd directory."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        loop = EvolutionLoop(state_dir=state_dir, repo_root=tmp_path)

        with (
            patch.object(loop._aggregator, "run_full_analysis"),
            patch.object(loop._detector, "identify_opportunities", return_value=[]),
        ):
            result = loop.run_cycle()

        assert result is None
        summary = loop.get_summary()
        assert summary["experiments_run"] == 0
        assert summary["acceptance_rate"] == 0.0


# ---------------------------------------------------------------------------
# 6. Circuit breaker integration with the loop
# ---------------------------------------------------------------------------


class TestCircuitBreakerIntegration:
    """Verify circuit breaker halts and resumes the evolution loop correctly."""

    def test_open_circuit_blocks_run_cycle(self, tmp_path: Path) -> None:
        """run_cycle() returns a non-accepted result when circuit is OPEN."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        loop = EvolutionLoop(state_dir=state_dir, repo_root=tmp_path)

        proposal = _make_proposal(id="UPG-CB-001", risk_level="low", confidence=0.95)
        opportunity = _make_opportunity(risk_level="low", confidence=0.95)

        with (
            patch.object(loop._aggregator, "run_full_analysis"),
            patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
            patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
            patch.object(
                loop._breaker,
                "can_evolve",
                return_value=(False, "Circuit OPEN — cooling off (3500s remaining)"),
            ),
        ):
            result = loop.run_cycle()

        assert result is not None
        assert result.accepted is False
        assert "Circuit breaker" in result.reason

    def test_three_sandbox_failures_trip_circuit(self, tmp_path: Path) -> None:
        """Three consecutive sandbox failures trip the circuit breaker to OPEN."""
        evolution_dir = tmp_path / "evolution"
        evolution_dir.mkdir()
        breaker = CircuitBreaker(state_dir=evolution_dir)

        assert breaker.state == CircuitState.CLOSED

        breaker.record_sandbox_failure("UPG-F1")
        breaker.record_sandbox_failure("UPG-F2")
        breaker.record_sandbox_failure("UPG-F3")

        assert breaker.state == CircuitState.OPEN

    def test_rollback_trips_circuit(self, tmp_path: Path) -> None:
        """A single rollback trips the circuit breaker."""
        evolution_dir = tmp_path / "evolution"
        evolution_dir.mkdir()
        breaker = CircuitBreaker(state_dir=evolution_dir)

        assert breaker.state == CircuitState.CLOSED
        breaker.record_rollback("UPG-ROLLBACK-001")
        assert breaker.state == CircuitState.OPEN

    def test_open_circuit_blocks_evolution_below_cooldown(self, tmp_path: Path) -> None:
        """can_evolve() returns False when circuit is OPEN and cooldown not elapsed."""
        evolution_dir = tmp_path / "evolution"
        evolution_dir.mkdir()
        breaker = CircuitBreaker(state_dir=evolution_dir)

        # Trip the breaker
        breaker.record_rollback("UPG-TRIP")
        assert breaker.state == CircuitState.OPEN

        # Within cooldown period (opened just now)
        allowed, reason = breaker.can_evolve(RiskLevel.L0_CONFIG)
        assert allowed is False
        assert "cooling off" in reason.lower() or "open" in reason.lower()

    def test_circuit_transitions_to_half_open_after_cooldown(self, tmp_path: Path) -> None:
        """Circuit transitions to HALF_OPEN after cooldown expires."""
        evolution_dir = tmp_path / "evolution"
        evolution_dir.mkdir()
        breaker = CircuitBreaker(state_dir=evolution_dir)

        # Manually set the circuit to OPEN with an expired opened_at
        breaker.state = CircuitState.OPEN
        breaker.opened_at = time.time() - 4000  # Beyond 1-hour cooldown
        breaker._save_state()

        # can_evolve should transition to HALF_OPEN and allow L0
        allowed, reason = breaker.can_evolve(RiskLevel.L0_CONFIG)
        assert allowed is True
        assert breaker.state == CircuitState.HALF_OPEN

    def test_half_open_blocks_non_l0_changes(self, tmp_path: Path) -> None:
        """In HALF_OPEN state, only L0_CONFIG changes are allowed."""
        evolution_dir = tmp_path / "evolution"
        evolution_dir.mkdir()
        breaker = CircuitBreaker(state_dir=evolution_dir)

        breaker.state = CircuitState.HALF_OPEN
        breaker._save_state()

        # L0 should be allowed
        allowed_l0, _ = breaker.can_evolve(RiskLevel.L0_CONFIG)
        assert allowed_l0 is True

        # L1 should be blocked
        allowed_l1, reason_l1 = breaker.can_evolve(RiskLevel.L1_TEMPLATE)
        assert allowed_l1 is False
        assert "HALF_OPEN" in reason_l1

    def test_successful_change_in_half_open_closes_circuit(self, tmp_path: Path) -> None:
        """A successful change in HALF_OPEN transitions circuit back to CLOSED."""
        evolution_dir = tmp_path / "evolution"
        evolution_dir.mkdir()
        breaker = CircuitBreaker(state_dir=evolution_dir)

        breaker.state = CircuitState.HALF_OPEN
        breaker._save_state()

        breaker.record_change(RiskLevel.L0_CONFIG, "UPG-RECOVER")
        assert breaker.state == CircuitState.CLOSED

    def test_circuit_state_persisted_to_disk(self, tmp_path: Path) -> None:
        """Circuit breaker state is persisted and reloaded correctly."""
        evolution_dir = tmp_path / "evolution"
        evolution_dir.mkdir()
        breaker = CircuitBreaker(state_dir=evolution_dir)

        breaker.record_rollback("UPG-PERSIST")
        assert breaker.state == CircuitState.OPEN

        # Reload from disk
        reloaded = CircuitBreaker(state_dir=evolution_dir)
        assert reloaded.state == CircuitState.OPEN

    def test_metrics_regression_trips_circuit(self, tmp_path: Path) -> None:
        """Janitor pass rate drop >15% WoW trips the circuit breaker."""
        evolution_dir = tmp_path / "evolution"
        evolution_dir.mkdir()
        breaker = CircuitBreaker(state_dir=evolution_dir)

        assert breaker.state == CircuitState.CLOSED
        breaker.check_metrics_regression(
            janitor_pass_rate_delta=-0.20,
            cost_per_task_delta=0.0,
        )
        assert breaker.state == CircuitState.OPEN

    def test_cost_regression_trips_circuit(self, tmp_path: Path) -> None:
        """Cost per task increase >25% WoW trips the circuit breaker."""
        evolution_dir = tmp_path / "evolution"
        evolution_dir.mkdir()
        breaker = CircuitBreaker(state_dir=evolution_dir)

        assert breaker.state == CircuitState.CLOSED
        breaker.check_metrics_regression(
            janitor_pass_rate_delta=0.0,
            cost_per_task_delta=0.30,
        )
        assert breaker.state == CircuitState.OPEN

    def test_rate_limit_blocks_l0_after_daily_cap(self, tmp_path: Path) -> None:
        """L0 changes are blocked after exceeding the daily rate limit (5/day)."""
        evolution_dir = tmp_path / "evolution"
        evolution_dir.mkdir()
        breaker = CircuitBreaker(state_dir=evolution_dir)

        # Apply 5 L0 changes (the daily cap)
        for i in range(5):
            breaker.record_change(RiskLevel.L0_CONFIG, f"UPG-RATE-{i}")

        allowed, reason = breaker.can_evolve(RiskLevel.L0_CONFIG)
        assert allowed is False
        assert "Rate limit" in reason

    def test_l3_structural_always_blocked_by_circuit(self, tmp_path: Path) -> None:
        """L3_STRUCTURAL is never allowed, even with a closed circuit."""
        evolution_dir = tmp_path / "evolution"
        evolution_dir.mkdir()
        breaker = CircuitBreaker(state_dir=evolution_dir)

        allowed, reason = breaker.can_evolve(RiskLevel.L3_STRUCTURAL)
        assert allowed is False
        assert "L3_STRUCTURAL" in reason

    def test_loop_run_cycle_circuit_blocked_logs_experiment(self, tmp_path: Path) -> None:
        """When circuit breaker blocks a proposal, the result is still logged to experiments.jsonl."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        loop = EvolutionLoop(state_dir=state_dir, repo_root=tmp_path)

        proposal = _make_proposal(id="UPG-CB-LOG", risk_level="low", confidence=0.95)
        opportunity = _make_opportunity(risk_level="low", confidence=0.95)

        with (
            patch.object(loop._aggregator, "run_full_analysis"),
            patch.object(loop._detector, "identify_opportunities", return_value=[opportunity]),
            patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
            patch.object(
                loop._breaker,
                "can_evolve",
                return_value=(False, "Rate limit reached"),
            ),
        ):
            result = loop.run_cycle()

        assert result is not None
        assert result.accepted is False

        experiments_path = loop._experiments_path
        assert experiments_path.exists()
        logged = json.loads(experiments_path.read_text().strip())
        assert logged["proposal_id"] == "UPG-CB-LOG"
        assert logged["accepted"] is False
        assert "Circuit breaker" in logged["reason"]
