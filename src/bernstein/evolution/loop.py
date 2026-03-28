"""Autoresearch evolution loop — continuous self-improvement via experiment cycles.

Implements the autoresearch pattern (Karpathy, March 2026):
editable asset + scalar metric + time-boxed cycle.

Target: 12 experiments per hour in 5-minute cycles.

Pipeline:
  MetricsAggregator → ProposalGenerator → SandboxValidator → ApprovalGate → Apply/Discard

Only L0 and L1 changes run in the automated loop.
L2+ proposals are saved for human review.
All results logged to .sdd/evolution/experiments.jsonl.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.evolution.aggregator import (
    FileMetricsCollector,
    MetricsAggregator,
)
from bernstein.evolution.applicator import FileUpgradeExecutor
from bernstein.evolution.benchmark import RunSummary, run_all, save_results
from bernstein.evolution.circuit import CircuitBreaker
from bernstein.evolution.detector import ImprovementOpportunity, OpportunityDetector
from bernstein.evolution.gate import ApprovalGate, ApprovalOutcome
from bernstein.evolution.proposals import (
    AnalysisTrigger,
    ProposalGenerator,
    UpgradeProposal,
)
from bernstein.evolution.sandbox import SandboxValidator
from bernstein.evolution.types import RiskLevel, SandboxResult
from bernstein.evolution.types import UpgradeProposal as TypesUpgradeProposal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Cost estimate per proposal generation (LLM call).
_COST_PER_PROPOSAL_USD = 0.05

# Risk levels eligible for the automated loop.
_AUTO_RISK_LEVELS: frozenset[str] = frozenset({
    RiskLevel.L0_CONFIG.value,
    RiskLevel.L1_TEMPLATE.value,
})


@dataclass
class ExperimentResult:
    """Outcome of a single autoresearch experiment cycle.

    Attributes:
        proposal_id: Unique identifier for the proposal tested.
        title: Human-readable title of the proposal.
        risk_level: String risk classification (from RiskLevel.value).
        baseline_score: Benchmark score before the experiment.
        candidate_score: Benchmark score after applying the proposal.
        delta: Difference (candidate - baseline).
        accepted: Whether the proposal was applied.
        reason: Explanation for the accept/discard decision.
        cost_usd: Estimated cost of the experiment.
        duration_seconds: Wall-clock time of the experiment cycle.
        timestamp: Unix timestamp when the result was recorded.
    """

    proposal_id: str
    title: str
    risk_level: str
    baseline_score: float
    candidate_score: float
    delta: float
    accepted: bool
    reason: str
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSONL output."""
        return {
            "proposal_id": self.proposal_id,
            "title": self.title,
            "risk_level": self.risk_level,
            "baseline_score": self.baseline_score,
            "candidate_score": self.candidate_score,
            "delta": self.delta,
            "accepted": self.accepted,
            "reason": self.reason,
            "cost_usd": self.cost_usd,
            "duration_seconds": self.duration_seconds,
            "timestamp": self.timestamp,
        }


