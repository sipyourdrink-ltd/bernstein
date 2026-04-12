"""Unit tests for agent collaboration pattern mining."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.quality.collaboration_miner import (
    CollaborationPattern,
    MiningResult,
    RunCollaboration,
    extract_collaborations,
    generate_recommendations,
    mine_patterns,
    render_patterns_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_archive(path: Path, records: list[dict[str, object]]) -> None:
    """Write archive records as JSONL to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _make_archive_record(
    *,
    task_id: str = "t1",
    title: str = "Do something",
    role: str = "backend",
    status: str = "done",
    created_at: float = 1000.0,
    completed_at: float = 1060.0,
    claimed_by_session: str | None = "run-1",
    tenant_id: str = "default",
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "title": title,
        "role": role,
        "status": status,
        "created_at": created_at,
        "completed_at": completed_at,
        "duration_seconds": completed_at - created_at,
        "result_summary": None,
        "cost_usd": 0.01,
        "assigned_agent": None,
        "owned_files": [],
        "claimed_by_session": claimed_by_session,
        "tenant_id": tenant_id,
    }


# ---------------------------------------------------------------------------
# CollaborationPattern dataclass
# ---------------------------------------------------------------------------


class TestCollaborationPattern:
    def test_frozen(self) -> None:
        pat = CollaborationPattern(
            roles=("backend", "qa"),
            ordering="sequential",
            success_rate=0.9,
            avg_rework_cycles=0.5,
            sample_size=10,
            description="test",
        )
        with pytest.raises(AttributeError):
            pat.success_rate = 0.0  # type: ignore[misc]

    def test_fields(self) -> None:
        pat = CollaborationPattern(
            roles=("backend", "frontend"),
            ordering="parallel",
            success_rate=0.75,
            avg_rework_cycles=1.2,
            sample_size=5,
            description="backend + frontend in parallel",
        )
        assert pat.roles == ("backend", "frontend")
        assert pat.ordering == "parallel"
        assert pat.success_rate == 0.75
        assert pat.sample_size == 5


# ---------------------------------------------------------------------------
# RunCollaboration dataclass
# ---------------------------------------------------------------------------


class TestRunCollaboration:
    def test_frozen(self) -> None:
        rc = RunCollaboration(
            run_id="r1",
            role_sequence=("backend", "qa"),
            success=True,
            rework_count=0,
            duration_s=120.0,
        )
        with pytest.raises(AttributeError):
            rc.success = False  # type: ignore[misc]

    def test_fields(self) -> None:
        rc = RunCollaboration(
            run_id="r2",
            role_sequence=("frontend", "backend", "qa"),
            success=False,
            rework_count=2,
            duration_s=300.0,
        )
        assert rc.role_sequence == ("frontend", "backend", "qa")
        assert rc.rework_count == 2


# ---------------------------------------------------------------------------
# MiningResult dataclass
# ---------------------------------------------------------------------------


class TestMiningResult:
    def test_frozen(self) -> None:
        mr = MiningResult(patterns=(), total_runs_analyzed=0, recommendations=())
        with pytest.raises(AttributeError):
            mr.total_runs_analyzed = 5  # type: ignore[misc]

    def test_fields(self) -> None:
        pat = CollaborationPattern(
            roles=("a", "b"),
            ordering="sequential",
            success_rate=1.0,
            avg_rework_cycles=0.0,
            sample_size=3,
            description="test",
        )
        mr = MiningResult(
            patterns=(pat,),
            total_runs_analyzed=10,
            recommendations=("do X",),
        )
        assert len(mr.patterns) == 1
        assert mr.total_runs_analyzed == 10
        assert mr.recommendations == ("do X",)


# ---------------------------------------------------------------------------
# extract_collaborations
# ---------------------------------------------------------------------------


