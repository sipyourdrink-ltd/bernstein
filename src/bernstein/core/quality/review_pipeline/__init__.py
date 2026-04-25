"""YAML-driven multi-phase review pipeline DSL.

A ``review.yaml`` declares an ordered list of stages.  Each stage runs N
agents in parallel; stage outputs are forwarded to the next stage's
context via the bulletin board.  The pipeline's final verdict plugs into
the existing janitor gate so a failed review blocks merge — same UX as
the legacy single-pass cross-model verifier, but generalised.

Public API:

* :class:`ReviewPipeline` / :class:`StageSpec` / :class:`AgentSpec`
* :func:`load_pipeline` / :func:`parse_pipeline_yaml`
* :class:`AgentVerdict` / :class:`StageVerdict` / :class:`PipelineVerdict`
* :func:`run_pipeline` / :func:`run_pipeline_sync`
* :func:`should_block_merge` / :func:`to_cross_model_verdict`
"""

from __future__ import annotations

from bernstein.core.quality.review_pipeline.runner import (
    DiffSource,
    diff_from_pr,
    diff_from_task,
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
    "AgentSpec",
    "AgentVerdict",
    "AggregatorConfig",
    "AggregatorStrategy",
    "DiffSource",
    "EffortLevel",
    "FinalVerdict",
    "PipelineVerdict",
    "ReviewPipeline",
    "ReviewPipelineError",
    "StageSpec",
    "StageVerdict",
    "aggregate_pipeline",
    "aggregate_stage",
    "diff_from_pr",
    "diff_from_task",
    "load_pipeline",
    "parse_pipeline_yaml",
    "run_pipeline",
    "run_pipeline_sync",
    "should_block_merge",
    "to_cross_model_verdict",
]