class EvolutionLoop:
    """Autoresearch evolution loop — runs time-boxed experiment cycles.

    Each cycle: load metrics, run baseline, generate proposal, sandbox test,
    approve or discard, log results. Targets 12 experiments per hour in
    5-minute cycles.

    Only L0_CONFIG and L1_TEMPLATE proposals are processed in the automated
    loop. L2+ proposals are saved to .sdd/evolution/deferred.jsonl for human
    review.

    Args:
        state_dir: Path to the .sdd directory.
        repo_root: Repository root. Defaults to state_dir.parent.
        cycle_seconds: Duration of each experiment cycle (default 300 = 5 min).
        max_proposals: Maximum proposals to evaluate per session (default 24).
        window_seconds: Total session duration in seconds (default 7200 = 2h).
    """

    def __init__(
        self,
        state_dir: Path,
        repo_root: Path | None = None,
        cycle_seconds: int = 300,
        max_proposals: int = 24,
        window_seconds: int = 7200,
    ) -> None:
        self._state_dir = state_dir
        self._repo_root = repo_root or state_dir.parent
        self._cycle_seconds = cycle_seconds
        self._max_proposals = max_proposals
        self._window_seconds = window_seconds

        # --- Component wiring ---
        self._analysis_dir = state_dir / "analysis"
        self._analysis_dir.mkdir(parents=True, exist_ok=True)
        self._collector = FileMetricsCollector(state_dir)
        self._aggregator = MetricsAggregator(self._collector, analysis_dir=self._analysis_dir)
        self._detector = OpportunityDetector(self._collector, analysis_dir=self._analysis_dir)
        self._proposal_generator = ProposalGenerator()
        self._sandbox = SandboxValidator(self._repo_root)
        self._evolution_dir = state_dir / "evolution"
        self._gate = ApprovalGate(decisions_dir=self._evolution_dir)
        self._breaker = CircuitBreaker(state_dir=self._evolution_dir)
        self._executor = FileUpgradeExecutor(state_dir)

        # --- State directory setup ---
        self._evolution_dir.mkdir(parents=True, exist_ok=True)
        self._experiments_path = self._evolution_dir / "experiments.jsonl"
        self._deferred_path = self._evolution_dir / "deferred.jsonl"

        # --- Session counters ---
        self._experiments: list[ExperimentResult] = []
        self._proposals_generated: int = 0
        self._proposals_accepted: int = 0
        self._start_time: float = 0.0
        self._running: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        window_seconds: int | None = None,
        max_proposals: int | None = None,
    ) -> list[ExperimentResult]:
        """Run the autoresearch loop for the specified window.

        Args:
            window_seconds: Override session duration. Uses default if None.
            max_proposals: Override max proposals. Uses default if None.

        Returns:
            List of ExperimentResult from this session.
        """
        effective_window = window_seconds if window_seconds is not None else self._window_seconds
        effective_max = max_proposals if max_proposals is not None else self._max_proposals

        self._running = True
        self._start_time = time.time()
        self._experiments = []
        self._proposals_generated = 0
        self._proposals_accepted = 0

        logger.info(
            "Evolution loop started: window=%ds, max_proposals=%d, cycle=%ds",
            effective_window,
            effective_max,
            self._cycle_seconds,
        )

        while (
            self._within_window(effective_window)
            and self._proposals_generated < effective_max
            and self._running
        ):
            cycle_start = time.time()

            try:
                result = self.run_cycle()
                if result is not None:
                    self._experiments.append(result)
            except Exception:
                logger.exception("Unhandled error in evolution cycle")

            # Sleep until next cycle boundary, but only if still running.
            if self._running and self._within_window(effective_window):
                elapsed_in_cycle = time.time() - cycle_start
                remaining = self._cycle_seconds - elapsed_in_cycle
                if remaining > 0:
                    time.sleep(remaining)

        self._running = False

        logger.info(
            "Evolution loop finished: %d experiments, %d accepted, %d generated",
            len(self._experiments),
            self._proposals_accepted,
            self._proposals_generated,
        )
        return self._experiments

    def run_cycle(self) -> ExperimentResult | None:
        """Run a single experiment cycle (one proposal).

        Steps:
        1. Load recent metrics and run full analysis.
        2. Run baseline benchmark.
        3. Generate a proposal from detected opportunities.
        4. Check circuit breaker.
        5. Route through approval gate.
        6. If auto-approved: sandbox validate, then apply or discard.
        7. If needs human review: save for later, log as deferred.
        8. Log result to experiments.jsonl.

        Returns:
            ExperimentResult, or None if no opportunities were found.
        """
        cycle_start = time.time()

        # Step 1 — Gather metrics and detect opportunities.
        self._aggregator.run_full_analysis()
        opportunities = self._detector.identify_opportunities()

        # Step 2 — Run baseline benchmark.
        baseline_score = self._run_baseline()

        # Step 3 — Generate a proposal.
        proposal = self._generate_proposal(opportunities)
        if proposal is None:
            logger.debug("No actionable opportunities found this cycle")
            return None

        self._proposals_generated += 1
        logger.info(
            "Proposal %s: %s (risk=%s, confidence=%.2f)",
            proposal.id,
            proposal.title,
            proposal.risk_assessment.level,
            proposal.confidence,
        )

        # Step 4 — Circuit breaker check.
        # Map the proposal risk assessment to a RiskLevel for the breaker.
        risk_level = self._infer_risk_level(proposal)
        can_evolve, breaker_reason = self._breaker.can_evolve(risk_level)
        if not can_evolve:
            logger.warning(
                "Circuit breaker blocked %s: %s", proposal.id, breaker_reason,
            )
            result = ExperimentResult(
                proposal_id=proposal.id,
                title=proposal.title,
                risk_level=risk_level.value,
                baseline_score=baseline_score,
                candidate_score=baseline_score,
                delta=0.0,
                accepted=False,
                reason=f"Circuit breaker: {breaker_reason}",
                cost_usd=_COST_PER_PROPOSAL_USD,
                duration_seconds=time.time() - cycle_start,
            )
            self._log_experiment(result)
            return result

        # Step 5 — Approval gate routing.
        decision = self._gate.route(
            _to_types_proposal(proposal, risk_level),
        )

        is_auto = decision.outcome in (
            ApprovalOutcome.AUTO_APPROVED,
            ApprovalOutcome.AUTO_APPROVED_AUDIT,
        )

        if not is_auto:
            # L2+ or low-confidence: defer to human.
            self._log_deferred(proposal, decision.reason)
            result = ExperimentResult(
                proposal_id=proposal.id,
                title=proposal.title,
                risk_level=risk_level.value,
                baseline_score=baseline_score,
                candidate_score=baseline_score,
                delta=0.0,
                accepted=False,
                reason=f"Deferred for human review: {decision.reason}",
                cost_usd=_COST_PER_PROPOSAL_USD,
                duration_seconds=time.time() - cycle_start,
            )
            self._log_experiment(result)
            return result

        # Step 6 — Sandbox validation.
        sandbox_result = self._sandbox.validate(
            proposal_id=proposal.id,
            diff=proposal.proposed_change,
            baseline_score=baseline_score,
        )

        if not sandbox_result.passed:
            self._breaker.record_sandbox_failure(proposal.id)
            result = ExperimentResult(
                proposal_id=proposal.id,
                title=proposal.title,
                risk_level=risk_level.value,
                baseline_score=baseline_score,
                candidate_score=sandbox_result.candidate_score,
                delta=sandbox_result.delta,
                accepted=False,
                reason=f"Sandbox failed: {sandbox_result.error or 'tests did not pass'}",
                cost_usd=_COST_PER_PROPOSAL_USD,
                duration_seconds=time.time() - cycle_start,
            )
            self._log_experiment(result)
            return result

        # Step 7 — Apply the proposal.
        applied = self._apply_proposal(proposal, sandbox_result)
        candidate_score = sandbox_result.candidate_score if applied else baseline_score
        delta = sandbox_result.delta if applied else 0.0

        if applied:
            self._proposals_accepted += 1

        result = ExperimentResult(
            proposal_id=proposal.id,
            title=proposal.title,
            risk_level=risk_level.value,
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            delta=delta,
            accepted=applied,
            reason="Applied successfully" if applied else "Application failed",
            cost_usd=_COST_PER_PROPOSAL_USD,
            duration_seconds=time.time() - cycle_start,
        )
        self._log_experiment(result)
        return result

    def stop(self) -> None:
        """Signal the loop to stop after the current cycle."""
        logger.info("Evolution loop stop requested")
        self._running = False

    def get_summary(self) -> dict[str, Any]:
        """Return summary of the evolution session.

        Returns:
            Dict with session statistics including counts, rates, and timing.
        """
        elapsed = time.time() - self._start_time if self._start_time else 0.0
        return {
            "experiments_run": len(self._experiments),
            "proposals_generated": self._proposals_generated,
            "proposals_accepted": self._proposals_accepted,
            "acceptance_rate": self.acceptance_rate,
            "elapsed_seconds": round(elapsed, 1),
            "experiments_per_hour": (
                round(len(self._experiments) / (elapsed / 3600), 1)
                if elapsed > 0
                else 0.0
            ),
            "total_cost_usd": round(
                sum(e.cost_usd for e in self._experiments), 4,
            ),
            "running": self._running,
        }

    @property
    def acceptance_rate(self) -> float:
        """Fraction of generated proposals that were accepted."""
        if self._proposals_generated == 0:
            return 0.0
        return self._proposals_accepted / self._proposals_generated

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_baseline(self) -> float:
        """Run benchmark suite and return aggregate score.

        Looks for benchmarks under tests/benchmarks/ relative to the repo
        root. Returns passed/total ratio, or 1.0 if no benchmarks exist.
        """
        benchmarks_dir = self._repo_root / "tests" / "benchmarks"
        if not benchmarks_dir.is_dir():
            logger.debug("No benchmarks directory found at %s — baseline=1.0", benchmarks_dir)
            return 1.0

        try:
            summary: RunSummary = run_all(benchmarks_dir)
            save_results(summary, self._state_dir)
            if summary.total == 0:
                return 1.0
            score = summary.passed / summary.total
            logger.info(
                "Baseline benchmark: %d/%d passed (%.2f)",
                summary.passed,
                summary.total,
                score,
            )
            return score
        except Exception:
            logger.exception("Baseline benchmark run failed — defaulting to 1.0")
            return 1.0

    def _generate_proposal(
        self,
        opportunities: list[ImprovementOpportunity],
    ) -> UpgradeProposal | None:
        """Pick the best L0/L1 opportunity and create a proposal.

        Filters opportunities to those with risk levels eligible for the
        automated loop (low and medium map to L0/L1), then sorts by
        confidence descending and creates a proposal from the best one.

        Args:
            opportunities: Detected improvement opportunities.

        Returns:
            UpgradeProposal for the highest-confidence eligible opportunity,
            or None if no eligible opportunities exist.
        """
        # Filter to low-risk opportunities suitable for the automated loop.
        # In the detector, risk_level is a string literal ("low", "medium", "high").
        # "low" maps to L0_CONFIG, "medium" to L1_TEMPLATE in the automated loop.
        eligible = [
            opp for opp in opportunities
            if opp.risk_level in ("low", "medium")
        ]

        if not eligible:
            return None

        # Sort by confidence descending — pick the most promising.
        eligible.sort(key=lambda o: o.confidence, reverse=True)
        best = eligible[0]

        return self._proposal_generator.create_proposal(
            best, AnalysisTrigger.SCHEDULED,
        )

    def _apply_proposal(
        self,
        proposal: UpgradeProposal,
        sandbox_result: SandboxResult,
    ) -> bool:
        """Apply an accepted proposal.

        Uses FileUpgradeExecutor to execute the change, then records the
        change in the circuit breaker.

        Args:
            proposal: The approved proposal.
            sandbox_result: Sandbox validation result.

        Returns:
            True if the proposal was applied successfully.
        """
        risk_level = self._infer_risk_level(proposal)

        success = self._executor.execute_upgrade(proposal)
        if success:
            self._breaker.record_change(risk_level, proposal.id)
            logger.info("Proposal %s applied successfully", proposal.id)
        else:
            logger.warning("Proposal %s application failed — attempting rollback", proposal.id)
            self._executor.rollback_upgrade(proposal)
            self._breaker.record_rollback(proposal.id)

        return success

    def _log_experiment(self, result: ExperimentResult) -> None:
        """Append experiment result to experiments.jsonl."""
        try:
            with self._experiments_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(result.to_dict()) + "\n")
        except OSError:
            logger.exception("Failed to write experiment log")

    def _log_deferred(self, proposal: UpgradeProposal, reason: str) -> None:
        """Log a deferred proposal to deferred.jsonl for human review."""
        record = {
            "proposal_id": proposal.id,
            "title": proposal.title,
            "category": proposal.category.value,
            "description": proposal.description,
            "confidence": proposal.confidence,
            "reason": reason,
            "deferred_at": time.time(),
        }
        try:
            with self._deferred_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            logger.info("Proposal %s deferred for human review", proposal.id)
        except OSError:
            logger.exception("Failed to write deferred proposal log")

    def _within_window(self, window_seconds: int) -> bool:
        """Check if the loop is still within the evolution window.

        Args:
            window_seconds: Maximum session duration in seconds.

        Returns:
            True if current time is within the window.
        """
        return (time.time() - self._start_time) < window_seconds

    @staticmethod
    def _infer_risk_level(proposal: UpgradeProposal) -> RiskLevel:
        """Map a proposal's risk assessment to a RiskLevel enum.

        The ProposalGenerator sets ``risk_assessment.level`` as a string
        ("low", "medium", "high"). We map these to evolution risk levels:
        - "low" → L0_CONFIG
        - "medium" → L1_TEMPLATE
        - "high" → L2_LOGIC (will be blocked by the automated loop)

        Args:
            proposal: The upgrade proposal to classify.

        Returns:
            Corresponding RiskLevel.
        """
        level_str = proposal.risk_assessment.level
        mapping: dict[str, RiskLevel] = {
            "low": RiskLevel.L0_CONFIG,
            "medium": RiskLevel.L1_TEMPLATE,
            "high": RiskLevel.L2_LOGIC,
            "critical": RiskLevel.L3_STRUCTURAL,
        }
        return mapping.get(level_str, RiskLevel.L2_LOGIC)


def _to_types_proposal(
    proposal: UpgradeProposal,
    risk_level: RiskLevel,
) -> TypesUpgradeProposal:
    """Convert a proposals.UpgradeProposal to a types.UpgradeProposal for the gate.

    The ApprovalGate.route() expects bernstein.evolution.types.UpgradeProposal
    which has different fields than proposals.UpgradeProposal. This adapter
    bridges the two schemas.

    Args:
        proposal: The proposals-module UpgradeProposal.
        risk_level: The classified RiskLevel.

    Returns:
        A types-module UpgradeProposal suitable for the approval gate.
    """
    return TypesUpgradeProposal(
        id=proposal.id,
        title=proposal.title,
        description=proposal.description,
        risk_level=risk_level,
        target_files=[],  # No specific file targets for generated proposals.
        diff=proposal.proposed_change,
        rationale=proposal.description,
        expected_impact=proposal.expected_improvement,
        confidence=proposal.confidence,
    )
