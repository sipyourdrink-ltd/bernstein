"""Runner tests for the review pipeline DSL.

Covers:
* Single-stage parallel execution.
* Multi-stage propagation through the bulletin board.
* Janitor block-on-fail integration.
* HMAC audit per-stage breakdown.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.communication.bulletin import BulletinBoard
from bernstein.core.quality.review_pipeline import (
    AgentSpec,
    AggregatorConfig,
    DiffSource,
    ReviewPipeline,
    StageSpec,
    run_pipeline_sync,
    should_block_merge,
    to_cross_model_verdict,
)


def _diff_src(text: str = "+ added line") -> DiffSource:
    return DiffSource(title="t", description="d", diff=text)


def _approve_response(*, role: str = "r") -> str:
    return json.dumps({"verdict": "approve", "feedback": f"{role} ok", "issues": []})


def _reject_response(*, role: str = "r") -> str:
    return json.dumps(
        {
            "verdict": "request_changes",
            "feedback": f"{role} concerns",
            "issues": [f"issue from {role}"],
        }
    )


def _make_stub(behaviour: dict[str, str]) -> Any:
    """Build a fake LLM caller; ``behaviour`` maps model→raw response."""
    seen: list[str] = []

    async def caller(*, prompt: str, model: str, **_: object) -> str:
        seen.append(model)
        if model in behaviour:
            return behaviour[model]
        return _approve_response()

    caller.seen = seen  # type: ignore[attr-defined]
    return caller


class TestSingleStageParallel:
    def test_parallel_agents_all_approve(self) -> None:
        stage = StageSpec(
            name="cheap",
            parallelism=3,
            agents=[
                AgentSpec(role="lint", model="m1"),
                AgentSpec(role="tests", model="m2"),
                AgentSpec(role="security", model="m3"),
            ],
            aggregator=AggregatorConfig(strategy="majority"),
        )
        pipeline = ReviewPipeline(stages=[stage])
        caller = _make_stub({"m1": _approve_response(), "m2": _approve_response(), "m3": _approve_response()})
        verdict = run_pipeline_sync(pipeline, _diff_src(), llm_caller=caller)
        assert verdict.verdict == "approve"
        assert len(verdict.stages) == 1
        assert verdict.stages[0].approve_count == 3

    def test_parallelism_capped_by_agents(self) -> None:
        stage = StageSpec(
            name="s",
            parallelism=8,  # more than agents
            agents=[AgentSpec(role="a", model="m1")],
            aggregator=AggregatorConfig(strategy="any"),
        )
        pipeline = ReviewPipeline(stages=[stage])
        caller = _make_stub({"m1": _approve_response()})
        verdict = run_pipeline_sync(pipeline, _diff_src(), llm_caller=caller)
        assert verdict.verdict == "approve"

    def test_llm_failure_defaults_approve(self) -> None:
        async def caller(*, prompt: str, model: str, **_: object) -> str:
            raise RuntimeError("boom")

        stage = StageSpec(
            name="s",
            parallelism=1,
            agents=[AgentSpec(role="r", model="m")],
            aggregator=AggregatorConfig(strategy="any"),
        )
        pipeline = ReviewPipeline(stages=[stage])
        verdict = run_pipeline_sync(pipeline, _diff_src(), llm_caller=caller)
        assert verdict.verdict == "approve"
        assert verdict.stages[0].agents[0].confidence == 0.0


class TestMultiStagePropagation:
    def test_second_stage_sees_first_stage_findings(self) -> None:
        captured_prompts: list[str] = []

        async def caller(*, prompt: str, model: str, **_: object) -> str:
            captured_prompts.append(prompt)
            if model == "stage1-model":
                return _reject_response(role="lint")
            return _approve_response(role="senior")

        s1 = StageSpec(
            name="cheap",
            parallelism=1,
            agents=[AgentSpec(role="lint", model="stage1-model")],
            aggregator=AggregatorConfig(strategy="any"),
        )
        s2 = StageSpec(
            name="senior",
            parallelism=1,
            agents=[AgentSpec(role="senior", model="stage2-model")],
            aggregator=AggregatorConfig(strategy="any"),
        )
        pipeline = ReviewPipeline(stages=[s1, s2], pass_threshold=0.5)
        board = BulletinBoard()
        verdict = run_pipeline_sync(pipeline, _diff_src(), llm_caller=caller, bulletin=board)

        # Stage 2's prompt must include stage 1's findings.
        s2_prompt = captured_prompts[1]
        assert "Prior stage findings" in s2_prompt
        assert "cheap" in s2_prompt
        assert "lint" in s2_prompt

        # Bulletin board got the stage findings posted.
        msgs = [m.content for m in board.read_since(0)]
        assert any("[cheap/lint]" in m for m in msgs)

        # Pipeline majority: 1 fail + 1 approve under threshold 0.5 → approve.
        assert verdict.pass_score == pytest.approx(0.5)


class TestSupersetRule:
    def test_one_stage_one_agent_matches_legacy(self) -> None:
        """Single-stage, single-agent pipeline reproduces single-pass verifier."""
        stage = StageSpec(
            name="legacy",
            parallelism=1,
            agents=[AgentSpec(role="reviewer", model="anthropic/claude-haiku-4-5-20251001")],
            aggregator=AggregatorConfig(strategy="any"),
        )
        pipeline = ReviewPipeline(stages=[stage])
        caller = _make_stub({"anthropic/claude-haiku-4-5-20251001": _approve_response()})
        verdict = run_pipeline_sync(pipeline, _diff_src(), llm_caller=caller)
        assert verdict.verdict == "approve"

        legacy = to_cross_model_verdict(verdict)
        assert legacy.verdict == "approve"
        assert legacy.reviewer_model == "anthropic/claude-haiku-4-5-20251001"


class TestJanitorBlockOnFail:
    def test_block_on_failing_pipeline(self) -> None:
        stage = StageSpec(
            name="s",
            parallelism=1,
            agents=[AgentSpec(role="r", model="m")],
            aggregator=AggregatorConfig(strategy="all"),
        )
        pipeline = ReviewPipeline(stages=[stage], block_on_fail=True)
        caller = _make_stub({"m": _reject_response()})
        verdict = run_pipeline_sync(pipeline, _diff_src(), llm_caller=caller)
        assert verdict.verdict == "request_changes"
        assert should_block_merge(verdict) is True

        legacy = to_cross_model_verdict(verdict)
        assert legacy.verdict == "request_changes"
        assert legacy.issues  # populated from issues

    def test_no_block_when_disabled(self) -> None:
        stage = StageSpec(
            name="s",
            parallelism=1,
            agents=[AgentSpec(role="r", model="m")],
            aggregator=AggregatorConfig(strategy="all"),
        )
        pipeline = ReviewPipeline(stages=[stage], block_on_fail=False)
        caller = _make_stub({"m": _reject_response()})
        verdict = run_pipeline_sync(pipeline, _diff_src(), llm_caller=caller)
        assert should_block_merge(verdict) is False


class TestHMACAuditBreakdown:
    def test_audit_log_records_each_stage(self, tmp_path: Path) -> None:
        from bernstein.core.security.audit import AuditLog

        # Provide an explicit key so we avoid disk-permission probes.
        key_path = tmp_path / "audit.key"
        key_path.write_text("00" * 32)
        key_path.chmod(0o600)
        log = AuditLog(audit_dir=tmp_path / "audit", key_path=key_path)

        s1 = StageSpec(
            name="cheap",
            parallelism=1,
            agents=[AgentSpec(role="lint", model="m1")],
            aggregator=AggregatorConfig(strategy="any"),
        )
        s2 = StageSpec(
            name="senior",
            parallelism=1,
            agents=[AgentSpec(role="senior", model="m2")],
            aggregator=AggregatorConfig(strategy="any"),
        )
        pipeline = ReviewPipeline(stages=[s1, s2])
        caller = _make_stub({"m1": _approve_response(), "m2": _approve_response()})
        run_pipeline_sync(
            pipeline,
            DiffSource(title="t", description="d", diff="+x", pr_number=42),
            llm_caller=caller,
            audit_log=log,
        )

        events: list[dict[str, Any]] = []
        for f in sorted((tmp_path / "audit").glob("*.jsonl")):
            for line in f.read_text().splitlines():
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        types = [e["event_type"] for e in events]
        assert "review_pipeline.start" in types
        assert types.count("review_pipeline.stage") == 2
        assert "review_pipeline.complete" in types

        # Stage rows carry stage-level breakdown.
        stage_rows = [e for e in events if e["event_type"] == "review_pipeline.stage"]
        names = sorted(r["details"]["stage"] for r in stage_rows)
        assert names == ["cheap", "senior"]
        assert all("agents" in r["details"] and isinstance(r["details"]["agents"], list) for r in stage_rows)
