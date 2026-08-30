"""YAML-driven multi-phase review pipeline DSL.

A ``review.yaml`` declares an ordered list of stages.  Each stage runs N
agents in parallel; stage outputs are forwarded to the next stage's
context via the bulletin board.  The pipeline's final verdict plugs into
the existing janitor gate so a failed review blocks merge - same UX as
the legacy single-pass cross-model verifier, but generalised.

Public API:

* :class:`ReviewPipeline` / :class:`StageSpec` / :class:`AgentSpec`
* :func:`load_pipeline` / :func:`parse_pipeline_yaml`
* :class:`AgentVerdict` / :class:`StageVerdict` / :class:`PipelineVerdict`
* :func:`run_pipeline` / :func:`run_pipeline_sync`
* :func:`should_block_merge` / :func:`to_cross_model_verdict`
* :class:`ReviewRuleset` / :func:`load_ruleset` - the raise and guard rules a
  verdict is produced under, and the digest that names them.
* :func:`run_review_contour` - the review -> fix -> re-check loop, its bounded
  budget, and one chained review receipt per pass.
"""

from __future__ import annotations

from bernstein.core.quality.review_pipeline.ast_chunker import (
    ReviewChunk,
    chunk_for_review,
)
from bernstein.core.quality.review_pipeline.contour import (
    CheckLogExcerpt,
    CheckRollup,
    CheckRun,
    CheckState,
    ContourOutcome,
    ContourResult,
    FixOutcome,
    FixRequest,
    FixRunner,
    PassReceiptEmitter,
    PassReceiptRequest,
    PassRecord,
    check_log_fetcher,
    command_fix_runner,
    receipt_emitter,
    rollup_from_payload,
    run_review_contour,
    wait_for_checks,
)
from bernstein.core.quality.review_pipeline.review_gate import (
    EvalGateConfigError,
    FreshContextViolation,
    ImplementerContext,
    ModelSelection,
    ReviewGate,
    ReviewInputs,
    ReviewState,
    ReviewVerdict,
    parse_structured_verdict,
)
from bernstein.core.quality.review_pipeline.ruleset import (
    EMPTY_RULESET,
    ReviewRule,
    ReviewRuleset,
    ReviewRulesetError,
    RulesSpec,
    load_ruleset,
    parse_ruleset,
)
from bernstein.core.quality.review_pipeline.runner import (
    DiffSource,
    check_rollup_from_pr,
    diff_from_pr,
    diff_from_task,
    gh_pr_view_json,
    run_pipeline,
    run_pipeline_sync,
    should_block_merge,
    to_cross_model_verdict,
)
from bernstein.core.quality.review_pipeline.schema import (
    DEFAULT_PASS_THRESHOLD,
    AgentSpec,
    AggregatorConfig,
    AggregatorStrategy,
    EffortLevel,
    ReviewPipeline,
    ReviewPipelineError,
    StageSpec,
    load_pipeline,
    parse_pipeline_yaml,
)
from bernstein.core.quality.review_pipeline.scope import (
    ScopeResolution,
    compute_resolution_hash,
    resolve_scope,
)
from bernstein.core.quality.review_pipeline.verdict import (
    AgentVerdict,
    FinalVerdict,
    PipelineVerdict,
    StageVerdict,
    aggregate_pipeline,
    aggregate_stage,
)

__all__ = [
    "DEFAULT_PASS_THRESHOLD",
    "EMPTY_RULESET",
    "AgentSpec",
    "AgentVerdict",
    "AggregatorConfig",
    "AggregatorStrategy",
    "CheckLogExcerpt",
    "CheckRollup",
    "CheckRun",
    "CheckState",
    "ContourOutcome",
    "ContourResult",
    "DiffSource",
    "EffortLevel",
    "EvalGateConfigError",
    "FinalVerdict",
    "FixOutcome",
    "FixRequest",
    "FixRunner",
    "FreshContextViolation",
    "ImplementerContext",
    "ModelSelection",
    "PassReceiptEmitter",
    "PassReceiptRequest",
    "PassRecord",
    "PipelineVerdict",
    "ReviewChunk",
    "ReviewGate",
    "ReviewInputs",
    "ReviewPipeline",
    "ReviewPipelineError",
    "ReviewRule",
    "ReviewRuleset",
    "ReviewRulesetError",
    "ReviewState",
    "ReviewVerdict",
    "RulesSpec",
    "ScopeResolution",
    "StageSpec",
    "StageVerdict",
    "aggregate_pipeline",
    "aggregate_stage",
    "check_log_fetcher",
    "check_rollup_from_pr",
    "chunk_for_review",
    "command_fix_runner",
    "compute_resolution_hash",
    "diff_from_pr",
    "diff_from_task",
    "gh_pr_view_json",
    "load_pipeline",
    "load_ruleset",
    "parse_pipeline_yaml",
    "parse_ruleset",
    "parse_structured_verdict",
    "receipt_emitter",
    "resolve_scope",
    "rollup_from_payload",
    "run_pipeline",
    "run_pipeline_sync",
    "run_review_contour",
    "should_block_merge",
    "to_cross_model_verdict",
    "wait_for_checks",
]
