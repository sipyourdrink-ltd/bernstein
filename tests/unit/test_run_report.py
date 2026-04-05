"""Tests for bernstein.core.run_report."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.run_report import (
    RunReportGenerator,
    TimelineEntry,
    _fmt_duration,
    _render_ascii_timeline,
)

# ---------------------------------------------------------------------------
# Helper: set up a fake .sdd directory with test data
# ---------------------------------------------------------------------------


def _setup_sdd(
    tmp_path: Path,
    *,
    run_id: str = "run-001",
    tasks_completed: int = 2,
    tasks_failed: int = 1,
    wall_clock_seconds: float = 300.0,
    total_cost_usd: float = 0.55,
    goal: str = "Add JWT auth",
) -> Path:
    """Create a minimal .sdd/ tree with summary.json and cost data."""
    workdir = tmp_path / "project"
    workdir.mkdir()
    sdd = workdir / ".sdd"

    # Summary
    runs_dir = sdd / "runs" / run_id
    runs_dir.mkdir(parents=True)
    summary = {
        "run_id": run_id,
        "tasks_completed": tasks_completed,
        "tasks_failed": tasks_failed,
        "tasks_total": tasks_completed + tasks_failed,
        "wall_clock_seconds": wall_clock_seconds,
        "total_cost_usd": total_cost_usd,
        "goal": goal,
    }
    (runs_dir / "summary.json").write_text(json.dumps(summary))

    # Cost data
    metrics_dir = sdd / "metrics"
    metrics_dir.mkdir(parents=True)
    cost_report = {
        "run_id": run_id,
        "total_spent_usd": total_cost_usd,
        "budget_usd": 5.0,
        "per_model": [
            {"model": "sonnet", "total_cost_usd": 0.35, "invocation_count": 4, "total_tokens": 12000},
            {"model": "haiku", "total_cost_usd": 0.20, "invocation_count": 2, "total_tokens": 8000},
        ],
        "per_agent": [],
    }
    (metrics_dir / f"costs_{run_id}.json").write_text(json.dumps(cost_report))

    # Task metric files
    base_ts = 1700000000.0
    for i, (success, janitor) in enumerate([(True, True), (True, True), (False, False)]):
        task_data = {
            "task_id": f"T-{i + 1}",
            "role": "backend" if i < 2 else "qa",
            "model": "sonnet" if i < 2 else "haiku",
            "success": success,
            "janitor_passed": janitor,
            "cost_usd": 0.20 if success else 0.15,
            "start_time": base_ts + i * 60,
            "end_time": base_ts + i * 60 + 90,
        }
        (metrics_dir / f"task_{i}.json").write_text(json.dumps(task_data))

    # Agent metric files
    for i in range(2):
        agent_data = {"agent_id": f"A-{i + 1}", "role": "backend", "model": "sonnet"}
        (metrics_dir / f"agent_{i}.json").write_text(json.dumps(agent_data))

    return workdir


# ---------------------------------------------------------------------------
# Tests: report generation
# ---------------------------------------------------------------------------


def test_generate_report_with_mock_metrics(tmp_path: Path) -> None:
    """generate() populates all RunReport fields from .sdd/ data."""
    workdir = _setup_sdd(tmp_path)
    gen = RunReportGenerator(workdir, run_id="run-001")
    report = gen.generate()

    assert report.run_id == "run-001"
    assert report.goal == "Add JWT auth"
    assert report.duration_s == 300.0
    assert report.total_cost_usd == 0.55
    assert report.tasks_completed == 2
    assert report.tasks_failed == 1
    assert report.agents_spawned == 2
    assert len(report.task_rows) == 3
    assert len(report.model_costs) == 2
    assert report.quality_pass_count == 2
    assert report.quality_fail_count == 1


def test_generate_report_task_rows_correct(tmp_path: Path) -> None:
    """Task rows reflect per-task data from metric files."""
    workdir = _setup_sdd(tmp_path)
    gen = RunReportGenerator(workdir, run_id="run-001")
    report = gen.generate()

    done_rows = [r for r in report.task_rows if r.status == "done"]
    fail_rows = [r for r in report.task_rows if r.status == "failed"]
    assert len(done_rows) == 2
    assert len(fail_rows) == 1
    assert fail_rows[0].role == "qa"
    # Each task ran for 90 seconds
    for row in report.task_rows:
        assert row.duration_s == 90.0


def test_generate_report_model_costs(tmp_path: Path) -> None:
    """Model costs come from the cost report file."""
    workdir = _setup_sdd(tmp_path)
    gen = RunReportGenerator(workdir, run_id="run-001")
    report = gen.generate()

    models = {mc.model: mc for mc in report.model_costs}
    assert "sonnet" in models
    assert "haiku" in models
    assert models["sonnet"].total_cost_usd == 0.35
    assert models["haiku"].invocation_count == 2


def test_generate_report_timeline(tmp_path: Path) -> None:
    """Timeline entries are populated with correct offsets."""
    workdir = _setup_sdd(tmp_path)
    gen = RunReportGenerator(workdir, run_id="run-001")
    report = gen.generate()

    assert len(report.timeline_entries) == 3
    # First entry starts at offset 0
    offsets = sorted(e.start_offset_s for e in report.timeline_entries)
    assert offsets[0] == 0.0
    # Second starts 60s after first
    assert offsets[1] == 60.0


# ---------------------------------------------------------------------------
# Tests: markdown output
# ---------------------------------------------------------------------------


def test_markdown_output_contains_sections(tmp_path: Path) -> None:
    """to_markdown() includes all expected section headings."""
    workdir = _setup_sdd(tmp_path)
    gen = RunReportGenerator(workdir, run_id="run-001")
    report = gen.generate()
    md = gen.to_markdown(report)

    assert "# Run Report" in md
    assert "## Task Breakdown" in md
    assert "## Quality Gates" in md
    assert "## Cost Analysis" in md
    assert "## Timeline" in md


def test_markdown_output_contains_data(tmp_path: Path) -> None:
    """Markdown includes actual task and cost data."""
    workdir = _setup_sdd(tmp_path)
    gen = RunReportGenerator(workdir, run_id="run-001")
    report = gen.generate()
    md = gen.to_markdown(report)

    assert "run-001" in md
    assert "$0.55" in md
    assert "Add JWT auth" in md
    assert "sonnet" in md
    assert "haiku" in md
    # Task table header
    assert "| Task |" in md
    # Model table header
    assert "| Model |" in md


def test_markdown_quality_gate_pass_rate(tmp_path: Path) -> None:
    """Quality gates section shows the correct pass rate."""
    workdir = _setup_sdd(tmp_path)
    gen = RunReportGenerator(workdir, run_id="run-001")
    report = gen.generate()
    md = gen.to_markdown(report)

    # 2 out of 3 passed = 67%
    assert "67%" in md
    assert "2/3" in md


def test_markdown_most_expensive_task(tmp_path: Path) -> None:
    """Cost analysis shows the most expensive task."""
    workdir = _setup_sdd(tmp_path)
    gen = RunReportGenerator(workdir, run_id="run-001")
    report = gen.generate()
    md = gen.to_markdown(report)

    assert "Most expensive task" in md


# ---------------------------------------------------------------------------
# Tests: empty metrics edge case
# ---------------------------------------------------------------------------


def test_empty_metrics_no_tasks(tmp_path: Path) -> None:
    """Report generation handles the case where no tasks were recorded."""
    workdir = tmp_path / "empty_project"
    workdir.mkdir()
    sdd = workdir / ".sdd"

    # Only a summary.json with zeros
    runs_dir = sdd / "runs" / "run-empty"
    runs_dir.mkdir(parents=True)
    summary = {
        "run_id": "run-empty",
        "tasks_completed": 0,
        "tasks_failed": 0,
        "tasks_total": 0,
        "wall_clock_seconds": 5.0,
        "total_cost_usd": 0.0,
    }
    (runs_dir / "summary.json").write_text(json.dumps(summary))
    (sdd / "metrics").mkdir(parents=True)

    gen = RunReportGenerator(workdir, run_id="run-empty")
    report = gen.generate()

    assert report.tasks_completed == 0
    assert report.tasks_failed == 0
    assert report.agents_spawned == 0
    assert report.task_rows == []
    assert report.model_costs == []
    assert report.timeline_entries == []

    # Markdown should still render without errors
    md = gen.to_markdown(report)
    assert "# Run Report" in md
    assert "No tasks recorded." in md
    assert "No quality gate data available." in md
    assert "No cost data available." in md


def test_empty_metrics_no_sdd(tmp_path: Path) -> None:
    """Report generation handles a project with no .sdd/ at all."""
    workdir = tmp_path / "bare_project"
    workdir.mkdir()

    gen = RunReportGenerator(workdir, run_id="nonexistent")
    report = gen.generate()

    assert report.run_id == "nonexistent"
    assert report.tasks_completed == 0
    assert report.task_rows == []


# ---------------------------------------------------------------------------
# Tests: save
# ---------------------------------------------------------------------------


def test_save_report(tmp_path: Path) -> None:
    """save() writes the markdown file to .sdd/reports/."""
    workdir = _setup_sdd(tmp_path)
    gen = RunReportGenerator(workdir, run_id="run-001")
    report = gen.generate()
    out_path = gen.save(report)

    assert out_path.exists()
    assert out_path.name == "run-001.md"
    content = out_path.read_text(encoding="utf-8")
    assert "# Run Report" in content


def test_save_report_custom_path(tmp_path: Path) -> None:
    """save() respects an explicit output path."""
    workdir = _setup_sdd(tmp_path)
    gen = RunReportGenerator(workdir, run_id="run-001")
    report = gen.generate()
    custom = tmp_path / "custom" / "my_report.md"
    out_path = gen.save(report, path=custom)

    assert out_path == custom
    assert out_path.exists()


# ---------------------------------------------------------------------------
# Tests: helper functions
# ---------------------------------------------------------------------------


def test_fmt_duration() -> None:
    """_fmt_duration formats seconds correctly."""
    assert _fmt_duration(0.0) == "0s"
    assert _fmt_duration(45.0) == "45s"
    assert _fmt_duration(125.0) == "2m 5s"
    assert _fmt_duration(3661.0) == "1h 1m 1s"


def test_render_ascii_timeline_basic() -> None:
    """_render_ascii_timeline produces a plausible ASCII diagram."""
    entries = [
        TimelineEntry(title="Task A", start_offset_s=0.0, end_offset_s=50.0),
        TimelineEntry(title="Task B", start_offset_s=30.0, end_offset_s=100.0),
    ]
    result = _render_ascii_timeline(entries, total_duration_s=100.0)
    assert "Task A" in result
    assert "Task B" in result
    assert "#" in result
    assert "```" in result


def test_render_ascii_timeline_zero_duration() -> None:
    """_render_ascii_timeline handles zero duration gracefully."""
    result = _render_ascii_timeline([], total_duration_s=0.0)
    assert "zero duration" in result.lower()


def test_detect_latest_run_id(tmp_path: Path) -> None:
    """Auto-detects the latest run from .sdd/runs/."""
    workdir = _setup_sdd(tmp_path, run_id="run-latest")
    gen = RunReportGenerator(workdir)
    assert gen._run_id == "run-latest"
