"""Tests for bernstein.away_summary — JSONL reading, summary generation, formatting."""

from __future__ import annotations

import json
import time
from pathlib import Path

from bernstein.away_summary import (
    AwaySummary,
    _collect_api_usage_since,
    _collect_error_records,
    _estimate_cost_from_api_usage,
    _fetch_task_completion_events,
    _read_jsonl,
    format_away_report,
    generate_away_summary,
)

# ---------------------------------------------------------------------------
# _read_jsonl
# ---------------------------------------------------------------------------


class TestReadJsonl:
    def test_returns_empty_list_for_missing_file(self, tmp_path: Path) -> None:
        result = _read_jsonl(tmp_path / "nonexistent.jsonl", since_ts=0.0)
        assert result == []

    def test_returns_empty_list_for_empty_file(self, tmp_path: Path) -> None:
        filepath = tmp_path / "empty.jsonl"
        filepath.write_text("")
        result = _read_jsonl(filepath, since_ts=0.0)
        assert result == []

    def test_skips_lines_before_since_ts(self, tmp_path: Path) -> None:
        filepath = tmp_path / "metrics.jsonl"
        lines = [
            json.dumps({"timestamp": 100.0, "value": 1}),
            json.dumps({"timestamp": 200.0, "value": 2}),
            json.dumps({"timestamp": 300.0, "value": 3}),
        ]
        filepath.write_text("\n".join(lines))
        result = _read_jsonl(filepath, since_ts=150.0)
        assert len(result) == 2
        assert result[0]["value"] == 2
        assert result[1]["value"] == 3

    def test_skips_malformed_json_lines(self, tmp_path: Path) -> None:
        filepath = tmp_path / "bad.jsonl"
        lines = [
            "not json",
            json.dumps({"timestamp": 100.0, "value": 10}),
        ]
        filepath.write_text("\n".join(lines))
        result = _read_jsonl(filepath, since_ts=0.0)
        assert len(result) == 1
        assert result[0]["value"] == 10

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        filepath = tmp_path / "sparse.jsonl"
        filepath.write_text("\n\n\n")
        result = _read_jsonl(filepath, since_ts=0.0)
        assert result == []


# ---------------------------------------------------------------------------
# _fetch_task_completion_events
# ---------------------------------------------------------------------------


class TestFetchTaskCompletionEvents:
    def test_returns_empty_when_no_tasks_jsonl(self, tmp_path: Path) -> None:
        result = _fetch_task_completion_events(tmp_path, since_ts=0.0)
        assert result == []

    def test_filters_done_and_failed_only(self, tmp_path: Path) -> None:
        """Only done/failed events are returned, open/claimed are excluded."""
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        tasks_jsonl = runtime / "tasks.jsonl"
        now = time.time()
        lines = [
            json.dumps({"timestamp": now - 10, "id": "a", "status": "open", "title": "T0"}),
            json.dumps({"timestamp": now - 5, "id": "b", "status": "done", "title": "T1"}),
            json.dumps({"timestamp": now - 2, "id": "c", "status": "failed", "title": "T2"}),
        ]
        tasks_jsonl.write_text("\n".join(lines))
        result = _fetch_task_completion_events(tmp_path, since_ts=now - 20)
        assert len(result) == 2
        assert result[0]["id"] == "b"
        assert result[1]["id"] == "c"

    def test_returns_empty_for_old_tasks(self, tmp_path: Path) -> None:
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        tasks_jsonl = runtime / "tasks.jsonl"
        old_ts = time.time() - 1000
        tasks_jsonl.write_text(json.dumps({"timestamp": old_ts, "id": "old", "status": "done", "title": "OldTask"}))
        result = _fetch_task_completion_events(tmp_path, since_ts=time.time() - 100)
        assert result == []

    def test_handles_missing_tasks_jsonl(self, tmp_path: Path) -> None:
        """Returns empty list when tasks.jsonl does not exist."""
        result = _fetch_task_completion_events(tmp_path, since_ts=time.time() - 60)
        assert result == []


# ---------------------------------------------------------------------------
# _collect_api_usage_since
# ---------------------------------------------------------------------------


