"""Aggregator-strategy tests for the review pipeline DSL."""

from __future__ import annotations

import pytest

from bernstein.core.quality.review_pipeline import (
    AgentSpec,
    AgentVerdict,
    AggregatorConfig,
    ReviewPipeline,
    StageSpec,
    aggregate_pipeline,
    aggregate_stage,
)


def _stage(strategy: str = "majority", *, threshold: float | None = None) -> StageSpec:
    return StageSpec(
        name="t",
        parallelism=1,
        agents=[AgentSpec(role="r")],
        aggregator=AggregatorConfig(strategy=strategy, pass_threshold=threshold),  # type: ignore[arg-type]
    )


def _pipeline(stage: StageSpec, *, threshold: float = 0.5) -> ReviewPipeline:
    return ReviewPipeline(stages=[stage], pass_threshold=threshold)


def _v(role: str, verdict: str, model: str = "m") -> AgentVerdict:
    return AgentVerdict(role=role, model=model, verdict=verdict)  # type: ignore[arg-type]


class TestStageStrategies:
    def test_any_one_approve_passes(self) -> None:
        stage = _stage("any")
        sv = aggregate_stage(stage, [_v("a", "approve"), _v("b", "request_changes")], _pipeline(stage))
        assert sv.verdict == "approve"
        assert sv.approve_count == 1
        assert sv.total_count == 2

    def test_any_zero_approves_fails(self) -> None:
        stage = _stage("any")
        sv = aggregate_stage(stage, [_v("a", "request_changes"), _v("b", "request_changes")], _pipeline(stage))
        assert sv.verdict == "request_changes"

    def test_all_requires_unanimous(self) -> None:
        stage = _stage("all")
        sv = aggregate_stage(stage, [_v("a", "approve"), _v("b", "approve")], _pipeline(stage))
        assert sv.verdict == "approve"
        sv2 = aggregate_stage(stage, [_v("a", "approve"), _v("b", "request_changes")], _pipeline(stage))
        assert sv2.verdict == "request_changes"

    def test_majority(self) -> None:
        stage = _stage("majority")
        sv = aggregate_stage(
            stage,
            [_v("a", "approve"), _v("b", "approve"), _v("c", "request_changes")],
            _pipeline(stage),
        )
        assert sv.verdict == "approve"
        sv2 = aggregate_stage(
            stage,
            [_v("a", "approve"), _v("b", "request_changes"), _v("c", "request_changes")],
            _pipeline(stage),
        )
        assert sv2.verdict == "request_changes"

    def test_majority_tie_rejects(self) -> None:
        stage = _stage("majority")
        sv = aggregate_stage(
            stage,
            [_v("a", "approve"), _v("b", "request_changes")],
            _pipeline(stage),
        )
        assert sv.verdict == "request_changes"

    def test_weighted_uses_role_weights(self) -> None:
        stage = StageSpec(
            name="w",
            parallelism=1,
            agents=[AgentSpec(role="lint"), AgentSpec(role="security")],
            aggregator=AggregatorConfig(
                strategy="weighted",
                weights={"lint": 0.2, "security": 0.8},
                pass_threshold=0.5,
            ),
        )
        # security approve dominates
        sv = aggregate_stage(stage, [_v("lint", "request_changes"), _v("security", "approve")], _pipeline(stage))
        assert sv.verdict == "approve"
        assert sv.pass_score == pytest.approx(0.8)
        # lint approve alone is not enough
        sv2 = aggregate_stage(stage, [_v("lint", "approve"), _v("security", "request_changes")], _pipeline(stage))
        assert sv2.verdict == "request_changes"
        assert sv2.pass_score == pytest.approx(0.2)


class TestPipelineAggregation:
    def test_single_stage_single_agent_byte_for_byte(self) -> None:
        """1-stage / 1-agent / strategy=any reproduces today's verifier."""
        stage = _stage("any")
        pipeline = _pipeline(stage)
        sv = aggregate_stage(stage, [_v("r", "approve")], pipeline)
        pv = aggregate_pipeline(pipeline, [sv])
        assert pv.verdict == "approve"
        assert pv.pass_score == 1.0

        sv_bad = aggregate_stage(stage, [_v("r", "request_changes")], pipeline)
        pv_bad = aggregate_pipeline(pipeline, [sv_bad])
        assert pv_bad.verdict == "request_changes"

    def test_pipeline_threshold(self) -> None:
        s1 = StageSpec(name="s1", agents=[AgentSpec(role="r")])
        s2 = StageSpec(name="s2", agents=[AgentSpec(role="r")])
        s3 = StageSpec(name="s3", agents=[AgentSpec(role="r")])
        pipeline = ReviewPipeline(stages=[s1, s2, s3], pass_threshold=0.66)

        good = aggregate_stage(s1, [_v("r", "approve")], pipeline)
        good2 = aggregate_stage(s2, [_v("r", "approve")], pipeline)
        bad = aggregate_stage(s3, [_v("r", "request_changes")], pipeline)
        pv = aggregate_pipeline(pipeline, [good, good2, bad])
        assert pv.verdict == "approve"
        assert pv.pass_score == pytest.approx(2 / 3)

        # All fail → reject
        bad1 = aggregate_stage(s1, [_v("r", "request_changes")], pipeline)
        bad2 = aggregate_stage(s2, [_v("r", "request_changes")], pipeline)
        bad3 = aggregate_stage(s3, [_v("r", "request_changes")], pipeline)
        pv2 = aggregate_pipeline(pipeline, [bad1, bad2, bad3])
        assert pv2.verdict == "request_changes"

    def test_block_on_fail_propagates(self) -> None:
        from bernstein.core.quality.review_pipeline import should_block_merge

        stage = _stage("any")
        pipeline = ReviewPipeline(stages=[stage], block_on_fail=True)
        sv = aggregate_stage(stage, [_v("r", "request_changes")], pipeline)
        pv = aggregate_pipeline(pipeline, [sv])
        assert should_block_merge(pv) is True

        pipeline_off = ReviewPipeline(stages=[stage], block_on_fail=False)
        sv2 = aggregate_stage(stage, [_v("r", "request_changes")], pipeline_off)
        pv2 = aggregate_pipeline(pipeline_off, [sv2])
        assert should_block_merge(pv2) is False
