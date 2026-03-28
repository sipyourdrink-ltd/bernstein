"""Tests for the creative evolution pipeline (visionary → analyst → production gate)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.evolution.creative import (
    AnalystVerdict,
    CreativePipeline,
    PipelineResult,
    VisionaryProposal,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sdd(tmp_path: Path) -> Path:
    """Temporary .sdd directory with required subdirectories."""
    sdd = tmp_path / ".sdd"
    (sdd / "backlog" / "open").mkdir(parents=True)
    (sdd / "backlog" / "done").mkdir(parents=True)
    (sdd / "evolution" / "creative").mkdir(parents=True)
    return sdd


@pytest.fixture
def pipeline(sdd: Path) -> CreativePipeline:
    return CreativePipeline(state_dir=sdd, repo_root=sdd.parent)


@pytest.fixture
def sample_proposals() -> list[VisionaryProposal]:
    return [
        VisionaryProposal(
            title="Live agent replay",
            why="Developers can't debug agent decisions after the fact",
            what="Record and replay agent sessions with full tool call history",
            impact="10x faster debugging of failed agent runs",
            risk="Storage overhead for large sessions",
            effort_estimate="L",
        ),
        VisionaryProposal(
            title="One-click rollback",
            why="When an agent breaks something, reverting is manual and scary",
            what="Atomic rollback of all changes from a single agent session",
            impact="Fearless experimentation — users try bolder tasks",
            risk="Git history pollution with rollback commits",
            effort_estimate="M",
        ),
        VisionaryProposal(
            title="Agent marketplace",
            why="Users are limited to built-in roles",
            what="Discover and install community-created specialist agents",
            impact="Ecosystem effect — community builds what we can't",
            risk="Quality control, security of community agents",
            effort_estimate="L",
        ),
    ]


@pytest.fixture
def approved_verdict() -> AnalystVerdict:
    return AnalystVerdict(
        proposal_title="Live agent replay",
        verdict="APPROVE",
        feasibility_score=8,
        impact_score=9,
        risk_score=3,
        composite_score=7.75,
        reasoning="High impact, technically feasible with existing tool-call logging.",
        decomposition=[
            "Add session recording to spawner",
            "Build replay viewer CLI command",
            "Add storage cleanup policy",
        ],
    )


@pytest.fixture
def rejected_verdict() -> AnalystVerdict:
    return AnalystVerdict(
        proposal_title="Agent marketplace",
        verdict="REJECT",
        feasibility_score=5,
        impact_score=7,
        risk_score=8,
        composite_score=2.8,
        reasoning="Too early — need stable plugin API first.",
    )


@pytest.fixture
def revised_verdict() -> AnalystVerdict:
    return AnalystVerdict(
        proposal_title="One-click rollback",
        verdict="REVISE",
        feasibility_score=7,
        impact_score=8,
        risk_score=4,
        composite_score=6.5,
        reasoning="Good idea but scope too broad. Start with single-file rollback.",
        revisions=["Limit to single-file rollback first", "Add confirmation prompt"],
    )


# ---------------------------------------------------------------------------
# VisionaryProposal tests
# ---------------------------------------------------------------------------


class TestVisionaryProposal:
    def test_to_dict_roundtrip(self, sample_proposals: list[VisionaryProposal]) -> None:
        for p in sample_proposals:
            d = p.to_dict()
            restored = VisionaryProposal.from_dict(d)
            assert restored.title == p.title
            assert restored.why == p.why
            assert restored.effort_estimate == p.effort_estimate

    def test_from_dict_invalid_effort_defaults_to_m(self) -> None:
        raw = {
            "title": "Test",
            "why": "Why",
            "what": "What",
            "impact": "Impact",
            "risk": "Risk",
            "effort_estimate": "XL",
        }
        p = VisionaryProposal.from_dict(raw)
        assert p.effort_estimate == "M"

    def test_from_dict_missing_effort_defaults_to_m(self) -> None:
        raw = {
            "title": "Test",
            "why": "Why",
            "what": "What",
            "impact": "Impact",
            "risk": "Risk",
        }
        p = VisionaryProposal.from_dict(raw)
        assert p.effort_estimate == "M"


# ---------------------------------------------------------------------------
# AnalystVerdict tests
# ---------------------------------------------------------------------------


class TestAnalystVerdict:
    def test_to_dict_roundtrip(self, approved_verdict: AnalystVerdict) -> None:
        d = approved_verdict.to_dict()
        restored = AnalystVerdict.from_dict(d)
        assert restored.verdict == "APPROVE"
        assert restored.composite_score == approved_verdict.composite_score
        assert restored.decomposition == approved_verdict.decomposition

    def test_from_dict_invalid_verdict_defaults_to_reject(self) -> None:
        raw = {
            "proposal_title": "Test",
            "verdict": "MAYBE",
            "feasibility_score": 5,
            "impact_score": 5,
            "risk_score": 5,
            "composite_score": 5.0,
            "reasoning": "Unclear",
        }
        v = AnalystVerdict.from_dict(raw)
        assert v.verdict == "REJECT"

    def test_compute_composite_high_feasibility_high_impact_low_risk(self) -> None:
        score = AnalystVerdict.compute_composite(
            feasibility=9,
            impact=9,
            risk=2,
        )
        assert score == pytest.approx(8.5, abs=0.1)

    def test_compute_composite_low_scores(self) -> None:
        score = AnalystVerdict.compute_composite(
            feasibility=2,
            impact=2,
            risk=9,
        )
        assert score == pytest.approx(0.0, abs=0.5)

    def test_compute_composite_clamped_to_zero(self) -> None:
        score = AnalystVerdict.compute_composite(
            feasibility=1,
            impact=1,
            risk=10,
        )
        assert score >= 0.0

    def test_compute_composite_clamped_to_ten(self) -> None:
        score = AnalystVerdict.compute_composite(
            feasibility=10,
            impact=10,
            risk=0,
        )
        assert score <= 10.0


# ---------------------------------------------------------------------------
# CreativePipeline tests
# ---------------------------------------------------------------------------


class TestCreativePipeline:
    def test_filter_approved_returns_only_approved_above_threshold(
        self,
        pipeline: CreativePipeline,
        approved_verdict: AnalystVerdict,
        rejected_verdict: AnalystVerdict,
        revised_verdict: AnalystVerdict,
    ) -> None:
        verdicts = [approved_verdict, rejected_verdict, revised_verdict]
        approved = pipeline.filter_approved(verdicts)
        assert len(approved) == 1
        assert approved[0].proposal_title == "Live agent replay"

    def test_filter_approved_rejects_approve_below_threshold(
        self,
        sdd: Path,
    ) -> None:
        pipeline = CreativePipeline(state_dir=sdd, approval_threshold=9.0)
        verdict = AnalystVerdict(
            proposal_title="Low score",
            verdict="APPROVE",
            feasibility_score=7,
            impact_score=7,
            risk_score=5,
            composite_score=5.75,
            reasoning="Not good enough.",
        )
        assert pipeline.filter_approved([verdict]) == []

    def test_run_creates_backlog_tasks(
        self,
        pipeline: CreativePipeline,
        sample_proposals: list[VisionaryProposal],
        approved_verdict: AnalystVerdict,
        rejected_verdict: AnalystVerdict,
    ) -> None:
        result = pipeline.run(
            sample_proposals,
            [approved_verdict, rejected_verdict],
        )
        assert isinstance(result, PipelineResult)
        assert len(result.approved) == 1
        # 3 decomposition items = 3 tasks
        assert len(result.tasks_created) == 3
        for path in result.tasks_created:
            assert path.exists()
            content = path.read_text(encoding="utf-8")
            assert "creative-pipeline" in content

    def test_run_dry_run_creates_no_tasks(
        self,
        pipeline: CreativePipeline,
        sample_proposals: list[VisionaryProposal],
        approved_verdict: AnalystVerdict,
    ) -> None:
        result = pipeline.run(
            sample_proposals,
            [approved_verdict],
            dry_run=True,
        )
        assert len(result.approved) == 1
        assert len(result.tasks_created) == 0

    def test_run_logs_to_jsonl(
        self,
        pipeline: CreativePipeline,
        sample_proposals: list[VisionaryProposal],
        approved_verdict: AnalystVerdict,
    ) -> None:
        import json

        pipeline.run(sample_proposals, [approved_verdict])
        log_path = pipeline._creative_dir / "runs.jsonl"
        assert log_path.exists()
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["approved_count"] == 1

    def test_run_no_approved_creates_no_tasks(
        self,
        pipeline: CreativePipeline,
        sample_proposals: list[VisionaryProposal],
        rejected_verdict: AnalystVerdict,
    ) -> None:
        result = pipeline.run(sample_proposals, [rejected_verdict])
        assert len(result.approved) == 0
        assert len(result.tasks_created) == 0

    def test_create_backlog_task_without_decomposition(
        self,
        pipeline: CreativePipeline,
        sample_proposals: list[VisionaryProposal],
    ) -> None:
        """A verdict with no decomposition creates a single task."""
        verdict = AnalystVerdict(
            proposal_title="Live agent replay",
            verdict="APPROVE",
            feasibility_score=8,
            impact_score=9,
            risk_score=3,
            composite_score=7.75,
            reasoning="Ship it.",
            decomposition=[],
        )
        paths = pipeline.create_backlog_tasks(sample_proposals, [verdict])
        assert len(paths) == 1
        content = paths[0].read_text(encoding="utf-8")
        assert "Live agent replay" in content

    def test_ticket_ids_increment(
        self,
        pipeline: CreativePipeline,
        sample_proposals: list[VisionaryProposal],
        approved_verdict: AnalystVerdict,
    ) -> None:
        """Ticket IDs should increment sequentially."""
        # Place an existing ticket.
        open_dir = pipeline._backlog_dir / "open"
        (open_dir / "42-existing.md").write_text("# 42 — Existing\n")

        paths = pipeline.create_backlog_tasks(sample_proposals, [approved_verdict])
        # Should start at 43.
        assert any("43-" in p.name for p in paths)

    def test_get_history_empty(self, pipeline: CreativePipeline) -> None:
        assert pipeline.get_history() == []

    def test_get_history_returns_runs(
        self,
        pipeline: CreativePipeline,
        sample_proposals: list[VisionaryProposal],
        approved_verdict: AnalystVerdict,
    ) -> None:
        pipeline.run(sample_proposals, [approved_verdict])
        pipeline.run(sample_proposals, [approved_verdict])
        history = pipeline.get_history()
        assert len(history) == 2

    def test_pipeline_result_to_dict(
        self,
        sample_proposals: list[VisionaryProposal],
        approved_verdict: AnalystVerdict,
    ) -> None:
        result = PipelineResult(
            proposals=sample_proposals,
            verdicts=[approved_verdict],
            approved=[approved_verdict],
            tasks_created=[],
        )
        d = result.to_dict()
        assert d["approved_count"] == 1
        assert len(d["proposals"]) == 3
        assert len(d["verdicts"]) == 1