class TestCollectApiUsageSince:
    def test_empty_metrics_dir(self, tmp_path: Path) -> None:
        result = _collect_api_usage_since(tmp_path / "missing", since_ts=0.0)
        assert result == []

    def test_collects_matching_records(self, tmp_path: Path) -> None:
        metrics = tmp_path / "metrics"
        metrics.mkdir()
        now = time.time()
        lines = [
            json.dumps(
                {
                    "timestamp": now - 5,
                    "metric_type": "api_usage",
                    "value": 0.0,
                    "labels": {"model": "sonnet"},
                }
            ),
            json.dumps(
                {
                    "timestamp": now - 100,
                    "metric_type": "api_usage",
                    "value": 0.0,
                    "labels": {"model": "opus"},
                }
            ),
        ]
        (metrics / "api_usage_2026-04-02.jsonl").write_text("\n".join(lines))
        result = _collect_api_usage_since(metrics, since_ts=now - 10)
        assert len(result) == 1
        assert result[0]["labels"]["model"] == "sonnet"

    def test_ignores_non_api_usage_files(self, tmp_path: Path) -> None:
        metrics = tmp_path / "metrics"
        metrics.mkdir()
        (metrics / "error_rate_2026-04-02.jsonl").write_text(
            json.dumps({"timestamp": time.time(), "metric_type": "error_rate"})
        )
        result = _collect_api_usage_since(metrics, since_ts=0.0)
        assert result == []


# ---------------------------------------------------------------------------
# _collect_error_records
# ---------------------------------------------------------------------------


class TestCollectErrorRecords:
    def test_empty_metrics_dir(self, tmp_path: Path) -> None:
        result = _collect_error_records(tmp_path / "missing", since_ts=0.0)
        assert result == []

    def test_collects_error_records(self, tmp_path: Path) -> None:
        metrics = tmp_path / "metrics"
        metrics.mkdir()
        now = time.time()
        (metrics / "error_rate_2026-04-02.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": now,
                    "metric_type": "error_rate",
                    "labels": {"model": "haiku"},
                }
            )
        )
        result = _collect_error_records(metrics, since_ts=now - 60)
        assert len(result) == 1
        assert result[0]["labels"]["model"] == "haiku"


# ---------------------------------------------------------------------------
# _estimate_cost_from_api_usage
# ---------------------------------------------------------------------------


class TestEstimateCostFromApiUsage:
    def test_empty_list_returns_zero(self) -> None:
        assert _estimate_cost_from_api_usage([]) == 0.0

    def test_uses_value_field(self, tmp_path: Path) -> None:
        records = [{"value": 0.42, "labels": {}}]
        result = _estimate_cost_from_api_usage(records)
        assert abs(result - 0.42) < 1e-6

    def test_uses_nested_cost_usd(self, tmp_path: Path) -> None:
        records = [{"labels": {"cost_usd": 1.23}}]
        result = _estimate_cost_from_api_usage(records)
        assert abs(result - 1.23) < 1e-6


# ---------------------------------------------------------------------------
# generate_away_summary
# ---------------------------------------------------------------------------


