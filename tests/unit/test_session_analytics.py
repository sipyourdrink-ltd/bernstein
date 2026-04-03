"""Tests for session_analytics — trace analysis and insights."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bernstein.session_analytics import (
    AnalyticsReport,
    _calculate_helpfulness,
    _extract_goal,
    _infer_category,
    analyze_traces,
    format_report,
    parse_trace,
    save_report,
)


@pytest.fixture()
def sample_trace_file(tmp_path: Path) -> Path:
    """Create a sample trace file."""
    f = tmp_path / "trace1.jsonl"
    lines = [
        json.dumps(
            {
                "trace_id": "trace-001",
                "session_id": "session-001",
                "task_ids": ["task-001"],
                "agent_role": "backend",
                "model": "sonnet",
                "spawn_ts": 1700000000.0,
                "end_ts": 1700003600.0,
                "outcome": "success",
                "task_snapshots": [{"title": "Fix login bug"}],
            }
        ),
        json.dumps(
            {
                "type": "orient",
                "timestamp": 1700000010.0,
                "detail": "Reading codebase",
                "files": ["src/auth.py"],
                "tokens": 500,
            }
        ),
        json.dumps(
            {
                "type": "edit",
                "timestamp": 1700000100.0,
                "detail": "Fixing auth logic",
                "files": ["src/auth.py"],
                "tokens": 200,
            }
        ),
        json.dumps(
            {
                "type": "verify",
                "timestamp": 1700000200.0,
                "detail": "Running tests",
                "files": [],
                "tokens": 100,
            }
        ),
        json.dumps(
            {
                "type": "complete",
                "timestamp": 1700003600.0,
                "detail": "Task completed",
                "files": [],
                "tokens": 0,
            }
        ),
    ]
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


@pytest.fixture()
def failed_trace_file(tmp_path: Path) -> Path:
    """Create a failed trace file."""
    f = tmp_path / "trace2.jsonl"
    lines = [
        json.dumps(
            {
                "trace_id": "trace-002",
                "session_id": "session-002",
                "task_ids": ["task-002"],
                "agent_role": "qa",
                "model": "opus",
                "spawn_ts": 1700010000.0,
                "end_ts": 1700010300.0,
                "outcome": "failed",
                "task_snapshots": [{"title": "Add test coverage"}],
            }
        ),
        json.dumps(
            {
                "type": "orient",
                "timestamp": 1700010010.0,
                "detail": "Analyzing test files",
                "files": [],
                "tokens": 300,
            }
        ),
        json.dumps(
            {
                "type": "fail",
                "timestamp": 1700010300.0,
                "detail": "Could not run tests",
                "files": [],
                "tokens": 0,
            }
        ),
    ]
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


@pytest.fixture()
def empty_trace_file(tmp_path: Path) -> Path:
    """Create an empty trace file."""
    f = tmp_path / "trace3.jsonl"
    f.write_text("", encoding="utf-8")
    return f


@pytest.fixture()
def traces_dir(tmp_path: Path, sample_trace_file: Path, failed_trace_file: Path) -> Path:
    """Create a traces directory with multiple trace files."""
    traces = tmp_path / "traces"
    traces.mkdir()

    # Copy trace files
    (traces / "trace1.jsonl").write_text(sample_trace_file.read_text())
    (traces / "trace2.jsonl").write_text(failed_trace_file.read_text())

    return traces


# --- TestInferCategory ---


class TestInferCategory:
    def test_bug_fix(self) -> None:
        assert _infer_category("fix login bug") == "bug_fix"

    def test_feature(self) -> None:
        assert _infer_category("add new feature") == "feature"

    def test_refactor(self) -> None:
        assert _infer_category("refactor code structure") == "refactor"

    def test_test(self) -> None:
        assert _infer_category("test coverage for auth") == "test"

    def test_docs(self) -> None:
        assert _infer_category("update readme documentation") == "docs"

    def test_config(self) -> None:
        assert _infer_category("change config settings") == "config"

    def test_ci(self) -> None:
        assert _infer_category("ci pipeline build") == "ci"

    def test_other(self) -> None:
        assert _infer_category("something else entirely") == "other"


# --- TestExtractGoal ---


class TestExtractGoal:
    def test_with_title(self) -> None:
        goal = _extract_goal([{"title": "Fix login bug"}])
        assert goal == "Fix login bug"

    def test_with_description(self) -> None:
        goal = _extract_goal([{"description": "Fix the login flow"}])
        assert goal == "Fix the login flow"

    def test_empty_snapshots(self) -> None:
        goal = _extract_goal([])
        assert goal == "(unknown)"


# --- TestCalculateHelpfulness ---


class TestCalculateHelpfulness:
    def test_success_with_edits(self) -> None:
        score = _calculate_helpfulness("success", 5, 2, 10, 300.0)
        assert score > 50

    def test_failed(self) -> None:
        score = _calculate_helpfulness("failed", 0, 0, 5, 60.0)
        assert score < 50

    def test_with_verification(self) -> None:
        score = _calculate_helpfulness("success", 5, 5, 20, 300.0)
        assert score >= 80  # Success + verification ratio

    def test_too_many_edits(self) -> None:
        score = _calculate_helpfulness("success", 50, 0, 60, 300.0)
        assert score < 90  # Penalty for too many edits

    def test_clamped_to_100(self) -> None:
        score = _calculate_helpfulness("success", 5, 3, 10, 120.0)
        assert score <= 100

    def test_clamped_to_0(self) -> None:
        score = _calculate_helpfulness("failed", 0, 0, 5, 10.0)
        assert score >= 0


# --- TestParseTrace ---


class TestParseTrace:
    def test_parses_success(self, sample_trace_file: Path) -> None:
        meta = parse_trace(sample_trace_file)
        assert meta is not None
        assert meta.trace_id == "trace-001"
        assert meta.session_id == "session-001"
        assert meta.agent_role == "backend"
        assert meta.model == "sonnet"
        assert meta.outcome == "success"
        assert meta.edits_made == 1
        assert meta.verifications == 1
        assert meta.steps_count == 4
        assert meta.tokens_used == 800
        assert "src/auth.py" in meta.files_touched

    def test_parses_failed(self, failed_trace_file: Path) -> None:
        meta = parse_trace(failed_trace_file)
        assert meta is not None
        assert meta.outcome == "failed"
        assert meta.agent_role == "qa"
        assert meta.edits_made == 0
        assert meta.verifications == 0

    def test_empty_file(self, empty_trace_file: Path) -> None:
        meta = parse_trace(empty_trace_file)
        assert meta is None

    def test_missing_file(self, tmp_path: Path) -> None:
        meta = parse_trace(tmp_path / "missing.jsonl")
        assert meta is None

    def test_inferred_goal(self, sample_trace_file: Path) -> None:
        meta = parse_trace(sample_trace_file)
        assert meta is not None
        assert meta.goal == "Fix login bug"

    def test_inferred_category(self, sample_trace_file: Path) -> None:
        meta = parse_trace(sample_trace_file)
        assert meta is not None
        assert meta.category == "bug_fix"

    def test_duration_calculated(self, sample_trace_file: Path) -> None:
        meta = parse_trace(sample_trace_file)
        assert meta is not None
        assert meta.duration_seconds == 3600.0


# --- TestAnalyzeTraces ---


class TestAnalyzeTraces:
    def test_analyzes_directory(self, traces_dir: Path) -> None:
        report = analyze_traces(traces_dir)
        assert report.total_sessions == 2
        assert report.successful_sessions == 1
        assert report.failed_sessions == 1
        assert report.avg_helpfulness > 0

    def test_category_breakdown(self, traces_dir: Path) -> None:
        report = analyze_traces(traces_dir)
        assert "bug_fix" in report.category_breakdown
        assert report.category_breakdown["bug_fix"] == 1

    def test_role_breakdown(self, traces_dir: Path) -> None:
        report = analyze_traces(traces_dir)
        assert "backend" in report.role_breakdown
        assert "qa" in report.role_breakdown

    def test_model_breakdown(self, traces_dir: Path) -> None:
        report = analyze_traces(traces_dir)
        assert "sonnet" in report.model_breakdown
        assert "opus" in report.model_breakdown

    def test_empty_directory(self, tmp_path: Path) -> None:
        report = analyze_traces(tmp_path / "traces")
        assert report.total_sessions == 0
        assert report.avg_helpfulness == 0.0

    def test_top_goals(self, traces_dir: Path) -> None:
        report = analyze_traces(traces_dir)
        assert len(report.top_goals) > 0


# --- TestFormatReport ---


class TestFormatReport:
    def test_format_empty(self) -> None:
        report = AnalyticsReport(
            generated_at=datetime.now(tz=UTC),
            total_sessions=0,
            successful_sessions=0,
            failed_sessions=0,
            avg_duration_seconds=0.0,
            avg_helpfulness=0.0,
            category_breakdown={},
            role_breakdown={},
            model_breakdown={},
            top_goals=[],
            sessions=[],
        )
        output = format_report(report)
        assert "SESSION ANALYTICS REPORT" in output
        assert "Total sessions:      0" in output

    def test_format_with_data(self, traces_dir: Path) -> None:
        report = analyze_traces(traces_dir)
        output = format_report(report)
        assert "Total sessions:      2" in output
        assert "TASK CATEGORIES" in output
        assert "AGENT ROLES" in output
        assert "MODELS" in output


# --- TestSaveReport ---


class TestSaveReport:
    def test_saves_file(self, traces_dir: Path, tmp_path: Path) -> None:
        report = analyze_traces(traces_dir)
        reports_dir = tmp_path / "reports"
        path = save_report(report, reports_dir)
        assert path.exists()
        assert path.suffix == ".md"
        content = path.read_text()
        assert "SESSION ANALYTICS REPORT" in content
