"""Bernstein self-evolution system.

Risk-stratified architecture:
  L0 (Config)     — auto-apply after schema check
  L1 (Templates)  — sandbox A/B test, auto-apply if metrics improve
  L2 (Logic)      — git worktree + tests + PR + human review
  L3 (Structural) — NEVER auto-apply, human only

Safety-critical modules (janitor, server, orchestrator, invariants, circuit)
are hash-locked and cannot be modified by the evolution system.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.evolution.aggregator import (
    AgentMetrics,
    AnomalyDetection,
    CostMetrics,
    FileMetricsCollector,
    MetricRecord,
    MetricsAggregator,
    MetricsCollector,
    QualityMetrics,
    TaskMetrics,
    TrendAnalysis,
)
from bernstein.evolution.applicator import (
    FileUpgradeExecutor,
    UpgradeExecutor,
)
from bernstein.evolution.circuit import CircuitBreaker
from bernstein.evolution.detector import (
    FailureAnalyzer,
    FailurePattern,
    FailureRecord,
    ImprovementOpportunity,
    OpportunityDetector,
    UpgradeCategory,
)
from bernstein.evolution.gate import ApprovalGate
from bernstein.evolution.invariants import (
    check_proposal_targets,
    compute_invariants,
    verify_invariants,
    write_lockfile,
)
from bernstein.evolution.loop import EvolutionLoop, ExperimentResult
from bernstein.evolution.proposals import (
    AnalysisTrigger,
    ApprovalMode,
    ProposalGenerator,
    UpgradeProposal,
    UpgradeStatus,
)
from bernstein.evolution.sandbox import SandboxValidator
from bernstein.evolution.types import (
    CircuitState,
    MetricsRecord,
    ProposalStatus,
    RiskLevel,
    SandboxResult,
)

if TYPE_CHECKING:
    from bernstein.core.models import Task

__all__ = [
    "AgentMetrics",
    "AnalysisEngine",  # wrapper combining aggregator + detector
    # proposals
    "AnalysisTrigger",
    "AnomalyDetection",
    # gate
    "ApprovalGate",
    "ApprovalMode",
    # circuit
    "CircuitBreaker",
    "CircuitState",
    "CostMetrics",
    # coordinator
    "EvolutionCoordinator",
    # loop
    "EvolutionLoop",
    "ExperimentResult",
    "FailureAnalyzer",
    "FailurePattern",
    "FailureRecord",
    "FileMetricsCollector",
    "FileUpgradeExecutor",
    "ImprovementOpportunity",
    # aggregator
    "MetricRecord",
    "MetricsAggregator",
    "MetricsCollector",
    "MetricsRecord",
    "OpportunityDetector",
    "ProposalGenerator",
    "ProposalStatus",
    "QualityMetrics",
    # types
    "RiskLevel",
    "SandboxResult",
    # sandbox
    "SandboxValidator",
    "TaskMetrics",
    "TrendAnalysis",
    # detector
    "UpgradeCategory",
    # applicator
    "UpgradeExecutor",
    "UpgradeProposal",
    "UpgradeStatus",
    "check_proposal_targets",
    # invariants
    "compute_invariants",
    "get_default_coordinator",
    "verify_invariants",
    "write_lockfile",
]


class AnalysisEngine:
    """Combines MetricsAggregator + OpportunityDetector into a single analysis pass."""

    def __init__(self, collector: MetricsCollector) -> None:
        self.collector = collector
        self._aggregator = MetricsAggregator(collector)
        self._detector = OpportunityDetector(collector)
        self._trends: list[TrendAnalysis] = []
        self._anomalies: list[AnomalyDetection] = []
        self._opportunities: list[ImprovementOpportunity] = []

    def run_analysis(self) -> None:
        """Run aggregation, trend analysis, anomaly detection, and opportunity identification."""
        self._analyze_trends()
        self._detect_anomalies()
        self._identify_opportunities()

    def _analyze_trends(self) -> None:
        """Analyze trends from recent task metrics."""
        task_metrics = self.collector.get_recent_task_metrics(hours=168)
        if len(task_metrics) < 10:
            self._trends = []
            return

        trends: list[TrendAnalysis] = []
        extractors: list[tuple[str, Any]] = [
            ("cost_per_task", lambda m: m.cost_usd),
            ("task_duration", lambda m: m.duration_seconds),
            ("success_rate", lambda m: 1.0 if m.janitor_passed else 0.0),
        ]
        for metric_name, extractor in extractors:
            values = [extractor(m) for m in task_metrics]
            trend = self._calculate_trend(values, metric_name)
            if trend:
                trends.append(trend)

        self._trends = trends

    def _calculate_trend(
        self,
        values: list[float],
        metric_name: str,
    ) -> TrendAnalysis | None:
        """Calculate trend for a series of values. Delegates to aggregator."""
        return self._aggregator._calculate_trend(values, metric_name)

    def _detect_anomalies(self) -> None:
        """Detect anomalies in recent metrics."""
        self._anomalies = self._aggregator.detect_anomalies()

    def _identify_opportunities(self) -> None:
        """Identify improvement opportunities."""
        self._opportunities = self._detector.identify_opportunities()

    def get_trends(self) -> list[TrendAnalysis]:
        return self._trends

    def get_opportunities(self) -> list[ImprovementOpportunity]:
        return self._opportunities

    def get_anomalies(self) -> list[AnomalyDetection]:
        return self._anomalies


class EvolutionCoordinator:
    """Orchestrates the self-evolution feedback loop.

    Runs periodically to:
    1. Collect metrics from completed tasks
    2. Analyze trends and anomalies
    3. Generate upgrade proposals
    4. Execute approved upgrades
    """

    def __init__(
        self,
        state_dir: Path,
        collector: MetricsCollector | None = None,
        executor: UpgradeExecutor | None = None,
        analysis_interval_minutes: int = 60,
    ) -> None:
        self.state_dir = state_dir
        self.collector = collector or FileMetricsCollector(state_dir)
        self.executor = executor or FileUpgradeExecutor(state_dir)
        self.analysis_engine = AnalysisEngine(self.collector)
        self.analysis_interval_minutes = analysis_interval_minutes

        self._last_analysis: float = 0
        self._pending_upgrades: list[UpgradeProposal] = []
        self._applied_upgrades: list[UpgradeProposal] = []
        self._running: bool = False
        self._proposal_generator = ProposalGenerator()
        self._proposal_counter: int = 0

    def _create_proposal(
        self,
        opportunity: ImprovementOpportunity,
        trigger: AnalysisTrigger,
    ) -> UpgradeProposal:
        """Create an upgrade proposal from an opportunity."""
        return self._proposal_generator.create_proposal(opportunity, trigger)

    def _create_emergency_proposal(
        self,
        anomaly: AnomalyDetection,
    ) -> UpgradeProposal:
        """Create an emergency proposal for a critical anomaly."""
        return self._proposal_generator.create_emergency_proposal(anomaly)

    def run_analysis_cycle(
        self, trigger: AnalysisTrigger = AnalysisTrigger.SCHEDULED,
    ) -> list[UpgradeProposal]:
        """Run a complete analysis cycle and generate upgrade proposals.

        Args:
            trigger: What triggered this analysis cycle.

        Returns:
            List of generated upgrade proposals.
        """
        # Run analysis
        self.analysis_engine.run_analysis()

        # Generate proposals from opportunities
        proposals = []
        for opportunity in self.analysis_engine.get_opportunities():
            proposal = self._proposal_generator.create_proposal(opportunity, trigger)

            # Check if auto-approval applies
            if self._should_auto_approve(proposal):
                proposal.status = UpgradeStatus.APPROVED

            proposals.append(proposal)
            self._pending_upgrades.append(proposal)

        # Check for critical anomalies that need immediate attention
        for anomaly in self.analysis_engine.get_anomalies():
            if anomaly.severity == "critical":
                emergency_proposal = self._proposal_generator.create_emergency_proposal(anomaly)
                emergency_proposal.status = UpgradeStatus.APPROVED
                proposals.append(emergency_proposal)
                self._pending_upgrades.append(emergency_proposal)

        self._last_analysis = time.time()
        return proposals

    def _should_auto_approve(self, proposal: UpgradeProposal) -> bool:
        """Determine if a proposal should be auto-approved."""
        if proposal.approval_mode == ApprovalMode.AUTO:
            return True

        if proposal.approval_mode == ApprovalMode.HYBRID:
            return proposal.confidence >= 0.9

        return False

    def execute_pending_upgrades(self) -> list[UpgradeProposal]:
        """Execute all approved pending upgrades.

        On execution failure, attempts rollback via the executor.
        """
        executed = []

        for proposal in self._pending_upgrades:
            if proposal.status == UpgradeStatus.APPROVED:
                proposal.status = UpgradeStatus.IN_PROGRESS

                if self.executor.execute_upgrade(proposal):
                    proposal.status = UpgradeStatus.APPLIED
                    proposal.applied_at = time.time()
                    self._applied_upgrades.append(proposal)
                    executed.append(proposal)
                else:
                    # Execution failed — attempt rollback
                    if self.executor.rollback_upgrade(proposal):
                        proposal.status = UpgradeStatus.ROLLED_BACK
                    else:
                        proposal.status = UpgradeStatus.REJECTED

        # Remove resolved proposals from pending
        self._pending_upgrades = [
            p for p in self._pending_upgrades
            if p.status not in [
                UpgradeStatus.APPLIED,
                UpgradeStatus.REJECTED,
                UpgradeStatus.ROLLED_BACK,
            ]
        ]

        return executed

    def should_run_analysis(self) -> bool:
        """Check if it's time to run analysis."""
        elapsed_minutes = (time.time() - self._last_analysis) / 60
        return elapsed_minutes >= self.analysis_interval_minutes

    def get_pending_upgrades(self) -> list[UpgradeProposal]:
        return self._pending_upgrades.copy()

    def get_applied_upgrades(self) -> list[UpgradeProposal]:
        return self._applied_upgrades.copy()

    def get_analysis_summary(self) -> dict[str, Any]:
        """Get a summary of the latest analysis."""
        return {
            "last_analysis": self._last_analysis,
            "next_analysis_due": self._last_analysis + (self.analysis_interval_minutes * 60),
            "pending_upgrades": len(self._pending_upgrades),
            "applied_upgrades": len(self._applied_upgrades),
            "trends": [
                {
                    "metric": t.metric_name,
                    "direction": t.direction,
                    "change_percent": t.change_percent,
                }
                for t in self.analysis_engine.get_trends()
            ],
            "anomalies": [
                {
                    "metric": a.metric_name,
                    "severity": a.severity,
                    "description": a.description,
                }
                for a in self.analysis_engine.get_anomalies()
            ],
        }

    def record_task_completion(
        self,
        task: Task,
        duration_seconds: float,
        cost_usd: float,
        janitor_passed: bool,
        model: str | None = None,
        provider: str | None = None,
    ) -> None:
        """Record metrics for a completed task."""
        metrics = TaskMetrics(
            timestamp=time.time(),
            task_id=task.id,
            role=task.role,
            model=model,
            provider=provider,
            duration_seconds=duration_seconds,
            cost_usd=cost_usd,
            janitor_passed=janitor_passed,
        )
        self.collector.record_task_metrics(metrics)


# Default coordinator instance
_default_coordinator: EvolutionCoordinator | None = None


def get_default_coordinator(
    state_dir: Path | None = None,
    analysis_interval_minutes: int = 60,
) -> EvolutionCoordinator:
    """Get or create the default evolution coordinator."""
    global _default_coordinator

    if _default_coordinator is None:
        if state_dir is None:
            state_dir = Path(".sdd")

        _default_coordinator = EvolutionCoordinator(
            state_dir=state_dir,
            analysis_interval_minutes=analysis_interval_minutes,
        )

    return _default_coordinator