class TestGenerateAwaySummary:
    def test_empty_period_returns_zero_counts(self, tmp_path: Path) -> None:
        """When no metrics or tasks exist since since_ts, summary has zero counts."""
        sdd = tmp_path / ".sdd"
        (sdd / "metrics").mkdir(parents=True)
        (sdd / "runtime").mkdir(parents=True)

        since_ts = time.time() - 10  # 10 seconds ago
        summary = generate_away_summary(since_ts=since_ts, workdir=tmp_path)
        assert summary.completed_tasks == 0
        assert summary.failed_tasks == 0
        assert abs(summary.cost_spent) < 1e-6
        assert summary.duration_s > 0
        assert "Nothing happened" in summary.summary_text
        assert summary.events == []

    def test_one_completed_task(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        runtime = sdd / "runtime"
        metrics = sdd / "metrics"
        runtime.mkdir(parents=True)
        metrics.mkdir(parents=True)

        now = time.time()
        since_ts = now - 60
        (runtime / "tasks.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": now - 10,
                    "id": "abc123def",
                    "title": "Fix login bug",
                    "status": "done",
                    "result_summary": "Fixed the login validation",
                }
            )
        )
        summary = generate_away_summary(since_ts=since_ts, workdir=tmp_path)
        assert summary.completed_tasks == 1
        assert summary.failed_tasks == 0
        assert any("Fix login bug" in evt for evt in summary.events)
        assert "Fix login bug" in summary.summary_text

    def test_one_failed_task(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        runtime = sdd / "runtime"
        metrics = sdd / "metrics"
        runtime.mkdir(parents=True)
        metrics.mkdir(parents=True)

        now = time.time()
        since_ts = now - 60
        (runtime / "tasks.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": now - 5,
                    "id": "failtask1",
                    "title": "Implement billing",
                    "status": "failed",
                    "result_summary": "Could not connect to API",
                }
            )
        )
        summary = generate_away_summary(since_ts=since_ts, workdir=tmp_path)
        assert summary.completed_tasks == 0
        assert summary.failed_tasks == 1
        assert any("failtask1" in evt for evt in summary.events)
        assert "failed" in summary.summary_text

    def test_multiple_mixed_tasks(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        runtime = sdd / "runtime"
        metrics = sdd / "metrics"
        runtime.mkdir(parents=True)
        metrics.mkdir(parents=True)

        now = time.time()
        since_ts = now - 120
        events = [
            {"timestamp": now - 100, "id": "t1", "title": "Task one", "status": "done", "result_summary": "OK"},
            {"timestamp": now - 80, "id": "t2", "title": "Task two", "status": "done", "result_summary": "OK"},
            {"timestamp": now - 60, "id": "t3", "title": "Task three", "status": "failed", "result_summary": "Error"},
            {"timestamp": now - 40, "id": "t4", "title": "Task four", "status": "done", "result_summary": "OK"},
        ]
        (runtime / "tasks.jsonl").write_text("\n".join(json.dumps(e) for e in events))
        summary = generate_away_summary(since_ts=since_ts, workdir=tmp_path)
        assert summary.completed_tasks == 3
        assert summary.failed_tasks == 1
        assert len(summary.events) == 4
        assert "3 tasks completed" in summary.summary_text
        assert "1 task failed" in summary.summary_text

    def test_cost_accounted_from_api_usage(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        runtime = sdd / "runtime"
        metrics = sdd / "metrics"
        runtime.mkdir(parents=True)
        metrics.mkdir(parents=True)

        now = time.time()
        since_ts = now - 60
        api_line = json.dumps(
            {
                "timestamp": now - 10,
                "metric_type": "api_usage",
                "value": 0.05,
                "labels": {"model": "sonnet", "success": "True"},
            }
        )
        (metrics / "api_usage_2026-04-02.jsonl").write_text(api_line)
        (runtime / "tasks.jsonl").write_text("")

        summary = generate_away_summary(since_ts=since_ts, workdir=tmp_path)
        assert abs(summary.cost_spent - 0.05) < 1e-6

    def test_error_records_appended_to_events(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        runtime = sdd / "runtime"
        metrics = sdd / "metrics"
        runtime.mkdir(parents=True)
        metrics.mkdir(parents=True)

        now = time.time()
        since_ts = now - 60
        (metrics / "error_rate_2026-04-02.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": now - 5,
                    "metric_type": "error_rate",
                    "labels": {"model": "haiku"},
                }
            )
        )
        (runtime / "tasks.jsonl").write_text("")

        summary = generate_away_summary(since_ts=since_ts, workdir=tmp_path)
        assert any("haiku" in evt and "Provider error" in evt for evt in summary.events)


# ---------------------------------------------------------------------------
# format_away_report
# ---------------------------------------------------------------------------


class TestFormatAwayReport:
    def test_minimal_summary_empty_no_events(self) -> None:
        summary = AwaySummary(
            completed_tasks=0,
            failed_tasks=0,
            cost_spent=0.0,
            duration_s=0.0,
        )
        report = format_away_report(summary)
        assert "Since You Were Away" in report
        assert "tasks completed" in report
        assert "No events recorded" in report

    def test_report_shows_completed_tasks(self) -> None:
        summary = AwaySummary(
            completed_tasks=5,
            failed_tasks=2,
            cost_spent=0.5,
            duration_s=3600.0,
            events=["Task abc123 completed: Fix login"],
        )
        report = format_away_report(summary)
        assert "5" in report
        assert "tasks completed" in report
        assert "2" in report
        assert "tasks failed" in report
        assert "$0.5000" in report
        assert "abc123" in report

        assert "Events" in report

    def test_report_shows_zero_cost_when_empty(self) -> None:
        summary = AwaySummary(
            completed_tasks=0,
            failed_tasks=0,
            cost_spent=0.0,
            duration_s=120.0,
        )
        report = format_away_report(summary)
        assert "$0.0000" in report

    def test_report_shows_events(self) -> None:
        summary = AwaySummary(
            completed_tasks=2,
            failed_tasks=0,
            cost_spent=0.02,
            duration_s=300,
            events=["Task x completed", "Task y completed"],
        )
        report = format_away_report(summary)
        assert "Task x completed" in report
        assert "Task y completed" in report
