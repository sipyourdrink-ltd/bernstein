"""Tests for bernstein.core.observability.run_export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.export_cmd import export_cmd
from bernstein.core.observability.run_export import (
    MAX_FILE_SIZE,
    _agent_summary,
    _ExportReport,
    _fmt_duration,
    _fmt_duration_short,
    _load_report_data,
    _sequential_estimate,
    _TaskRow,
    export_run_report,
)


class CostModelPayload(TypedDict):
    model: str
    total_cost_usd: float
    invocation_count: int
    total_tokens: int


class CostReportPayload(TypedDict, total=False):
    run_id: str
    total_spent_usd: float
    per_model: list[CostModelPayload]


# ---------------------------------------------------------------------------
# Helper: set up a minimal .sdd directory with test data
# ---------------------------------------------------------------------------


def _setup_sdd(tmp_path: Path, run_id: str = "run-001") -> Path:
    """Create a minimal .sdd/ tree with summary and metrics."""
    workdir = tmp_path / "project"
    workdir.mkdir()
    sdd = workdir / ".sdd"

    runs_dir = sdd / "runs" / run_id
    runs_dir.mkdir(parents=True)
    summary = {
        "run_id": run_id,
        "tasks_completed": 2,
        "tasks_failed": 1,
        "wall_clock_seconds": 300.0,
        "total_cost_usd": 4.82,
        "goal": "Implement user authentication",
    }
    (runs_dir / "summary.json").write_text(json.dumps(summary))

    metrics_dir = sdd / "metrics"
    metrics_dir.mkdir(parents=True)

    # Task metrics written to .sdd/archive/tasks.jsonl
    archive_dir = sdd / "archive"
    archive_dir.mkdir(parents=True)
    base_ts = 1700000000.0
    task_lines = []
    for i, (success, janitor, model) in enumerate(
        [
            (True, True, "claude-opus"),
            (True, True, "claude-sonnet"),
            (False, False, "codex"),
        ]
    ):
        task_data = {
            "task_id": f"T-{i + 1}",
            "role": ["backend", "frontend", "qa"][i],
            "model": model,
            "success": success,
            "janitor_passed": janitor,
            "cost_usd": [2.50, 1.32, 1.00][i],
            "start_time": base_ts + i * 60,
            "end_time": base_ts + i * 60 + 90,
        }
        task_lines.append(json.dumps(task_data))
    (archive_dir / "tasks.jsonl").write_text("\n".join(task_lines) + "\n")

    # Agent metrics
    for i in range(3):
        agent_data = {"agent_id": f"A-{i + 1}", "role": ["backend", "frontend", "qa"][i]}
        (metrics_dir / f"agent_{i}.json").write_text(json.dumps(agent_data))

    # Cost data
    cost_report: CostReportPayload = {
        "run_id": run_id,
        "total_spent_usd": 4.82,
        "per_model": [
            {"model": "claude-opus", "total_cost_usd": 2.50, "invocation_count": 3, "total_tokens": 15000},
            {"model": "claude-sonnet", "total_cost_usd": 1.32, "invocation_count": 5, "total_tokens": 22000},
            {"model": "codex", "total_cost_usd": 1.00, "invocation_count": 2, "total_tokens": 8000},
        ],
    }
    (metrics_dir / f"costs_{run_id}.json").write_text(json.dumps(cost_report))

    return workdir


# ---------------------------------------------------------------------------
# Tests: _fmt_duration
# ---------------------------------------------------------------------------


def test_fmt_duration_seconds_only() -> None:
    """_fmt_duration formats sub-minute durations as seconds."""
    assert _fmt_duration(0.0) == "0s"
    assert _fmt_duration(45.0) == "45s"
    assert _fmt_duration(59.0) == "59s"


def test_fmt_duration_minutes_and_seconds() -> None:
    """_fmt_duration formats minute-level durations correctly."""
    assert _fmt_duration(60.0) == "1m 0s"
    assert _fmt_duration(125.0) == "2m 5s"
    assert _fmt_duration(90.0) == "1m 30s"


def test_fmt_duration_hours_minutes_seconds() -> None:
    """_fmt_duration formats hour-level durations correctly."""
    assert _fmt_duration(3600.0) == "1h 0m 0s"
    assert _fmt_duration(3661.0) == "1h 1m 1s"
    assert _fmt_duration(7384.0) == "2h 3m 4s"


# ---------------------------------------------------------------------------
# Tests: _fmt_duration_short
# ---------------------------------------------------------------------------


def test_fmt_duration_short_seconds() -> None:
    """_fmt_duration_short returns seconds for sub-minute durations."""
    assert _fmt_duration_short(30.0) == "30 seconds"


def test_fmt_duration_short_minutes() -> None:
    """_fmt_duration_short returns minutes for sub-hour durations."""
    assert _fmt_duration_short(60.0) == "1 minutes"
    assert _fmt_duration_short(18 * 60) == "18 minutes"


def test_fmt_duration_short_hours() -> None:
    """_fmt_duration_short returns hours for hour+ durations."""
    assert _fmt_duration_short(3600.0) == "1h 0m"
    assert _fmt_duration_short(2 * 3600 + 15 * 60) == "2h 15m"


# ---------------------------------------------------------------------------
# Tests: _agent_summary
# ---------------------------------------------------------------------------


def test_agent_summary() -> None:
    """_agent_summary builds correct agent summary string."""
    report = _ExportReport(
        goal="",
        run_id="x",
        duration_s=0,
        total_cost_usd=0,
        tasks_completed=3,
        tasks_failed=0,
        agents_spawned=3,
        task_rows=[
            _TaskRow("a", "backend", "done", "claude-opus", 10.0, 1.0, True),
            _TaskRow("b", "frontend", "done", "claude-sonnet", 20.0, 0.5, True),
            _TaskRow("c", "qa", "done", "codex", 30.0, 0.3, True),
        ],
    )
    result = _agent_summary(report)
    assert "3 (" in result
    assert "claude-opus x1" in result
    assert "claude-sonnet x1" in result
    assert "codex x1" in result


def test_agent_summary_duplicates() -> None:
    """_agent_summary groups by model name with counts."""
    report = _ExportReport(
        goal="",
        run_id="x",
        duration_s=0,
        total_cost_usd=0,
        tasks_completed=3,
        tasks_failed=0,
        agents_spawned=5,
        task_rows=[
            _TaskRow("a", "backend", "done", "claude-opus", 10.0, 1.0, True),
            _TaskRow("b", "backend", "done", "claude-opus", 20.0, 1.5, True),
            _TaskRow("c", "frontend", "done", "claude-sonnet", 30.0, 0.5, True),
        ],
    )
    result = _agent_summary(report)
    assert "claude-opus x2" in result
    assert "claude-sonnet x1" in result


# ---------------------------------------------------------------------------
# Tests: _sequential_estimate
# ---------------------------------------------------------------------------


def test_sequential_estimate() -> None:
    """_sequential_estimate sums individual task durations."""
    report = _ExportReport(
        goal="",
        run_id="x",
        duration_s=0,
        total_cost_usd=0,
        tasks_completed=2,
        tasks_failed=0,
        agents_spawned=2,
        task_rows=[
            _TaskRow("a", "backend", "done", "model-a", 60.0, 1.0, True),
            _TaskRow("b", "frontend", "done", "model-b", 5400.0, 2.0, True),
        ],
    )
    result = _sequential_estimate(report)
    # 60 + 5400 = 5460 seconds = 1h 31m
    assert "1h" in result


# ---------------------------------------------------------------------------
# Tests: export_run_report - HTML output
# ---------------------------------------------------------------------------


def test_export_html_basic(tmp_path: Path) -> None:
    """export_run_report generates valid HTML for a basic run."""
    workdir = _setup_sdd(tmp_path)
    result = export_run_report(workdir=workdir, fmt="html")

    assert isinstance(result, Path)
    content = result.read_text(encoding="utf-8")

    # Structure checks
    assert "<!DOCTYPE html>" in content
    assert "<html" in content
    assert "<head>" in content
    assert "<body>" in content
    assert "</html>" in content

    # Content checks
    assert "Bernstein Run Report" in content
    assert "Implement user authentication" in content
    assert "2 completed, 1 failed" in content
    assert "$4.82" in content
    assert "2/3 gates passed" in content

    # Inline CSS (no external stylesheets)
    assert "<style>" in content
    assert 'href="http' not in content.lower() or "github" in content.lower()

    # Footer link
    assert "github.com/sipyourdrink-ltd/bernstein" in content


def test_export_html_contains_task_table(tmp_path: Path) -> None:
    """HTML report includes a task breakdown table."""
    workdir = _setup_sdd(tmp_path)
    result = export_run_report(workdir=workdir, fmt="html")
    content = result.read_text(encoding="utf-8")

    assert "<table>" in content
    assert "<th>Task</th>" in content
    assert "<th>Role</th>" in content
    assert "<th>Status</th>" in content
    assert "<th>Model</th>" in content
    assert "<th>Duration</th>" in content
    assert "<th>Cost</th>" in content


def test_export_html_contains_cost_table(tmp_path: Path) -> None:
    """HTML report includes a cost breakdown table."""
    workdir = _setup_sdd(tmp_path)
    result = export_run_report(workdir=workdir, fmt="html")
    content = result.read_text(encoding="utf-8")

    assert "<th>Model</th>" in content
    assert "claude-opus" in content
    assert "claude-sonnet" in content
    assert "codex" in content


def test_load_report_data_uses_runtime_cost_fallback(tmp_path: Path) -> None:
    workdir = _setup_sdd(tmp_path)
    run_id = "run-001"
    (workdir / ".sdd" / "metrics" / f"costs_{run_id}.json").unlink()

    fallback_dir = workdir / ".sdd" / "runtime" / "costs"
    fallback_dir.mkdir(parents=True)
    fallback_cost_report: CostReportPayload = {
        "total_spent_usd": 7.25,
        "per_model": [
            {
                "model": "fallback-model",
                "total_cost_usd": 7.25,
                "invocation_count": 2,
                "total_tokens": 1234,
            }
        ],
    }
    (fallback_dir / f"{run_id}.json").write_text(json.dumps(fallback_cost_report))

    report = _load_report_data(workdir, run_id)

    assert report.total_cost_usd == pytest.approx(7.25)
    assert len(report.model_costs) == 1
    assert report.model_costs[0].model == "fallback-model"


def test_export_html_contains_agent_stats(tmp_path: Path) -> None:
    """HTML report includes a per-agent stats table."""
    workdir = _setup_sdd(tmp_path)
    result = export_run_report(workdir=workdir, fmt="html")
    content = result.read_text(encoding="utf-8")

    assert "<th>Tasks</th>" in content


# ---------------------------------------------------------------------------
# Tests: export_run_report - Markdown output
# ---------------------------------------------------------------------------


def test_export_md_basic(tmp_path: Path) -> None:
    """export_run_report generates valid Markdown."""
    workdir = _setup_sdd(tmp_path)
    result = export_run_report(workdir=workdir, fmt="md")

    assert isinstance(result, Path)
    content = result.read_text(encoding="utf-8")

    assert "# Bernstein Run Report" in content
    assert "Implement user authentication" in content
    assert "2 completed, 1 failed" in content
    assert "$4.82" in content


def test_export_md_contains_sections(tmp_path: Path) -> None:
    """Markdown report includes all required sections."""
    workdir = _setup_sdd(tmp_path)
    result = export_run_report(workdir=workdir, fmt="md")
    content = result.read_text(encoding="utf-8")

    assert "## Task Breakdown" in content
    assert "## Quality Gates" in content
    assert "## Cost Breakdown by Model" in content
    assert "## Per-Agent Stats" in content


def test_export_md_format_flag(tmp_path: Path) -> None:
    """--format md produces Markdown, not HTML."""
    workdir = _setup_sdd(tmp_path)
    result = export_run_report(workdir=workdir, fmt="md")
    content = result.read_text(encoding="utf-8")

    assert "# Bernstein Run Report" in content
    assert "<html>" not in content


# ---------------------------------------------------------------------------
# Tests: output path
# ---------------------------------------------------------------------------


def test_export_custom_output_path(tmp_path: Path) -> None:
    """export_run_report respects explicit output path."""
    workdir = _setup_sdd(tmp_path)
    out = tmp_path / "reports" / "my-report.html"
    result = export_run_report(workdir=workdir, fmt="html", output_path=str(out))

    assert result == out
    assert result.exists()
    assert "<!DOCTYPE html>" in result.read_text(encoding="utf-8")


def test_export_default_writes_to_sdd_reports(tmp_path: Path) -> None:
    """export_run_report writes to .sdd/reports/ by default."""
    workdir = _setup_sdd(tmp_path)
    result = export_run_report(workdir=workdir, fmt="html")

    expected = workdir / ".sdd" / "reports" / "run-001.html"
    assert result == expected
    assert result.exists()


# ---------------------------------------------------------------------------
# Tests: run-id option
# ---------------------------------------------------------------------------


def test_export_specific_run_id(tmp_path: Path) -> None:
    """export_run_report respects explicit run_id."""
    workdir = _setup_sdd(tmp_path, run_id="my-run-xyz")
    result = export_run_report(workdir=workdir, run_id="my-run-xyz", fmt="html")

    assert "my-run-xyz" in str(result)


# ---------------------------------------------------------------------------
# Tests: no data found
# ---------------------------------------------------------------------------


def test_export_no_data(tmp_path: Path) -> None:
    """export_run_report raises ValueError when no run data exists."""
    workdir = tmp_path / "empty_project"
    workdir.mkdir()

    with pytest.raises(ValueError, match="No run data found"):
        export_run_report(workdir=workdir)


# ---------------------------------------------------------------------------
# Tests: 500KB size limit
# ---------------------------------------------------------------------------


def test_size_limit_enforced(tmp_path: Path) -> None:
    """export_run_report raises ValueError when report exceeds 500KB."""
    # Create a mock report and directly call the render function with oversized data
    large_rows = [
        _TaskRow(
            title=f"Very long task title number {i} with extra padding to make it much larger so we exceed the limit",
            role="backend",
            status="done",
            model="test-model",
            duration_s=60.0,
            cost_usd=0.01,
            janitor_passed=True,
        )
        for i in range(3000)
    ]

    report = _ExportReport(
        goal="",
        run_id="big-run",
        duration_s=120000.0,
        total_cost_usd=999.99,
        tasks_completed=3000,
        tasks_failed=0,
        agents_spawned=10,
        task_rows=large_rows,
    )

    from bernstein.core.observability.run_export import _render_html

    html_content = _render_html(report)

    assert len(html_content.encode("utf-8")) > MAX_FILE_SIZE


def test_size_limit_constant() -> None:
    """MAX_FILE_SIZE is 500 * 1024 bytes."""
    assert MAX_FILE_SIZE == 500 * 1024


# ---------------------------------------------------------------------------
# Tests: Markdown footer link
# ---------------------------------------------------------------------------


def test_export_md_footer_link(tmp_path: Path) -> None:
    """Markdown report includes repository footer link."""
    workdir = _setup_sdd(tmp_path)
    result = export_run_report(workdir=workdir, fmt="md")
    content = result.read_text(encoding="utf-8")

    assert "github.com/sipyourdrink-ltd/bernstein" in content


# ---------------------------------------------------------------------------
# Tests: Unsupported format
# ---------------------------------------------------------------------------


def test_export_unsupported_format(tmp_path: Path) -> None:
    """export_run_report raises ValueError for unsupported formats."""
    workdir = _setup_sdd(tmp_path)

    with pytest.raises(ValueError, match="Unsupported format"):
        export_run_report(workdir=workdir, fmt="pdf")


# ---------------------------------------------------------------------------
# Tests: --last and --run-id mutual exclusivity (CLI level)
# ---------------------------------------------------------------------------


def test_export_cli_last_and_run_id_mutual_exclusion(tmp_path: Path) -> None:
    """CLI rejects both --last and --run-id simultaneously."""
    workdir = _setup_sdd(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        export_cmd,
        ["--last", "--run-id", "abc123", "--workdir", str(workdir)],
    )
    assert result.exit_code != 0
    assert "Cannot specify both" in result.output
