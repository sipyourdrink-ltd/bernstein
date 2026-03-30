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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bernstein.evolution.aggregator import (
    FileMetricsCollector,
    MetricsAggregator,
)
from bernstein.evolution.applicator import FileUpgradeExecutor
from bernstein.evolution.benchmark import RunSummary, run_all, save_results
from bernstein.evolution.circuit import CircuitBreaker
from bernstein.evolution.creative import (
    AnalystVerdict,
    CreativePipeline,
    PipelineResult,
    VisionaryProposal,
    issue_to_proposal,
)
from bernstein.evolution.detector import (
    FeatureDiscovery,
    ImprovementOpportunity,
    OpportunityDetector,
)
from bernstein.evolution.gate import ApprovalGate, ApprovalOutcome, EvalGate
from bernstein.evolution.governance import (
    AdaptiveGovernor,
    EvolutionWeights,
    GovernanceEntry,
    ProjectContext,
)
from bernstein.evolution.proposals import (
    AnalysisTrigger,
    ProposalGenerator,
    UpgradeProposal,
)
from bernstein.evolution.risk import ProposalRiskScore, RiskScorer
from bernstein.evolution.sandbox import SandboxValidator
from bernstein.core.prometheus import evolution_errors_by_type
from bernstein.evolution.types import (
    ApplyError,
    ProposalGenerationError,
    RiskLevel,
    RollbackError,
    SandboxResult,
    SandboxValidationError,
)
from bernstein.evolution.types import UpgradeProposal as TypesUpgradeProposal

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.github import GitHubClient

logger = logging.getLogger(__name__)

# Cost estimate per proposal generation (LLM call).
_COST_PER_PROPOSAL_USD = 0.05

# Risk levels eligible for the automated loop.
_AUTO_RISK_LEVELS: frozenset[str] = frozenset(
    {
        RiskLevel.L0_CONFIG.value,
        RiskLevel.L1_TEMPLATE.value,
    }
)

# Focus area rotation — creative_vision runs every 4th cycle.
# Agents write proposals to .sdd/evolution/creative/pending_proposals.jsonl;
# the loop picks them up on creative_vision turns.
_FOCUS_ROTATION: tuple[str, ...] = (
    "code_quality",
    "test_coverage",
    "performance",
    "creative_vision",
)