class TestExtractCollaborations:
    def test_empty_archive(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        archive.write_text("")
        result = extract_collaborations(archive)
        assert result == []

    def test_missing_file(self, tmp_path: Path) -> None:
        result = extract_collaborations(tmp_path / "nonexistent.jsonl")
        assert result == []

    def test_single_task_run_skipped(self, tmp_path: Path) -> None:
        """A run with only one task is not a collaboration."""
        archive = tmp_path / "tasks.jsonl"
        _write_archive(archive, [_make_archive_record(claimed_by_session="run-1")])
        result = extract_collaborations(archive)
        assert result == []

    def test_two_task_run(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        records = [
            _make_archive_record(
                task_id="t1",
                role="backend",
                completed_at=1060.0,
                claimed_by_session="run-1",
            ),
            _make_archive_record(
                task_id="t2",
                role="qa",
                completed_at=1120.0,
                claimed_by_session="run-1",
            ),
        ]
        _write_archive(archive, records)
        result = extract_collaborations(archive)
        assert len(result) == 1
        assert result[0].role_sequence == ("backend", "qa")
        assert result[0].success is True

    def test_failed_task_marks_run_unsuccessful(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        records = [
            _make_archive_record(
                task_id="t1",
                role="backend",
                status="done",
                completed_at=1060.0,
                claimed_by_session="run-1",
            ),
            _make_archive_record(
                task_id="t2",
                role="qa",
                status="failed",
                completed_at=1120.0,
                claimed_by_session="run-1",
            ),
        ]
        _write_archive(archive, records)
        result = extract_collaborations(archive)
        assert len(result) == 1
        assert result[0].success is False

    def test_rework_count_from_retry_titles(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        records = [
            _make_archive_record(
                task_id="t1",
                role="backend",
                title="Implement feature",
                completed_at=1060.0,
                claimed_by_session="run-1",
            ),
            _make_archive_record(
                task_id="t2",
                role="backend",
                title="[RETRY 1] Implement feature",
                completed_at=1120.0,
                claimed_by_session="run-1",
            ),
            _make_archive_record(
                task_id="t3",
                role="qa",
                title="Fix: test failure",
                completed_at=1180.0,
                claimed_by_session="run-1",
            ),
        ]
        _write_archive(archive, records)
        result = extract_collaborations(archive)
        assert len(result) == 1
        assert result[0].rework_count == 2

    def test_multiple_runs_separated(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        records = [
            _make_archive_record(task_id="t1", role="backend", claimed_by_session="run-1", completed_at=1060.0),
            _make_archive_record(task_id="t2", role="qa", claimed_by_session="run-1", completed_at=1120.0),
            _make_archive_record(task_id="t3", role="frontend", claimed_by_session="run-2", completed_at=2060.0),
            _make_archive_record(task_id="t4", role="devops", claimed_by_session="run-2", completed_at=2120.0),
        ]
        _write_archive(archive, records)
        result = extract_collaborations(archive)
        assert len(result) == 2
        run_ids = {r.run_id for r in result}
        assert run_ids == {"run-1", "run-2"}

    def test_falls_back_to_tenant_id(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        records = [
            _make_archive_record(
                task_id="t1",
                role="backend",
                claimed_by_session=None,
                tenant_id="tenant-a",
                completed_at=1060.0,
            ),
            _make_archive_record(
                task_id="t2",
                role="qa",
                claimed_by_session=None,
                tenant_id="tenant-a",
                completed_at=1120.0,
            ),
        ]
        _write_archive(archive, records)
        result = extract_collaborations(archive)
        assert len(result) == 1
        assert result[0].run_id == "tenant-a"

    def test_duration_computed(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        records = [
            _make_archive_record(
                task_id="t1",
                role="backend",
                created_at=1000.0,
                completed_at=1060.0,
                claimed_by_session="run-1",
            ),
            _make_archive_record(
                task_id="t2",
                role="qa",
                created_at=1050.0,
                completed_at=1200.0,
                claimed_by_session="run-1",
            ),
        ]
        _write_archive(archive, records)
        result = extract_collaborations(archive)
        assert len(result) == 1
        # Duration: 1200 - 1000 = 200
        assert result[0].duration_s == pytest.approx(200.0)

    def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        lines = [
            json.dumps(
                _make_archive_record(task_id="t1", role="backend", claimed_by_session="run-1", completed_at=1060.0)
            ),
            "not valid json{{{",
            json.dumps(_make_archive_record(task_id="t2", role="qa", claimed_by_session="run-1", completed_at=1120.0)),
        ]
        archive.write_text("\n".join(lines) + "\n")
        result = extract_collaborations(archive)
        assert len(result) == 1
        assert result[0].role_sequence == ("backend", "qa")


# ---------------------------------------------------------------------------
# mine_patterns
# ---------------------------------------------------------------------------


class TestMinePatterns:
    def test_empty_input(self) -> None:
        result = mine_patterns([])
        assert result.total_runs_analyzed == 0
        assert result.patterns == ()

    def test_below_min_support(self) -> None:
        """Pairs appearing fewer than min_support times are excluded."""
        collabs = [
            RunCollaboration("r1", ("backend", "qa"), True, 0, 100.0),
            RunCollaboration("r2", ("backend", "qa"), True, 0, 100.0),
        ]
        result = mine_patterns(collabs, min_support=3)
        assert result.patterns == ()

    def test_meets_min_support(self) -> None:
        collabs = [
            RunCollaboration("r1", ("backend", "qa"), True, 0, 100.0),
            RunCollaboration("r2", ("backend", "qa"), True, 0, 100.0),
            RunCollaboration("r3", ("backend", "qa"), False, 1, 100.0),
        ]
        result = mine_patterns(collabs, min_support=3)
        assert len(result.patterns) == 1
        pat = result.patterns[0]
        assert pat.roles == ("backend", "qa")
        assert pat.success_rate == pytest.approx(2 / 3)
        assert pat.sample_size == 3

    def test_sequential_ordering_detected(self) -> None:
        """When role_a always comes before role_b, ordering is sequential."""
        collabs = [
            RunCollaboration("r1", ("backend", "qa"), True, 0, 100.0),
            RunCollaboration("r2", ("backend", "qa"), True, 0, 100.0),
            RunCollaboration("r3", ("backend", "qa"), True, 0, 100.0),
        ]
        result = mine_patterns(collabs, min_support=3)
        assert len(result.patterns) == 1
        assert result.patterns[0].ordering == "sequential"

    def test_parallel_ordering_detected(self) -> None:
        """When roles interleave, ordering is parallel."""
        collabs = [
            RunCollaboration("r1", ("backend", "qa", "backend"), True, 0, 100.0),
            RunCollaboration("r2", ("qa", "backend", "qa"), True, 0, 100.0),
            RunCollaboration("r3", ("backend", "qa", "backend"), True, 0, 100.0),
        ]
        result = mine_patterns(collabs, min_support=3)
        assert len(result.patterns) == 1
        assert result.patterns[0].ordering == "parallel"

    def test_multiple_pairs_discovered(self) -> None:
        collabs = [
            RunCollaboration("r1", ("backend", "frontend", "qa"), True, 0, 100.0),
            RunCollaboration("r2", ("backend", "frontend", "qa"), True, 0, 100.0),
            RunCollaboration("r3", ("backend", "frontend", "qa"), False, 1, 100.0),
        ]
        result = mine_patterns(collabs, min_support=3)
        # Should find: backend+frontend, backend+qa, frontend+qa
        assert len(result.patterns) == 3
        role_pairs = {p.roles for p in result.patterns}
        assert ("backend", "frontend") in role_pairs
        assert ("backend", "qa") in role_pairs
        assert ("frontend", "qa") in role_pairs

    def test_sorted_by_success_rate(self) -> None:
        collabs = [
            RunCollaboration("r1", ("backend", "qa"), True, 0, 100.0),
            RunCollaboration("r2", ("backend", "qa"), True, 0, 100.0),
            RunCollaboration("r3", ("backend", "qa"), True, 0, 100.0),
            RunCollaboration("r4", ("frontend", "qa"), True, 0, 100.0),
            RunCollaboration("r5", ("frontend", "qa"), False, 1, 100.0),
            RunCollaboration("r6", ("frontend", "qa"), False, 1, 100.0),
        ]
        result = mine_patterns(collabs, min_support=3)
        assert len(result.patterns) == 2
        # backend+qa has 100% success, frontend+qa has ~33%
        assert result.patterns[0].roles == ("backend", "qa")
        assert result.patterns[1].roles == ("frontend", "qa")

    def test_avg_rework_computed(self) -> None:
        collabs = [
            RunCollaboration("r1", ("backend", "qa"), True, 0, 100.0),
            RunCollaboration("r2", ("backend", "qa"), True, 3, 100.0),
            RunCollaboration("r3", ("backend", "qa"), True, 0, 100.0),
        ]
        result = mine_patterns(collabs, min_support=3)
        assert result.patterns[0].avg_rework_cycles == pytest.approx(1.0)

    def test_custom_min_support(self) -> None:
        collabs = [
            RunCollaboration("r1", ("backend", "qa"), True, 0, 100.0),
        ]
        result = mine_patterns(collabs, min_support=1)
        assert len(result.patterns) == 1


# ---------------------------------------------------------------------------
# generate_recommendations
# ---------------------------------------------------------------------------


class TestGenerateRecommendations:
    def test_empty_patterns(self) -> None:
        result = generate_recommendations(())
        assert result == ()

    def test_high_success_sequential(self) -> None:
        patterns = (
            CollaborationPattern(
                roles=("backend", "qa"),
                ordering="sequential",
                success_rate=0.9,
                avg_rework_cycles=0.3,
                sample_size=10,
                description="test",
            ),
        )
        recs = generate_recommendations(patterns)
        assert any("backend" in r and "qa" in r and "sequential" in r for r in recs)

    def test_high_rework_warning(self) -> None:
        patterns = (
            CollaborationPattern(
                roles=("frontend", "devops"),
                ordering="parallel",
                success_rate=0.5,
                avg_rework_cycles=3.0,
                sample_size=5,
                description="test",
            ),
        )
        recs = generate_recommendations(patterns)
        assert any("rework" in r.lower() for r in recs)

    def test_qa_rework_reduction(self) -> None:
        patterns = (
            CollaborationPattern(
                roles=("backend", "qa"),
                ordering="sequential",
                success_rate=0.9,
                avg_rework_cycles=0.2,
                sample_size=5,
                description="test",
            ),
            CollaborationPattern(
                roles=("backend", "frontend"),
                ordering="parallel",
                success_rate=0.7,
                avg_rework_cycles=2.0,
                sample_size=5,
                description="test",
            ),
        )
        recs = generate_recommendations(patterns)
        assert any("qa" in r.lower() and "rework" in r.lower() for r in recs)


# ---------------------------------------------------------------------------
# render_patterns_report
# ---------------------------------------------------------------------------


class TestRenderPatternsReport:
    def test_empty_result(self) -> None:
        result = MiningResult(patterns=(), total_runs_analyzed=0, recommendations=())
        report = render_patterns_report(result)
        assert "# Collaboration Pattern Report" in report
        assert "No collaboration patterns found" in report

    def test_with_patterns(self) -> None:
        pat = CollaborationPattern(
            roles=("backend", "qa"),
            ordering="sequential",
            success_rate=0.85,
            avg_rework_cycles=0.5,
            sample_size=10,
            description="test",
        )
        result = MiningResult(
            patterns=(pat,),
            total_runs_analyzed=15,
            recommendations=("Run QA after backend.",),
        )
        report = render_patterns_report(result)
        assert "Runs analyzed:** 15" in report
        assert "Patterns found:** 1" in report
        assert "backend + qa" in report
        assert "sequential" in report
        assert "85%" in report
        assert "Run QA after backend." in report

    def test_report_is_valid_markdown(self) -> None:
        pat = CollaborationPattern(
            roles=("a", "b"),
            ordering="parallel",
            success_rate=1.0,
            avg_rework_cycles=0.0,
            sample_size=5,
            description="x",
        )
        result = MiningResult(
            patterns=(pat,),
            total_runs_analyzed=5,
            recommendations=("do X",),
        )
        report = render_patterns_report(result)
        # Should have markdown table delimiters
        assert "|" in report
        assert "---" in report
