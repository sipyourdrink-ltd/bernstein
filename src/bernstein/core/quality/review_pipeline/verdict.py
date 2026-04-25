"""Aggregation rules for the review pipeline.

A stage produces one :class:`AgentVerdict` per agent; the stage's
:class:`AggregatorConfig` decides how to fold them into a
:class:`StageVerdict`.  The pipeline then folds stage verdicts into a
final :class:`PipelineVerdict`.

Strict-superset rule: a 1-stage / 1-agent pipeline using ``strategy=any``
reproduces today's single-pass cross-model verifier behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from bernstein.core.quality.review_pipeline.schema import (
    DEFAULT_PASS_THRESHOLD,
    AggregatorConfig,
    AggregatorStrategy,
    ReviewPipeline,
    StageSpec,
)

#: Final verdict literal — matches CrossModelVerdict for drop-in compatibility.
FinalVerdict = Literal["approve", "request_changes"]


@dataclass(frozen=True)
class AgentVerdict:
    """Verdict returned by a single agent in a stage.

    Attributes:
        role: Agent role (mirrors :attr:`AgentSpec.role`).
        model: Model identifier the agent ran with.
        verdict: ``approve`` or ``request_changes``.
        feedback: One-or-two-sentence rationale.
        issues: Specific issues (empty when approved).
        confidence: Self-reported 0.0-1.0 confidence; defaults to 1.0 when
            the underlying reviewer does not return one.
    """

    role: str
    model: str
    verdict: FinalVerdict
    feedback: str = ""
    issues: list[str] = field(default_factory=list[str])
    confidence: float = 1.0


@dataclass(frozen=True)
class StageVerdict:
    """Aggregated verdict for one stage.

    Attributes:
        stage: Stage name.
        verdict: ``approve`` or ``request_changes``.
        approve_count: Number of agents that approved.
        total_count: Total agents that voted (excludes adapter failures
            already filtered upstream — those default to ``approve``).
        pass_score: Fraction of approval weight, in [0.0, 1.0].
        agents: Per-agent verdicts, in stage spawn order.
        feedback: Short summary of how the verdict was reached.
    """

    stage: str
    verdict: FinalVerdict
    approve_count: int
    total_count: int
    pass_score: float
    agents: list[AgentVerdict]
    feedback: str = ""


@dataclass(frozen=True)
class PipelineVerdict:
    """Final pipeline verdict.

    Attributes:
        verdict: ``approve`` or ``request_changes``.
        feedback: Short pipeline-level summary.
        pass_score: Fraction of stages that passed, in [0.0, 1.0].
        stages: All stage verdicts in execution order.
        block_on_fail: Mirrors :attr:`ReviewPipeline.block_on_fail` — drives
            janitor block-on-fail behaviour.
    """

    verdict: FinalVerdict
    feedback: str
    pass_score: float
    stages: list[StageVerdict]
    block_on_fail: bool = True

    @property
    def issues(self) -> list[str]:
        """Flattened list of issues across every failing agent."""
        out: list[str] = []
        for stage in self.stages:
            for agent in stage.agents:
                if agent.verdict == "request_changes":
                    for issue in agent.issues:
                        out.append(f"{stage.stage}/{agent.role}: {issue}")
                    if not agent.issues and agent.feedback:
                        out.append(f"{stage.stage}/{agent.role}: {agent.feedback}")
        return out

    @property
    def reviewer_models(self) -> list[str]:
        """Distinct list of reviewer model identifiers used across the run."""
        seen: list[str] = []
        for stage in self.stages:
            for agent in stage.agents:
                if agent.model and agent.model not in seen:
                    seen.append(agent.model)
        return seen


def _stage_pass_threshold(stage: StageSpec, pipeline: ReviewPipeline) -> float:
    if stage.aggregator.pass_threshold is not None:
        return stage.aggregator.pass_threshold
    if pipeline.pass_threshold is not None:
        return pipeline.pass_threshold
    return DEFAULT_PASS_THRESHOLD


def aggregate_stage(
    stage: StageSpec,
    agent_verdicts: list[AgentVerdict],
    pipeline: ReviewPipeline,
) -> StageVerdict:
    """Fold per-agent verdicts into a single stage verdict.

    Args:
        stage: The stage specification.
        agent_verdicts: One verdict per agent (in spawn order).
        pipeline: The owning pipeline (for default thresholds).

    Returns:
        :class:`StageVerdict`.
    """
    total = len(agent_verdicts)
    approves = sum(1 for v in agent_verdicts if v.verdict == "approve")
    threshold = _stage_pass_threshold(stage, pipeline)
    score, verdict = _apply_strategy(
        stage.aggregator.strategy,
        agent_verdicts,
        stage.aggregator,
        threshold,
    )
    feedback = _summarise_stage(stage.name, verdict, approves, total, score)
    return StageVerdict(
        stage=stage.name,
        verdict=verdict,
        approve_count=approves,
        total_count=total,
        pass_score=score,
        agents=list(agent_verdicts),
        feedback=feedback,
    )


def _apply_strategy(
    strategy: AggregatorStrategy,
    verdicts: list[AgentVerdict],
    cfg: AggregatorConfig,
    pass_threshold: float,
) -> tuple[float, FinalVerdict]:
    """Apply *strategy* and return ``(pass_score, verdict)``.

    Pass score is always the fraction of *approve weight* over total weight.
    The verdict reflects the strategy-specific decision rule.
    """
    if not verdicts:
        # No agents → nothing to gate; default to approve so missing config
        # never blocks merges.  Pass score = 1.0 to reflect that.
        return 1.0, "approve"

    total = len(verdicts)
    approves = sum(1 for v in verdicts if v.verdict == "approve")

    if strategy == "any":
        score = approves / total
        verdict: FinalVerdict = "approve" if approves >= 1 else "request_changes"
        return score, verdict

    if strategy == "all":
        score = approves / total
        verdict = "approve" if approves == total else "request_changes"
        return score, verdict

    if strategy == "majority":
        score = approves / total
        # Strict majority — half is not enough, matches "more approve than
        # reject" semantics; ties fall to request_changes (safe default).
        verdict = "approve" if approves > total - approves else "request_changes"
        return score, verdict

    # weighted
    return _weighted(verdicts, cfg, pass_threshold)


def _weighted(
    verdicts: list[AgentVerdict],
    cfg: AggregatorConfig,
    pass_threshold: float,
) -> tuple[float, FinalVerdict]:
    """Weighted aggregation: sum approve weight, compare to threshold."""
    total_weight = 0.0
    approve_weight = 0.0
    for v in verdicts:
        weight = _weight_for(v, cfg.weights)
        total_weight += weight
        if v.verdict == "approve":
            approve_weight += weight
    if total_weight == 0.0:
        # No weights match → fall back to fraction of approvals.
        approves = sum(1 for v in verdicts if v.verdict == "approve")
        score = approves / len(verdicts)
    else:
        score = approve_weight / total_weight
    verdict: FinalVerdict = "approve" if score >= pass_threshold else "request_changes"
    return score, verdict


def _weight_for(verdict: AgentVerdict, weights: dict[str, float]) -> float:
    """Look up a weight by role first, then by model. Default 1.0 when absent."""
    if verdict.role in weights:
        return weights[verdict.role]
    if verdict.model in weights:
        return weights[verdict.model]
    # When the user supplied weights at all but this voter is absent, treat
    # it as un-weighted (1.0) so missing entries do not silently zero out.
    return 1.0


def _summarise_stage(
    name: str,
    verdict: FinalVerdict,
    approves: int,
    total: int,
    score: float,
) -> str:
    """Compose a short human-readable line for a stage verdict."""
    state = "approved" if verdict == "approve" else "request_changes"
    return f"stage {name!r} {state} ({approves}/{total} agents, score={score:.2f})"


def aggregate_pipeline(
    pipeline: ReviewPipeline,
    stage_verdicts: list[StageVerdict],
) -> PipelineVerdict:
    """Fold stage verdicts into a final pipeline verdict.

    A pipeline approves when the fraction of approving stages meets the
    pipeline-level ``pass_threshold``.  When ``pass_threshold`` is the
    default (0.5) and exactly one stage runs, this matches today's
    single-pass verifier semantics.

    Args:
        pipeline: The pipeline spec.
        stage_verdicts: Stage verdicts in execution order.

    Returns:
        :class:`PipelineVerdict`.
    """
    if not stage_verdicts:
        return PipelineVerdict(
            verdict="approve",
            feedback="no stages ran",
            pass_score=1.0,
            stages=[],
            block_on_fail=pipeline.block_on_fail,
        )

    total = len(stage_verdicts)
    passed = sum(1 for s in stage_verdicts if s.verdict == "approve")
    score = passed / total

    # A stage configured with strategy=="all" is treated as a hard gate:
    # if it fails, the pipeline fails regardless of upstream tolerance.
    # This matches operator intent for the "final_gate" pattern (a single
    # authoritative reviewer that must approve).
    hard_gate_failure = any(
        s.verdict == "request_changes" and pipeline.stages[idx].aggregator.strategy == "all"
        for idx, s in enumerate(stage_verdicts)
    )

    score_pass = score >= pipeline.pass_threshold

    if hard_gate_failure or not score_pass:
        verdict: FinalVerdict = "request_changes"
    else:
        verdict = "approve"

    feedback = (
        f"pipeline {verdict} ({passed}/{total} stages passed, "
        f"score={score:.2f}, threshold={pipeline.pass_threshold:.2f})"
    )
    return PipelineVerdict(
        verdict=verdict,
        feedback=feedback,
        pass_score=score,
        stages=list(stage_verdicts),
        block_on_fail=pipeline.block_on_fail,
    )
