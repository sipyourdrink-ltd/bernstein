"""Tests for natural language analytics queries (#677)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.observability.nl_analytics import (
    QueryIntent,
    QueryResult,
    execute_query,
    format_answer,
    get_available_metrics,
    parse_nl_query,
    render_analytics_table,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_tasks_jsonl(tmp_path: Path, records: list[dict[str, object]]) -> Path:
    """Write task records to a JSONL file and return the path."""
    archive = tmp_path / "tasks.jsonl"
    with archive.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return archive


SAMPLE_TASKS: list[dict[str, object]] = [
    {
        "id": "t1",
        "status": "completed",
        "cost": 0.50,
        "duration": 120.0,
        "agent": "claude",
        "role": "backend",
        "model": "opus",
        "quality_score": 0.95,
        "created_at": "2026-04-10T10:00:00+00:00",
    },
    {
        "id": "t2",
        "status": "failed",
        "cost": 0.30,
        "duration": 60.0,
        "agent": "codex",
        "role": "frontend",
        "model": "gpt-4",
        "quality_score": 0.40,
        "created_at": "2026-04-11T08:00:00+00:00",
    },
    {
        "id": "t3",
        "status": "completed",
        "cost": 1.20,
        "duration": 300.0,
        "agent": "claude",
        "role": "backend",
        "model": "opus",
        "quality_score": 0.88,
        "created_at": "2026-04-12T06:00:00+00:00",
    },
    {
        "id": "t4",
        "status": "completed",
        "cost": 0.10,
        "duration": 30.0,
        "agent": "gemini",
        "role": "qa",
        "model": "gemini-pro",
        "quality_score": 0.72,
        "created_at": "2026-04-12T09:00:00+00:00",
    },
]


# ---------------------------------------------------------------------------
# QueryIntent — frozen dataclass
# ---------------------------------------------------------------------------


class TestQueryIntent:
    def test_frozen(self) -> None:
        intent = QueryIntent(metric="cost", aggregation="sum")
        with pytest.raises(AttributeError):
            intent.metric = "duration"  # type: ignore[misc]

    def test_defaults(self) -> None:
        intent = QueryIntent(metric="cost", aggregation="avg")
        assert intent.time_range is None
        assert intent.group_by is None
        assert intent.filter_by == {}

    def test_all_fields(self) -> None:
        intent = QueryIntent(
            metric="duration",
            aggregation="max",
            time_range="last week",
            group_by="agent",
            filter_by={"status": "completed"},
        )
        assert intent.metric == "duration"
        assert intent.aggregation == "max"
        assert intent.time_range == "last week"
        assert intent.group_by == "agent"
        assert intent.filter_by == {"status": "completed"}


# ---------------------------------------------------------------------------
# QueryResult — frozen dataclass
# ---------------------------------------------------------------------------


class TestQueryResult:
    def test_frozen(self) -> None:
        intent = QueryIntent(metric="cost", aggregation="sum")
        result = QueryResult(query="test", intent=intent, value=1.0)
        with pytest.raises(AttributeError):
            result.value = 2.0  # type: ignore[misc]

    def test_defaults(self) -> None:
        intent = QueryIntent(metric="tasks", aggregation="count")
        result = QueryResult(query="q", intent=intent)
        assert result.value is None
        assert result.rows == ()
        assert result.summary == ""


# ---------------------------------------------------------------------------
# parse_nl_query — keyword matching
# ---------------------------------------------------------------------------


class TestParseNlQuery:
    def test_most_expensive(self) -> None:
        intent = parse_nl_query("what is the most expensive task?")
        assert intent.metric == "cost"
        assert intent.aggregation == "max"

    def test_how_many(self) -> None:
        intent = parse_nl_query("how many tasks failed?")
        assert intent.aggregation == "count"
        assert intent.metric == "tasks"
        assert intent.filter_by.get("status") == "failed"

    def test_average_cost(self) -> None:
        intent = parse_nl_query("what is the average cost?")
        assert intent.metric == "cost"
        assert intent.aggregation == "avg"

    def test_total_duration(self) -> None:
        intent = parse_nl_query("total duration of all tasks")
        assert intent.metric == "duration"
        assert intent.aggregation == "sum"

    def test_last_week_time_range(self) -> None:
        intent = parse_nl_query("how many tasks completed last week?")
        assert intent.time_range == "last week"

    def test_today_time_range(self) -> None:
        intent = parse_nl_query("show tasks today")
        assert intent.time_range == "today"

    def test_last_month_time_range(self) -> None:
        intent = parse_nl_query("total cost last month")
        assert intent.time_range == "last month"

    def test_group_by_agent(self) -> None:
        intent = parse_nl_query("total cost by agent")
        assert intent.group_by == "agent"

    def test_group_by_role(self) -> None:
        intent = parse_nl_query("average duration per role")
        assert intent.group_by == "role"

    def test_group_by_status(self) -> None:
        intent = parse_nl_query("count tasks by status")
        assert intent.group_by == "status"

    def test_group_by_model(self) -> None:
        intent = parse_nl_query("average cost by model")
        assert intent.group_by == "model"

    def test_status_filter_failed(self) -> None:
        intent = parse_nl_query("how many tasks failed?")
        assert intent.filter_by.get("status") == "failed"

    def test_status_filter_completed(self) -> None:
        intent = parse_nl_query("total cost of completed tasks")
        assert intent.filter_by.get("status") == "completed"

    def test_status_filter_running(self) -> None:
        intent = parse_nl_query("how many running tasks?")
        assert intent.filter_by.get("status") == "in_progress"

    def test_minimum_quality(self) -> None:
        intent = parse_nl_query("minimum quality score?")
        assert intent.metric == "quality_score"
        assert intent.aggregation == "min"

    def test_case_insensitive(self) -> None:
        intent = parse_nl_query("TOTAL COST LAST WEEK")
        assert intent.metric == "cost"
        assert intent.aggregation == "sum"
        assert intent.time_range == "last week"

    def test_complex_query(self) -> None:
        intent = parse_nl_query("what is the average cost per agent for failed tasks last month?")
        assert intent.metric == "cost"
        assert intent.aggregation == "avg"
        assert intent.group_by == "agent"
        assert intent.time_range == "last month"
        assert intent.filter_by.get("status") == "failed"


# ---------------------------------------------------------------------------
# execute_query
# ---------------------------------------------------------------------------


class TestExecuteQuery:
    def test_count_all(self, tmp_path: Path) -> None:
        archive = _write_tasks_jsonl(tmp_path, SAMPLE_TASKS)
        intent = QueryIntent(metric="tasks", aggregation="count")
        result = execute_query(intent, archive)
        assert result.value == 4.0

    def test_sum_cost(self, tmp_path: Path) -> None:
        archive = _write_tasks_jsonl(tmp_path, SAMPLE_TASKS)
        intent = QueryIntent(metric="cost", aggregation="sum")
        result = execute_query(intent, archive)
        assert result.value is not None
        assert abs(result.value - 2.10) < 0.001

    def test_avg_duration(self, tmp_path: Path) -> None:
        archive = _write_tasks_jsonl(tmp_path, SAMPLE_TASKS)
        intent = QueryIntent(metric="duration", aggregation="avg")
        result = execute_query(intent, archive)
        assert result.value is not None
        expected = (120.0 + 60.0 + 300.0 + 30.0) / 4
        assert abs(result.value - expected) < 0.001

    def test_max_cost(self, tmp_path: Path) -> None:
        archive = _write_tasks_jsonl(tmp_path, SAMPLE_TASKS)
        intent = QueryIntent(metric="cost", aggregation="max")
        result = execute_query(intent, archive)
        assert result.value == 1.20

    def test_min_quality(self, tmp_path: Path) -> None:
        archive = _write_tasks_jsonl(tmp_path, SAMPLE_TASKS)
        intent = QueryIntent(metric="quality_score", aggregation="min")
        result = execute_query(intent, archive)
        assert result.value == 0.40

    def test_filter_by_status(self, tmp_path: Path) -> None:
        archive = _write_tasks_jsonl(tmp_path, SAMPLE_TASKS)
        intent = QueryIntent(
            metric="tasks",
            aggregation="count",
            filter_by={"status": "completed"},
        )
        result = execute_query(intent, archive)
        assert result.value == 3.0

    def test_group_by_agent(self, tmp_path: Path) -> None:
        archive = _write_tasks_jsonl(tmp_path, SAMPLE_TASKS)
        intent = QueryIntent(
            metric="cost",
            aggregation="sum",
            group_by="agent",
        )
        result = execute_query(intent, archive)
        assert result.value is None
        assert len(result.rows) == 3  # claude, codex, gemini

        # Verify grouped values
        row_map = {str(r["group"]): r for r in result.rows}
        claude_val = float(row_map["claude"]["value"])  # type: ignore[arg-type]
        assert abs(claude_val - 1.70) < 0.001

    def test_empty_archive(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        archive.write_text("")
        intent = QueryIntent(metric="cost", aggregation="sum")
        result = execute_query(intent, archive)
        assert result.value == 0.0
        assert "No matching" in result.summary

    def test_missing_archive(self, tmp_path: Path) -> None:
        archive = tmp_path / "nonexistent.jsonl"
        intent = QueryIntent(metric="cost", aggregation="sum")
        result = execute_query(intent, archive)
        assert result.value == 0.0

    def test_malformed_jsonl_skipped(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        archive.write_text('{"id": "t1", "cost": 5.0}\nnot-valid-json\n{"id": "t2", "cost": 3.0}\n')
        intent = QueryIntent(metric="cost", aggregation="sum")
        result = execute_query(intent, archive)
        assert result.value is not None
        assert abs(result.value - 8.0) < 0.001

    def test_no_numeric_data(self, tmp_path: Path) -> None:
        archive = _write_tasks_jsonl(
            tmp_path,
            [{"id": "t1", "status": "completed"}],
        )
        intent = QueryIntent(metric="cost", aggregation="sum")
        result = execute_query(intent, archive)
        assert result.value == 0.0
        assert "No numeric" in result.summary


# ---------------------------------------------------------------------------
# format_answer
# ---------------------------------------------------------------------------


class TestFormatAnswer:
    def test_scalar_result(self) -> None:
        intent = QueryIntent(metric="cost", aggregation="sum")
        result = QueryResult(query="q", intent=intent, value=42.0)
        answer = format_answer(result)
        assert "Total" in answer
        assert "cost" in answer
        assert "42" in answer

    def test_scalar_with_time_range(self) -> None:
        intent = QueryIntent(metric="cost", aggregation="avg", time_range="last week")
        result = QueryResult(query="q", intent=intent, value=3.14)
        answer = format_answer(result)
        assert "Average" in answer
        assert "last week" in answer

    def test_scalar_with_filter(self) -> None:
        intent = QueryIntent(
            metric="tasks",
            aggregation="count",
            filter_by={"status": "failed"},
        )
        result = QueryResult(query="q", intent=intent, value=5.0)
        answer = format_answer(result)
        assert "Count" in answer
        assert "status=failed" in answer

    def test_grouped_result(self) -> None:
        intent = QueryIntent(
            metric="cost",
            aggregation="sum",
            group_by="agent",
        )
        rows: tuple[dict[str, object], ...] = (
            {"group": "claude", "value": 1.5, "count": 2},
            {"group": "codex", "value": 0.3, "count": 1},
        )
        result = QueryResult(query="q", intent=intent, value=None, rows=rows)
        answer = format_answer(result)
        assert "claude" in answer
        assert "codex" in answer

    def test_no_results(self) -> None:
        intent = QueryIntent(metric="cost", aggregation="sum")
        result = QueryResult(
            query="q",
            intent=intent,
            value=None,
            summary="No results.",
        )
        answer = format_answer(result)
        assert "No results" in answer


# ---------------------------------------------------------------------------
# get_available_metrics
# ---------------------------------------------------------------------------


class TestGetAvailableMetrics:
    def test_returns_list(self) -> None:
        metrics = get_available_metrics()
        assert isinstance(metrics, list)

    def test_expected_metrics(self) -> None:
        metrics = get_available_metrics()
        assert "cost" in metrics
        assert "duration" in metrics
        assert "tasks" in metrics
        assert "agents" in metrics
        assert "quality_score" in metrics

    def test_count(self) -> None:
        metrics = get_available_metrics()
        assert len(metrics) == 5


# ---------------------------------------------------------------------------
# render_analytics_table
# ---------------------------------------------------------------------------


class TestRenderAnalyticsTable:
    def test_scalar_table(self) -> None:
        intent = QueryIntent(metric="cost", aggregation="sum")
        result = QueryResult(query="q", intent=intent, value=42.0)
        table = render_analytics_table(result)
        assert "| Metric | Value |" in table
        assert "sum cost" in table
        assert "42" in table

    def test_grouped_table(self) -> None:
        intent = QueryIntent(
            metric="cost",
            aggregation="sum",
            group_by="agent",
        )
        rows: tuple[dict[str, object], ...] = (
            {"group": "claude", "value": 1.5, "count": 2},
            {"group": "codex", "value": 0.3, "count": 1},
        )
        result = QueryResult(query="q", intent=intent, value=None, rows=rows)
        table = render_analytics_table(result)
        assert "agent" in table
        assert "sum(cost)" in table
        assert "claude" in table
        assert "codex" in table
        assert "Count" in table

    def test_no_data(self) -> None:
        intent = QueryIntent(metric="cost", aggregation="sum")
        result = QueryResult(query="q", intent=intent)
        table = render_analytics_table(result)
        assert "No data" in table

    def test_integer_formatting(self) -> None:
        intent = QueryIntent(metric="tasks", aggregation="count")
        result = QueryResult(query="q", intent=intent, value=10.0)
        table = render_analytics_table(result)
        assert "10" in table
        # Should not show decimal for whole numbers
        assert "10.0000" not in table

    def test_decimal_formatting(self) -> None:
        intent = QueryIntent(metric="cost", aggregation="avg")
        result = QueryResult(query="q", intent=intent, value=3.1415)
        table = render_analytics_table(result)
        assert "3.1415" in table
