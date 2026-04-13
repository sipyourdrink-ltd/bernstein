"""Tests for automated post-mortem report generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.observability.postmortem_report import (
    FailurePattern,
    PostMortem,
    TimelineEvent,
    build_timeline,
    detect_failure_patterns,
    generate_postmortem,
    generate_recommendations,
    render_postmortem_markdown,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_summary(sdd: Path, run_id: str, data: dict[str, object]) -> None:
    """Write a summary.json for the given run."""
    run_dir = sdd / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(json.dumps(data), encoding="utf-8")


def _write_replay(sdd: Path, run_id: str, events: list[dict[str, object]]) -> None:
    """Write replay.jsonl for the given run."""
    run_dir = sdd / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) for e in events]
    (run_dir / "replay.jsonl").write_text("\n".join(lines), encoding="utf-8")


def _write_task_metric(sdd: Path, filename: str, data: dict[str, object]) -> None:
    """Write a task metric JSON file."""
    metrics_dir = sdd / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / filename).write_text(json.dumps(data), encoding="utf-8")


def _ts(offset: float = 0.0) -> float:
    """Return a fixed base timestamp plus an offset."""
    return 1700000000.0 + offset


# ---------------------------------------------------------------------------
# TimelineEvent dataclass
# ---------------------------------------------------------------------------


class TestTimelineEvent:
    """Tests for the TimelineEvent frozen dataclass."""

    def test_create_with_all_fields(self) -> None:
        ev = TimelineEvent(
            timestamp=_ts(),
            event_type="task_failed",
            description="Task T-1 failed",
            agent_id="agent-1",
            task_id="T-1",
        )
        assert ev.timestamp == _ts()
        assert ev.event_type == "task_failed"
        assert ev.description == "Task T-1 failed"
        assert ev.agent_id == "agent-1"
        assert ev.task_id == "T-1"

    def test_optional_fields_default_to_none(self) -> None:
        ev = TimelineEvent(
            timestamp=_ts(),
            event_type="run_started",
            description="Run started",
        )
        assert ev.agent_id is None
        assert ev.task_id is None

    def test_is_frozen(self) -> None:
        ev = TimelineEvent(
            timestamp=_ts(),
            event_type="run_started",
            description="Run started",
        )
        with pytest.raises(AttributeError):
            ev.event_type = "modified"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FailurePattern dataclass
# ---------------------------------------------------------------------------


class TestFailurePattern:
    """Tests for the FailurePattern frozen dataclass."""

    def test_create_with_affected_tasks(self) -> None:
        fp = FailurePattern(
            pattern_name="cascade_failure",
            description="Cascade detected",
            occurrences=5,
            affected_tasks=("T-1", "T-2", "T-3"),
        )
        assert fp.pattern_name == "cascade_failure"
        assert fp.occurrences == 5
        assert len(fp.affected_tasks) == 3

    def test_affected_tasks_is_tuple(self) -> None:
        fp = FailurePattern(
            pattern_name="test",
            description="test",
            occurrences=1,
            affected_tasks=("T-1",),
        )
        assert isinstance(fp.affected_tasks, tuple)

    def test_is_frozen(self) -> None:
        fp = FailurePattern(
            pattern_name="test",
            description="test",
            occurrences=1,
            affected_tasks=(),
        )
        with pytest.raises(AttributeError):
            fp.occurrences = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PostMortem dataclass
# ---------------------------------------------------------------------------


class TestPostMortem:
    """Tests for the PostMortem frozen dataclass."""

    def test_create_full(self) -> None:
        pm = PostMortem(
            run_id="run-001",
            start_time=_ts(),
            end_time=_ts(300),
            timeline=(),
            root_causes=(),
            contributing_factors=(),
            recommendations=(),
            summary="Run failed.",
        )
        assert pm.run_id == "run-001"
        assert pm.end_time - pm.start_time == pytest.approx(300.0)

    def test_is_frozen(self) -> None:
        pm = PostMortem(
            run_id="run-001",
            start_time=_ts(),
            end_time=_ts(300),
            timeline=(),
            root_causes=(),
            contributing_factors=(),
            recommendations=(),
            summary="Run failed.",
        )
        with pytest.raises(AttributeError):
            pm.summary = "modified"  # type: ignore[misc]

    def test_timeline_is_tuple(self) -> None:
        ev = TimelineEvent(
            timestamp=_ts(),
            event_type="task_failed",
            description="fail",
        )
        pm = PostMortem(
            run_id="run-001",
            start_time=_ts(),
            end_time=_ts(1),
            timeline=(ev,),
            root_causes=(),
            contributing_factors=(),
            recommendations=(),
            summary="Summary.",
        )
        assert isinstance(pm.timeline, tuple)
        assert len(pm.timeline) == 1


# ---------------------------------------------------------------------------
# build_timeline
# ---------------------------------------------------------------------------


class TestBuildTimeline:
    """Tests for the build_timeline function."""

    def test_empty_archive(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        timeline = build_timeline(sdd, "nonexistent-run")
        assert timeline == ()

    def test_replay_events_parsed(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _write_replay(
            sdd,
            "run-1",
            [
                {"ts": _ts(0), "event": "run_started"},
                {"ts": _ts(10), "event": "agent_spawned", "agent_id": "a-1"},
                {"ts": _ts(20), "event": "task_claimed", "agent_id": "a-1", "task_id": "T-1"},
                {"ts": _ts(100), "event": "task_completed", "task_id": "T-1"},
            ],
        )
        timeline = build_timeline(sdd, "run-1")
        assert len(timeline) == 4
        assert timeline[0].event_type == "run_started"
        assert timeline[1].agent_id == "a-1"
        assert timeline[3].task_id == "T-1"

    def test_timeline_sorted_by_timestamp(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _write_replay(
            sdd,
            "run-1",
            [
                {"ts": _ts(50), "event": "task_failed", "task_id": "T-2"},
                {"ts": _ts(10), "event": "task_claimed", "task_id": "T-1"},
            ],
        )
        timeline = build_timeline(sdd, "run-1")
        assert timeline[0].timestamp < timeline[1].timestamp

    def test_task_metrics_contribute_events(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        (sdd / "runs" / "run-1").mkdir(parents=True)
        _write_task_metric(
            sdd,
            "task_001.json",
            {
                "task_id": "T-1",
                "start_time": _ts(0),
                "end_time": _ts(100),
                "success": True,
            },
        )
        timeline = build_timeline(sdd, "run-1")
        types = [e.event_type for e in timeline]
        assert "task_started" in types
        assert "task_completed" in types

    def test_failed_task_metric_produces_task_failed(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        (sdd / "runs" / "run-1").mkdir(parents=True)
        _write_task_metric(
            sdd,
            "task_001.json",
            {
                "task_id": "T-1",
                "start_time": _ts(0),
                "end_time": _ts(50),
                "success": False,
            },
        )
        timeline = build_timeline(sdd, "run-1")
        types = [e.event_type for e in timeline]
        assert "task_failed" in types

    def test_malformed_replay_lines_skipped(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        run_dir = sdd / "runs" / "run-1"
        run_dir.mkdir(parents=True)
        content = (
            '{"ts": 1700000000, "event": "run_started"}\n'
            "this is not json\n"
            '{"ts": 1700000010, "event": "run_completed"}\n'
        )
        (run_dir / "replay.jsonl").write_text(content, encoding="utf-8")
        timeline = build_timeline(sdd, "run-1")
        assert len(timeline) == 2


# ---------------------------------------------------------------------------
# detect_failure_patterns
# ---------------------------------------------------------------------------


class TestDetectFailurePatterns:
    """Tests for the detect_failure_patterns function."""

    def test_no_failures_yields_no_patterns(self) -> None:
        timeline = (
            TimelineEvent(_ts(0), "task_started", "Started T-1", task_id="T-1"),
            TimelineEvent(_ts(100), "task_completed", "Completed T-1", task_id="T-1"),
        )
        patterns = detect_failure_patterns(timeline)
        assert patterns == ()

    def test_repeated_failures_detected(self) -> None:
        timeline = (
            TimelineEvent(_ts(0), "task_failed", "Failed T-1", task_id="T-1"),
            TimelineEvent(_ts(10), "task_failed", "Failed T-1 again", task_id="T-1"),
            TimelineEvent(_ts(20), "task_failed", "Failed T-1 third", task_id="T-1"),
        )
        patterns = detect_failure_patterns(timeline)
        names = [p.pattern_name for p in patterns]
        assert "repeated_file_failure" in names

    def test_cascade_failure_detected(self) -> None:
        # 4 failures within 30 seconds
        timeline = (
            TimelineEvent(_ts(0), "task_failed", "fail", task_id="T-1"),
            TimelineEvent(_ts(10), "task_failed", "fail", task_id="T-2"),
            TimelineEvent(_ts(20), "task_failed", "fail", task_id="T-3"),
            TimelineEvent(_ts(30), "task_failed", "fail", task_id="T-4"),
        )
        patterns = detect_failure_patterns(timeline)
        names = [p.pattern_name for p in patterns]
        assert "cascade_failure" in names
        cascade = next(p for p in patterns if p.pattern_name == "cascade_failure")
        assert cascade.occurrences == 4

    def test_no_cascade_when_failures_spread_out(self) -> None:
        # Only 2 failures in any 60s window
        timeline = (
            TimelineEvent(_ts(0), "task_failed", "fail", task_id="T-1"),
            TimelineEvent(_ts(100), "task_failed", "fail", task_id="T-2"),
        )
        patterns = detect_failure_patterns(timeline)
        names = [p.pattern_name for p in patterns]
        assert "cascade_failure" not in names

    def test_timeout_spiral_detected(self) -> None:
        timeline = (
            TimelineEvent(_ts(0), "timeout", "Timeout T-1", task_id="T-1"),
            TimelineEvent(_ts(30), "timeout", "Timeout T-2", task_id="T-2"),
            TimelineEvent(_ts(60), "timeout", "Timeout T-3", task_id="T-3"),
        )
        patterns = detect_failure_patterns(timeline)
        names = [p.pattern_name for p in patterns]
        assert "timeout_spiral" in names

    def test_budget_exhaustion_detected(self) -> None:
        timeline = (
            TimelineEvent(_ts(0), "task_started", "Start T-1", task_id="T-1"),
            TimelineEvent(_ts(50), "budget_exceeded", "Budget hit"),
        )
        patterns = detect_failure_patterns(timeline)
        names = [p.pattern_name for p in patterns]
        assert "budget_exhaustion" in names

    def test_multiple_patterns_can_coexist(self) -> None:
        timeline = (
            TimelineEvent(_ts(0), "task_failed", "fail", task_id="T-1"),
            TimelineEvent(_ts(5), "task_failed", "fail", task_id="T-1"),
            TimelineEvent(_ts(10), "task_failed", "fail", task_id="T-2"),
            TimelineEvent(_ts(15), "task_failed", "fail", task_id="T-3"),
            TimelineEvent(_ts(50), "timeout", "timeout T-4", task_id="T-4"),
            TimelineEvent(_ts(55), "timeout", "timeout T-5", task_id="T-5"),
        )
        patterns = detect_failure_patterns(timeline)
        names = {p.pattern_name for p in patterns}
        assert "repeated_file_failure" in names
        assert "cascade_failure" in names
        assert "timeout_spiral" in names


# ---------------------------------------------------------------------------
# generate_recommendations
# ---------------------------------------------------------------------------


class TestGenerateRecommendations:
    """Tests for the generate_recommendations function."""

    def test_no_patterns_yields_no_recommendations(self) -> None:
        recs = generate_recommendations(())
        assert recs == ()

    def test_repeated_failure_recommendations(self) -> None:
        patterns = (FailurePattern("repeated_file_failure", "desc", 3, ("T-1",)),)
        recs = generate_recommendations(patterns)
        assert len(recs) >= 1
        assert any("decomposing" in r.lower() or "failing" in r.lower() for r in recs)

    def test_cascade_recommendations(self) -> None:
        patterns = (FailurePattern("cascade_failure", "desc", 4, ("T-1", "T-2")),)
        recs = generate_recommendations(patterns)
        assert len(recs) >= 1
        assert any("circuit" in r.lower() or "cascade" in r.lower() for r in recs)

    def test_timeout_recommendations(self) -> None:
        patterns = (FailurePattern("timeout_spiral", "desc", 3, ("T-1",)),)
        recs = generate_recommendations(patterns)
        assert len(recs) >= 1
        assert any("timeout" in r.lower() for r in recs)

    def test_budget_recommendations(self) -> None:
        patterns = (FailurePattern("budget_exhaustion", "desc", 1, ()),)
        recs = generate_recommendations(patterns)
        assert len(recs) >= 1
        assert any("budget" in r.lower() or "cost" in r.lower() for r in recs)

    def test_recommendations_deduplicated(self) -> None:
        patterns = (
            FailurePattern("cascade_failure", "a", 3, ("T-1",)),
            FailurePattern("cascade_failure", "b", 2, ("T-2",)),
        )
        recs = generate_recommendations(patterns)
        assert len(recs) == len(set(recs))

    def test_unknown_pattern_gets_fallback(self) -> None:
        patterns = (FailurePattern("unknown_pattern", "something", 1, ()),)
        recs = generate_recommendations(patterns)
        assert len(recs) == 1
        assert "review" in recs[0].lower()


# ---------------------------------------------------------------------------
# generate_postmortem (integration)
# ---------------------------------------------------------------------------


class TestGeneratePostmortem:
    """Tests for the full generate_postmortem pipeline."""

    def test_minimal_run(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _write_summary(
            sdd,
            "run-1",
            {
                "tasks_total": 2,
                "tasks_completed": 1,
                "tasks_failed": 1,
                "wall_clock_seconds": 120.0,
                "total_cost_usd": 0.05,
                "timestamp": _ts(),
            },
        )
        _write_replay(
            sdd,
            "run-1",
            [
                {"ts": _ts(0), "event": "run_started"},
                {"ts": _ts(120), "event": "run_completed"},
            ],
        )
        pm = generate_postmortem(sdd, "run-1")
        assert pm.run_id == "run-1"
        assert pm.start_time > 0
        assert pm.end_time >= pm.start_time
        assert "run-1" in pm.summary

    def test_with_failures_detects_patterns(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _write_summary(
            sdd,
            "run-2",
            {
                "tasks_total": 5,
                "tasks_completed": 1,
                "tasks_failed": 4,
                "wall_clock_seconds": 300.0,
                "total_cost_usd": 1.50,
                "timestamp": _ts(),
            },
        )
        _write_replay(
            sdd,
            "run-2",
            [
                {"ts": _ts(0), "event": "run_started"},
                {"ts": _ts(10), "event": "task_failed", "task_id": "T-1"},
                {"ts": _ts(20), "event": "task_failed", "task_id": "T-1"},
                {"ts": _ts(30), "event": "task_failed", "task_id": "T-2"},
                {"ts": _ts(40), "event": "task_failed", "task_id": "T-3"},
                {"ts": _ts(300), "event": "run_completed"},
            ],
        )
        pm = generate_postmortem(sdd, "run-2")
        assert len(pm.root_causes) > 0
        assert len(pm.recommendations) > 0

    def test_empty_archive(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        pm = generate_postmortem(sdd, "missing-run")
        assert pm.run_id == "missing-run"
        assert pm.timeline == ()
        assert pm.root_causes == ()

    def test_contributing_factors_high_failure_rate(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _write_summary(
            sdd,
            "run-3",
            {
                "tasks_total": 10,
                "tasks_completed": 2,
                "tasks_failed": 8,
                "wall_clock_seconds": 600.0,
                "total_cost_usd": 0.10,
                "timestamp": _ts(),
            },
        )
        _write_replay(sdd, "run-3", [])
        pm = generate_postmortem(sdd, "run-3")
        assert any("failure rate" in f.lower() for f in pm.contributing_factors)

    def test_contributing_factors_high_cost(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _write_summary(
            sdd,
            "run-4",
            {
                "tasks_total": 5,
                "tasks_completed": 5,
                "tasks_failed": 0,
                "wall_clock_seconds": 300.0,
                "total_cost_usd": 25.0,
                "timestamp": _ts(),
            },
        )
        _write_replay(sdd, "run-4", [])
        pm = generate_postmortem(sdd, "run-4")
        assert any("cost" in f.lower() for f in pm.contributing_factors)


# ---------------------------------------------------------------------------
# render_postmortem_markdown
# ---------------------------------------------------------------------------


class TestRenderPostmortemMarkdown:
    """Tests for the render_postmortem_markdown function."""

    def _minimal_pm(self) -> PostMortem:
        return PostMortem(
            run_id="run-001",
            start_time=_ts(),
            end_time=_ts(300),
            timeline=(),
            root_causes=(),
            contributing_factors=(),
            recommendations=(),
            summary="Test run failed.",
        )

    def test_contains_run_id_header(self) -> None:
        md = render_postmortem_markdown(self._minimal_pm())
        assert "# Post-Mortem Report: Run `run-001`" in md

    def test_contains_summary_section(self) -> None:
        md = render_postmortem_markdown(self._minimal_pm())
        assert "## Summary" in md
        assert "Test run failed." in md

    def test_contains_timeline_section(self) -> None:
        md = render_postmortem_markdown(self._minimal_pm())
        assert "## Event Timeline" in md

    def test_timeline_table_rendered(self) -> None:
        ev = TimelineEvent(
            timestamp=_ts(),
            event_type="task_failed",
            description="Task T-1 failed",
            agent_id="agent-1",
            task_id="T-1",
        )
        pm = PostMortem(
            run_id="run-001",
            start_time=_ts(),
            end_time=_ts(300),
            timeline=(ev,),
            root_causes=(),
            contributing_factors=(),
            recommendations=(),
            summary="Summary.",
        )
        md = render_postmortem_markdown(pm)
        assert "| Time |" in md
        assert "task_failed" in md
        assert "T-1" in md

    def test_root_causes_rendered(self) -> None:
        pattern = FailurePattern(
            pattern_name="cascade_failure",
            description="A cascade was detected.",
            occurrences=3,
            affected_tasks=("T-1", "T-2"),
        )
        pm = PostMortem(
            run_id="run-001",
            start_time=_ts(),
            end_time=_ts(300),
            timeline=(),
            root_causes=(pattern,),
            contributing_factors=(),
            recommendations=(),
            summary="Summary.",
        )
        md = render_postmortem_markdown(pm)
        assert "## Root Causes" in md
        assert "Cascade Failure" in md
        assert "A cascade was detected." in md

    def test_recommendations_rendered(self) -> None:
        pm = PostMortem(
            run_id="run-001",
            start_time=_ts(),
            end_time=_ts(300),
            timeline=(),
            root_causes=(),
            contributing_factors=(),
            recommendations=("Do thing A.", "Do thing B."),
            summary="Summary.",
        )
        md = render_postmortem_markdown(pm)
        assert "## Recommendations" in md
        assert "1. Do thing A." in md
        assert "2. Do thing B." in md

    def test_contributing_factors_rendered(self) -> None:
        pm = PostMortem(
            run_id="run-001",
            start_time=_ts(),
            end_time=_ts(300),
            timeline=(),
            root_causes=(),
            contributing_factors=("High failure rate: 80%",),
            recommendations=(),
            summary="Summary.",
        )
        md = render_postmortem_markdown(pm)
        assert "## Contributing Factors" in md
        assert "High failure rate: 80%" in md

    def test_empty_sections_show_placeholders(self) -> None:
        md = render_postmortem_markdown(self._minimal_pm())
        assert "No timeline events recorded." in md
        assert "No specific failure patterns detected." in md
        assert "No additional contributing factors identified." in md
        assert "No specific recommendations at this time." in md

    def test_full_pipeline_to_markdown(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _write_summary(
            sdd,
            "run-5",
            {
                "tasks_total": 3,
                "tasks_completed": 1,
                "tasks_failed": 2,
                "wall_clock_seconds": 180.0,
                "total_cost_usd": 0.50,
                "timestamp": _ts(),
            },
        )
        _write_replay(
            sdd,
            "run-5",
            [
                {"ts": _ts(0), "event": "run_started"},
                {"ts": _ts(10), "event": "agent_spawned", "agent_id": "a-1"},
                {"ts": _ts(30), "event": "task_failed", "task_id": "T-1"},
                {"ts": _ts(60), "event": "task_failed", "task_id": "T-1"},
                {"ts": _ts(90), "event": "task_completed", "task_id": "T-2"},
                {"ts": _ts(180), "event": "run_completed"},
            ],
        )
        pm = generate_postmortem(sdd, "run-5")
        md = render_postmortem_markdown(pm)
        assert "run-5" in md
        assert "## Summary" in md
        assert "## Event Timeline" in md
        assert "## Root Causes" in md
        assert "## Recommendations" in md