# In community mode the first slot of each rotation is replaced with a
# community_issue scan so community work gets priority.
_FOCUS_ROTATION_COMMUNITY: tuple[str, ...] = (
    "community_issue",
    "test_coverage",
    "performance",
    "creative_vision",
)


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
        github_sync: If True, sync proposals with GitHub Issues for distributed
            coordination.  Requires the ``gh`` CLI to be installed and
            authenticated.  Disabled by default.
        community_mode: If True, scan for community ``evolve-candidate`` /
            ``feature-request`` issues and prioritise them in the evolve queue.
            Implies ``github_sync=True``.
    """

    def __init__(
        self,
        state_dir: Path,
        repo_root: Path | None = None,
        cycle_seconds: int = 300,
        max_proposals: int = 24,
        window_seconds: int = 7200,
        github_sync: bool = False,
        community_mode: bool = False,
        governor: AdaptiveGovernor | None = None,
    ) -> None:
        self._state_dir = state_dir
        self._repo_root = repo_root or state_dir.parent
        self._cycle_seconds = cycle_seconds
        self._max_proposals = max_proposals
        self._window_seconds = window_seconds
        self._community_mode = community_mode
        # Community mode requires GitHub sync to claim/update issues.
        self._github_sync = github_sync or community_mode
        self._github: GitHubClient | None = None

        # --- Component wiring ---
        self._analysis_dir = state_dir / "analysis"
        self._analysis_dir.mkdir(parents=True, exist_ok=True)
        self._collector = FileMetricsCollector(state_dir)
        self._aggregator = MetricsAggregator(self._collector, analysis_dir=self._analysis_dir)
        self._detector = OpportunityDetector(self._collector, analysis_dir=self._analysis_dir)
        self._feature_discovery = FeatureDiscovery(
            repo_root=self._repo_root,
            backlog_dir=state_dir / "backlog",
        )
        self._proposal_generator = ProposalGenerator()
        self._sandbox = SandboxValidator(self._repo_root)
        self._evolution_dir = state_dir / "evolution"
        self._gate = ApprovalGate(decisions_dir=self._evolution_dir)
        self._breaker = CircuitBreaker(state_dir=self._evolution_dir)
        self._executor = FileUpgradeExecutor(state_dir)

        # --- Eval gate (eval-gated evolution #516) ---
        from bernstein.eval.harness import EvalHarness

        self._eval_harness = EvalHarness(state_dir=state_dir, repo_root=self._repo_root)
        self._eval_gate = EvalGate(
            eval_harness=self._eval_harness,
            state_dir=state_dir,
        )

        # --- State directory setup ---
        self._evolution_dir.mkdir(parents=True, exist_ok=True)
        self._experiments_path = self._evolution_dir / "experiments.jsonl"
        self._deferred_path = self._evolution_dir / "deferred.jsonl"

        # Creative pipeline (lazy-wired, shared across cycles).
        self._creative_pipeline = CreativePipeline(
            state_dir=state_dir,
            repo_root=self._repo_root,
            github_sync=github_sync,
        )

        # --- Adaptive governance and risk scoring ---
        self._governor = governor or AdaptiveGovernor(state_dir)
        self._risk_scorer = RiskScorer()
        self._current_weights: EvolutionWeights = self._governor.get_current_weights()
        # Per-cycle governance tracking (reset at start of each main cycle).
        self._cycle_weights_before: EvolutionWeights = EvolutionWeights()
        self._cycle_weight_reason: str = ""
        self._cycle_proposals_evaluated: int = 0
        self._cycle_proposals_applied: int = 0
        self._cycle_risk_scores: list[float] = []
        self._cycle_outcome_metrics: dict[str, float] = {}

        # --- Session counters ---
        self._experiments: list[ExperimentResult] = []
        self._proposals_generated: int = 0
        self._proposals_accepted: int = 0
        self._start_time: float = 0.0
        self._running: bool = False
        self._cycle_count: int = 0
        self._consecutive_empty: int = 0

        # --- Error tracking ---
        self._error_counts: dict[str, int] = {}
        self._consecutive_errors: dict[str, int] = {}

        # --- GitHub sync state ---
        # Tracks the GitHub issue number for the proposal currently in flight
        # so we can close it when the proposal is accepted.
        self._current_issue_number: int | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def _gh(self) -> GitHubClient | None:
        """Return the lazily-initialised GitHubClient, or None if sync disabled.

        Deferred import keeps the ``gh`` CLI optional — the evolution loop
        works without it.
        """
        if not self._github_sync:
            return None
        if self._github is None:
            from bernstein.core.github import GitHubClient

            self._github = GitHubClient()
            if not self._github.available:
                logger.warning(
                    "GitHub sync requested but gh CLI is unavailable or unauthenticated — running without GitHub sync"
                )
        return self._github

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
        self._cycle_count = 0

        logger.info(
            "Evolution loop started: window=%ds, max_proposals=%d, cycle=%ds",
            effective_window,
            effective_max,
            self._cycle_seconds,
        )
        if self._github_sync:
            gh = self._gh
            if gh and gh.available:
                if self._community_mode:
                    logger.info(
                        "Community mode enabled — scanning GitHub for evolve-candidate and feature-request issues"
                    )
                else:
                    logger.info("GitHub sync enabled — proposals will be synced as Issues")

        while self._within_window(effective_window) and self._proposals_generated < effective_max and self._running:
            cycle_start = time.time()

            try:
                result = self.run_cycle()
                if result is not None:
                    self._experiments.append(result)
                # Successful cycle completion — reset consecutive error counts.
                self._consecutive_errors.clear()
            except (ProposalGenerationError, SandboxValidationError, ApplyError, RollbackError) as e:
                self._record_error(
                    type(e).__name__,
                    proposal_id=e.proposal_id,
                    focus_area=e.focus_area,
                    risk_level=e.risk_level,
                )
                logger.exception("Evolution cycle error (%s): %s", type(e).__name__, e)
            except Exception as e:
                self._record_error(
                    "UnhandledError",
                    proposal_id=None,
                    focus_area="",
                    risk_level="",
                )
                logger.exception("Unhandled error in evolution cycle: %s", e)

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

        # Determine focus area from rotation.
        rotation = _FOCUS_ROTATION_COMMUNITY if self._community_mode else _FOCUS_ROTATION
        focus = rotation[self._cycle_count % len(rotation)]
        self._cycle_count += 1

        logger.info("Evolution cycle %d — focus: %s", self._cycle_count, focus)

        # Community issue cycles delegate to the community pipeline.
        if focus == "community_issue":
            return self._run_community_cycle(cycle_start)

        # Creative vision cycles delegate entirely to the creative pipeline.
        if focus == "creative_vision":
            return self._run_creative_cycle(cycle_start)

        # Governance — adjust weights before each scoring cycle.
        self._cycle_weights_before = self._current_weights
        project_ctx = self._build_project_context()
        self._current_weights, self._cycle_weight_reason = self._governor.adjust_weights(
            self._cycle_weights_before, project_ctx
        )
        self._governor.persist_weights(self._current_weights, self._cycle_weight_reason)
        # Reset per-cycle accumulators.
        self._cycle_proposals_evaluated = 0
        self._cycle_proposals_applied = 0
        self._cycle_risk_scores = []
        self._cycle_outcome_metrics = {}

        # Step 1 — Gather metrics, detect opportunities, and run feature discovery.
        self._aggregator.run_full_analysis()
        opportunities = self._detector.identify_opportunities()
        feature_tickets = self._feature_discovery.discover(max_tickets=5)
        if feature_tickets:
            logger.info(
                "Feature discovery: %d new ticket(s) written to backlog",
                len(feature_tickets),
            )

        # Step 2 — Run baseline benchmark.
        baseline_score = self._run_baseline()

        # Step 3 — GitHub coordination: check for unclaimed issues before generating.
        # If GitHub sync is enabled, check whether another instance is already
        # working on something similar.  We still generate locally (the proposal
        # generator drives from detected metrics), but we skip publishing a new
        # issue if an equivalent one is already open and unclaimed.
        self._current_issue_number = None
        github_hint: str | None = None
        if self._github_sync:
            github_hint = self._github_check_unclaimed()

        # Step 4 — Generate a proposal.
        try:
            proposal = self._generate_proposal(opportunities)
        except Exception as exc:
            raise ProposalGenerationError(
                str(exc),
                focus_area=focus,
                risk_level="unknown",
            ) from exc
        if proposal is None:
            logger.debug("No actionable opportunities found this cycle")
            self._consecutive_empty += 1
            if github_hint is not None:
                # We may have claimed an issue but generated nothing locally.
                # Unclaim so another instance can pick it up.
                self._github_unclaim_current()
            self._flush_governance_log()
            return None

        self._proposals_generated += 1
        # Governance — compute risk score and determine routing strategy.
        risk_score = self._compute_proposal_risk(proposal)
        self._cycle_risk_scores.append(risk_score.composite_risk)
        self._cycle_proposals_evaluated = 1
        risk_route = self._classify_risk_route(risk_score.composite_risk)
        logger.info(
            "Proposal %s: %s (risk=%s, confidence=%.2f, composite_risk=%.2f, route=%s)",
            proposal.id,
            proposal.title,
            proposal.risk_assessment.level,
            proposal.confidence,
            risk_score.composite_risk,
            risk_route,
        )

        # Publish or claim a GitHub issue for this proposal.
        if self._github_sync:
            self._github_sync_proposal(proposal.title, proposal.description)

        # Step 5 — Circuit breaker check.
        # Map the proposal risk assessment to a RiskLevel for the breaker.
        risk_level = self._infer_risk_level(proposal)
        can_evolve, breaker_reason = self._breaker.can_evolve(risk_level)
        if not can_evolve:
            logger.warning(
                "Circuit breaker blocked %s: %s",
                proposal.id,
                breaker_reason,
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
            self._flush_governance_log()
            return result

        # Step 6 — Approval gate routing.
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
            # Unclaim the GitHub issue so a human (or another instance) can
            # pick it up via the normal review flow.
            self._github_unclaim_current()
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
            self._flush_governance_log()
            return result

        # Step 7 — Sandbox validation.
        # Fast-tracked proposals (composite_risk < 0.3) bypass the sandbox.
        # High-risk proposals (composite_risk > 0.6) are always sandbox-verified.
        if risk_route == "fast_track":
            logger.info(
                "Proposal %s fast-tracked (composite_risk=%.2f) — skipping sandbox",
                proposal.id,
                risk_score.composite_risk,
            )
            sandbox_result = self._make_fast_track_sandbox_result(proposal.id, baseline_score)
        else:
            try:
                sandbox_result = self._sandbox.validate(
                    proposal_id=proposal.id,
                    diff=proposal.proposed_change,
                    baseline_score=baseline_score,
                )
            except Exception as exc:
                raise SandboxValidationError(
                    str(exc),
                    proposal_id=proposal.id,
                    focus_area=focus,
                    risk_level=risk_level.value,
                ) from exc

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
                self._flush_governance_log()
                return result

        # Step 7b — Eval gate (eval-gated evolution #516).
        # After sandbox passes, run the eval harness and compare against baseline.
        eval_result = self._eval_gate.evaluate(
            proposal=_to_types_proposal(proposal, risk_level),
            risk_level=risk_level,
        )
        if not eval_result.skipped and not eval_result.accepted:
            result = ExperimentResult(
                proposal_id=proposal.id,
                title=proposal.title,
                risk_level=risk_level.value,
                baseline_score=eval_result.baseline_score,
                candidate_score=eval_result.score,
                delta=eval_result.delta,
                accepted=False,
                reason=f"Eval gate rejected: {eval_result.reason}",
                cost_usd=_COST_PER_PROPOSAL_USD,
                duration_seconds=time.time() - cycle_start,
            )
            self._log_experiment(result)
            self._flush_governance_log()
            return result

        # Step 8 — Apply the proposal.
        applied = self._apply_proposal(proposal, sandbox_result)
        candidate_score = sandbox_result.candidate_score if applied else baseline_score
        delta = sandbox_result.delta if applied else 0.0

        if applied:
            self._proposals_accepted += 1
            self._consecutive_empty = 0
            self._cycle_proposals_applied = 1
            self._cycle_outcome_metrics = {
                "delta": delta,
                "candidate_score": candidate_score,
                "composite_risk": risk_score.composite_risk,
            }
            # Close the GitHub issue to signal completion.
            self._github_close_current(
                comment=(
                    f"Proposal **{proposal.title}** applied automatically.\n\n"
                    f"- Risk: `{risk_level.value}`\n"
                    f"- Score delta: `{delta:+.4f}`"
                ),
            )
        else:
            self._consecutive_empty += 1
            self._cycle_outcome_metrics = {
                "delta": 0.0,
                "composite_risk": risk_score.composite_risk,
            }

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
        self._flush_governance_log()
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
            "experiments_per_hour": (round(len(self._experiments) / (elapsed / 3600), 1) if elapsed > 0 else 0.0),
            "total_cost_usd": round(
                sum(e.cost_usd for e in self._experiments),
                4,
            ),
            "running": self._running,
        }

    @property
    def acceptance_rate(self) -> float:
        """Fraction of generated proposals that were accepted."""
        if self._proposals_generated == 0:
            return 0.0
        return self._proposals_accepted / self._proposals_generated

    def get_error_summary(self) -> dict[str, int]:
        """Return cumulative error counts by type for this session.

        Returns:
            Dict mapping error type name to total occurrence count.
        """
        return dict(self._error_counts)

    def _record_error(
        self,
        error_type: str,
        *,
        proposal_id: str | None,
        focus_area: str,
        risk_level: str,
    ) -> None:
        """Increment error counters and emit a structured error log.

        Increments both the cumulative count and consecutive count for the
        given error type.  Logs a WARNING when consecutive errors of the same
        type reach 3 ("evolution loop degraded").  Also increments the
        Prometheus counter so the error is visible on the ``/metrics`` endpoint.

        Args:
            error_type: Short name of the error class (e.g. "ApplyError").
            proposal_id: ID of the proposal being processed, if known.
            focus_area: Cycle focus area, if known.
            risk_level: Risk level string of the proposal, if known.
        """
        self._error_counts[error_type] = self._error_counts.get(error_type, 0) + 1
        self._consecutive_errors[error_type] = self._consecutive_errors.get(error_type, 0) + 1

        evolution_errors_by_type.labels(error_type=error_type).inc()

        logger.error(
            "Evolution error: %s — proposal=%s focus=%s risk=%s",
            error_type,
            proposal_id,
            focus_area,
            risk_level,
            extra={
                "error_type": error_type,
                "proposal_id": proposal_id,
                "focus_area": focus_area,
                "risk_level": risk_level,
            },
        )

        consecutive = self._consecutive_errors[error_type]
        if consecutive >= 3:
            logger.warning(
                "evolution loop degraded: %d consecutive %s errors (total=%d)",
                consecutive,
                error_type,
                self._error_counts[error_type],
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_creative_cycle(self, cycle_start: float) -> ExperimentResult | None:
        """Run a creative vision cycle via the three-stage pipeline.

        Reads pending visionary proposals and analyst verdicts from
        ``.sdd/evolution/creative/pending_proposals.jsonl``.  Each line must
        be a JSON object with ``"proposal"`` and ``"verdict"`` keys.  The loop
        drains the file on each creative turn and runs the production gate.

        Agents that want to contribute creative ideas write lines to that file;
        the evolution loop picks them up on the next creative_vision turn.

        Args:
            cycle_start: Unix timestamp when this cycle started.

        Returns:
            ExperimentResult summarising the creative run, or None if no
            pending proposals were found.
        """
        pending_path = self._evolution_dir / "creative" / "pending_proposals.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)

        if not pending_path.exists() or pending_path.stat().st_size == 0:
            logger.debug("Creative vision: no pending proposals — skipping cycle")
            return None

        proposals: list[VisionaryProposal] = []
        verdicts: list[AnalystVerdict] = []

        try:
            raw_lines = pending_path.read_text(encoding="utf-8").strip().splitlines()
            for line in raw_lines:
                if not line.strip():
                    continue
                record = json.loads(line)
                if "proposal" in record:
                    proposals.append(VisionaryProposal.from_dict(record["proposal"]))
                if "verdict" in record:
                    verdicts.append(AnalystVerdict.from_dict(record["verdict"]))
            # Drain the file so proposals are not re-processed next cycle.
            pending_path.write_text("", encoding="utf-8")
        except (OSError, json.JSONDecodeError, KeyError):
            logger.exception("Creative vision: failed to read pending proposals")
            return None

        if not proposals and not verdicts:
            return None

        logger.info(
            "Creative vision: running pipeline with %d proposal(s), %d verdict(s)",
            len(proposals),
            len(verdicts),
        )

        pipeline_result: PipelineResult = self._creative_pipeline.run(
            proposals,
            verdicts,
        )

        approved_count = len(pipeline_result.approved)
        tasks_count = len(pipeline_result.tasks_created)
        accepted = approved_count > 0

        logger.info(
            "Creative vision: %d approved, %d backlog task(s) created",
            approved_count,
            tasks_count,
        )

        result = ExperimentResult(
            proposal_id=f"creative-{self._cycle_count}",
            title=f"Creative vision cycle ({len(proposals)} proposals)",
            risk_level=RiskLevel.L1_TEMPLATE.value,
            baseline_score=1.0,
            candidate_score=1.0 + (0.01 * approved_count),
            delta=0.01 * approved_count,
            accepted=accepted,
            reason=(f"{approved_count}/{len(verdicts)} approved, {tasks_count} backlog task(s) created"),
            cost_usd=_COST_PER_PROPOSAL_USD * max(1, len(proposals)),
            duration_seconds=time.time() - cycle_start,
        )
        self._log_experiment(result)
        return result

    def _run_community_cycle(self, cycle_start: float) -> ExperimentResult | None:
        """Process the highest-priority community-requested issue.

        Fetches open ``evolve-candidate`` / ``feature-request`` issues from
        GitHub, sorted by 👍 reaction count.  The top issue that passes the
        trust check (collaborator author or ``maintainer-approved`` label) is
        converted to a ``VisionaryProposal``, pushed through the analyst gate,
        and written to the backlog for the main orchestrator.

        Marks the issue as ``evolve-in-progress`` on start, and closes it
        when the backlog task is created successfully.

        Args:
            cycle_start: Unix timestamp when this cycle started.

        Returns:
            ExperimentResult summarising the community cycle, or None if no
            eligible community issues were found.
        """
        gh = self._gh
        if gh is None or not gh.available:
            logger.debug("Community cycle: GitHub unavailable — skipping")
            return None

        issues = gh.fetch_community_issues()
        if not issues:
            logger.debug("Community cycle: no eligible community issues found")
            return None

        # Pick the first issue that passes the trust check.
        selected = None
        for issue in issues:
            if issue.is_maintainer_approved:
                selected = issue
                break
            if issue.author and gh.check_is_collaborator(issue.author):
                selected = issue
                break

        if selected is None:
            logger.info(
                "Community cycle: %d issue(s) found but none passed trust check "
                "(no collaborator author and no maintainer-approved label)",
                len(issues),
            )
            return None

        logger.info(
            "Community cycle: processing issue #%d '%s' (%d 👍)",
            selected.number,
            selected.title,
            selected.thumbs_up,
        )

        # Mark in-progress so other instances skip this issue.
        gh.mark_in_progress(selected.number)

        # Convert the issue to a visionary proposal.
        proposal = issue_to_proposal(selected)

        # Run the analyst + production gate via the creative pipeline.
        # We create a synthetic AnalystVerdict that auto-approves community
        # issues with a reasonable baseline score.  The human can always
        # reject the resulting backlog task or PR.
        analyst_verdict = AnalystVerdict(
            proposal_title=proposal.title,
            verdict="APPROVE",
            feasibility_score=7.0,
            impact_score=8.0,
            risk_score=4.0,
            composite_score=AnalystVerdict.compute_composite(7.0, 8.0, 4.0),
            reasoning=(
                f"Community-requested feature from GitHub issue #{selected.number}. "
                f"Thumbs-up: {selected.thumbs_up}. Auto-approved for backlog creation."
            ),
            decomposition=[],
        )

        pipeline_result: PipelineResult = self._creative_pipeline.run(
            [proposal],
            [analyst_verdict],
        )

        tasks_created = len(pipeline_result.tasks_created)
        accepted = tasks_created > 0

        if accepted:
            logger.info(
                "Community cycle: created %d backlog task(s) for issue #%d",
                tasks_created,
                selected.number,
            )
            # Close the GitHub issue now that a backlog task exists.
            closing_comment = (
                "Bernstein has created a backlog task for this request. "
                "Implementation will be tracked internally.\n\n"
                "*Processed by `bernstein evolve run --community`*"
            )
            gh.close_issue(selected.number, comment=closing_comment)
        else:
            # Pipeline rejected or no tasks — unmark so it can be retried.
            gh.unmark_in_progress(selected.number)
            logger.info("Community cycle: pipeline produced no tasks for issue #%d", selected.number)

        result = ExperimentResult(
            proposal_id=f"community-{selected.number}",
            title=f"Community issue #{selected.number}: {selected.title}",
            risk_level=RiskLevel.L1_TEMPLATE.value,
            baseline_score=1.0,
            candidate_score=1.0 + (0.01 * tasks_created),
            delta=0.01 * tasks_created,
            accepted=accepted,
            reason=(f"{tasks_created} backlog task(s) created" if accepted else "Pipeline produced no tasks"),
            cost_usd=_COST_PER_PROPOSAL_USD,
            duration_seconds=time.time() - cycle_start,
        )
        self._log_experiment(result)
        return result

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
        except Exception as e:
            logger.exception("Baseline benchmark run failed — defaulting to 1.0: %s", e)
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
        eligible = [opp for opp in opportunities if opp.risk_level in ("low", "medium")]

        if not eligible:
            return None

        # Sort by confidence descending — pick the most promising.
        eligible.sort(key=lambda o: o.confidence, reverse=True)
        best = eligible[0]

        return self._proposal_generator.create_proposal(
            best,
            AnalysisTrigger.SCHEDULED,
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

        try:
            success = self._executor.execute_upgrade(proposal)
        except Exception as exc:
            raise ApplyError(
                str(exc),
                proposal_id=proposal.id,
                risk_level=risk_level.value,
            ) from exc

        if success:
            self._breaker.record_change(risk_level, proposal.id)
            logger.info("Proposal %s applied successfully", proposal.id)
        else:
            logger.warning("Proposal %s application failed — attempting rollback", proposal.id)
            try:
                self._executor.rollback_upgrade(proposal)
            except Exception as exc:
                raise RollbackError(
                    str(exc),
                    proposal_id=proposal.id,
                    risk_level=risk_level.value,
                ) from exc
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

    # ------------------------------------------------------------------
    # GitHub sync helpers
    # ------------------------------------------------------------------

    def _github_check_unclaimed(self) -> str | None:
        """Check GitHub for unclaimed evolution issues before generating.

        If an unclaimed issue exists, claim it and track its number so we
        can close it on success.  Returns the issue title as a hint (for
        logging purposes only — the proposal generator still runs normally).

        Returns:
            Title of the claimed issue, or None if none available or GitHub
            sync is disabled / unavailable.
        """
        gh = self._gh
        if gh is None or not gh.available:
            return None

        unclaimed = gh.find_unclaimed()
        if not unclaimed:
            return None

        issue = unclaimed[0]
        logger.info(
            "GitHub sync: claiming existing issue #%d '%s'",
            issue.number,
            issue.title,
        )
        claimed = gh.claim_issue(issue.number)
        if claimed:
            self._current_issue_number = issue.number
        return issue.title

    def _github_sync_proposal(self, title: str, description: str) -> None:
        """Publish a new proposal as a GitHub issue, or claim an existing one.

        If an issue with the same title hash already exists (from another
        instance), claim that issue rather than creating a duplicate.

        Args:
            title: Proposal title.
            description: Proposal description for the issue body.
        """
        gh = self._gh
        if gh is None or not gh.available:
            return

        # If we already claimed an issue in _github_check_unclaimed, skip.
        if self._current_issue_number is not None:
            return

        # Check for a duplicate by title hash.
        existing = gh.find_by_hash(title)
        if existing is not None:
            logger.info(
                "GitHub sync: duplicate detected — claiming existing issue #%d",
                existing.number,
            )
            if gh.claim_issue(existing.number):
                self._current_issue_number = existing.number
            return

        # No duplicate — create a new issue.
        body = (
            f"## Auto-generated evolution proposal\n\n"
            f"{description}\n\n"
            f"---\n"
            f"*Generated by `bernstein evolve run --github`*"
        )
        issue = gh.create_issue(title=title, body=body)
        if issue is not None:
            logger.info(
                "GitHub sync: created issue #%d for proposal '%s'",
                issue.number,
                title,
            )
            if gh.claim_issue(issue.number):
                self._current_issue_number = issue.number

    def _github_close_current(self, comment: str | None = None) -> None:
        """Close the currently tracked GitHub issue.

        Args:
            comment: Optional closing comment.
        """
        gh = self._gh
        if gh is None or self._current_issue_number is None:
            return
        closed = gh.close_issue(self._current_issue_number, comment=comment)
        if closed:
            logger.info(
                "GitHub sync: closed issue #%d",
                self._current_issue_number,
            )
        self._current_issue_number = None

    def _github_unclaim_current(self) -> None:
        """Unclaim the currently tracked GitHub issue.

        Called when the proposal is deferred or blocked so another instance
        can pick it up.
        """
        gh = self._gh
        if gh is None or self._current_issue_number is None:
            return
        gh.unclaim_issue(self._current_issue_number)
        logger.info(
            "GitHub sync: unclaimed issue #%d",
            self._current_issue_number,
        )
        self._current_issue_number = None

    # ------------------------------------------------------------------
    # Governance helpers
    # ------------------------------------------------------------------

    def _build_project_context(self) -> ProjectContext:
        """Build a ProjectContext snapshot for weight adjustment.

        Uses available loop state as a proxy for project health. Source file
        count is derived from the repo root (Python files, .git excluded).

        Returns:
            ProjectContext populated from current loop state.
        """
        try:
            src_files = sum(1 for p in self._repo_root.rglob("*.py") if ".git" not in p.parts)
        except OSError:
            src_files = 0
        return ProjectContext(
            cycle_number=self._cycle_count,
            test_pass_rate=0.9,  # optimistic default; updated by test runs
            lint_violations=0,
            security_issues_last_5_cycles=0,
            codebase_size_files=src_files,
            consecutive_empty_cycles=self._consecutive_empty,
        )

    def _compute_proposal_risk(self, proposal: UpgradeProposal) -> ProposalRiskScore:
        """Compute a composite risk score for a proposal via RiskScorer.

        Args:
            proposal: The upgrade proposal to score.

        Returns:
            ProposalRiskScore with all dimensions populated.
        """
        target_files = list(proposal.risk_assessment.affected_components)
        # Estimate diff size from proposed_change length (lines heuristic).
        diff_estimate = max(len(proposal.proposed_change) // 10, 10)
        return self._risk_scorer.score_proposal(
            target_files=target_files,
            diff_size=diff_estimate,
            test_coverage_delta=0.0,  # unknown pre-execution
        )

    @staticmethod
    def _classify_risk_route(composite_risk: float) -> str:
        """Map a composite risk score to a routing strategy.

        Thresholds:
          - composite_risk > 0.6 → ``sandbox_verify``  (forced sandbox)
          - composite_risk 0.3-0.6 → ``standard``       (normal flow)
          - composite_risk < 0.3 → ``fast_track``       (skip sandbox)

        Args:
            composite_risk: Composite risk score in [0.0, 1.0].

        Returns:
            One of ``"sandbox_verify"``, ``"standard"``, or ``"fast_track"``.
        """
        if composite_risk > 0.6:
            return "sandbox_verify"
        if composite_risk > 0.3:
            return "standard"
        return "fast_track"

    def _flush_governance_log(self) -> None:
        """Write accumulated per-cycle governance state to governance_log.jsonl."""
        self._governor.log_decision(
            GovernanceEntry(
                cycle=self._cycle_count,
                timestamp=datetime.now(UTC).isoformat(),
                weights_before=self._cycle_weights_before.to_dict(),
                weights_after=self._current_weights.to_dict(),
                weight_change_reason=self._cycle_weight_reason,
                proposals_evaluated=self._cycle_proposals_evaluated,
                proposals_applied=self._cycle_proposals_applied,
                risk_scores=self._cycle_risk_scores,
                outcome_metrics=self._cycle_outcome_metrics,
            )
        )

    def _make_fast_track_sandbox_result(
        self,
        proposal_id: str,
        baseline_score: float,
    ) -> SandboxResult:
        """Return a synthetic passed SandboxResult for fast-tracked proposals.

        Fast-tracked proposals (composite_risk < 0.3) skip sandbox validation.
        We create a neutral result so the apply path can proceed normally.

        Args:
            proposal_id: ID of the fast-tracked proposal.
            baseline_score: Current baseline benchmark score.

        Returns:
            A ``SandboxResult`` marked as passed with no test data.
        """
        return SandboxResult(
            proposal_id=proposal_id,
            passed=True,
            tests_passed=0,
            tests_failed=0,
            tests_total=0,
            baseline_score=baseline_score,
            candidate_score=baseline_score,
            delta=0.0,
            duration_seconds=0.0,
            log_path="",
        )

    # ------------------------------------------------------------------
    # Cycle helpers
    # ------------------------------------------------------------------

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
