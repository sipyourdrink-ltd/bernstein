"""Focused tests for tick pipeline helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.tick_pipeline import (
    complete_task,
    compute_total_spent,
    fail_task,
    fetch_all_tasks,
    parse_backlog_file,
    total_spent_cache,
)


def test_parse_backlog_file_uses_safe_defaults_when_parser_returns_none() -> None:
    """parse_backlog_file falls back to filename-derived defaults when markdown parsing fails."""
    with patch("bernstein.core.orchestration.tick_pipeline.parse_backlog_text", return_value=None):
        payload = parse_backlog_file("123-fix-auth.md", "Body text")

    assert payload["title"] == "123 fix auth"
    assert payload["role"] == "backend"
    assert payload["priority"] == 2


def test_fetch_all_tasks_buckets_tasks_by_status(make_task: MagicMock) -> None:
    """fetch_all_tasks returns requested buckets populated from a single GET response."""
    raw_tasks = [
        make_task(id="T-open").__dict__ | {"status": "open", "scope": "medium", "complexity": "medium"},
        make_task(id="T-done").__dict__ | {"status": "done", "scope": "medium", "complexity": "medium"},
    ]
    client = MagicMock()
    client.get.return_value.json.return_value = raw_tasks
    client.get.return_value.raise_for_status.return_value = None

    by_status = fetch_all_tasks(client, "http://server")

    assert [task.id for task in by_status["open"]] == ["T-open"]
    assert [task.id for task in by_status["done"]] == ["T-done"]
    assert by_status["failed"] == []


def test_fail_and_complete_task_post_expected_payloads() -> None:
    """fail_task and complete_task send the expected JSON payloads to the server."""
    client = MagicMock()
    client.post.return_value.raise_for_status.return_value = None

    fail_task(client, "http://server", "T-1", "boom")
    complete_task(client, "http://server", "T-1", "done")

    assert client.post.call_args_list[0].args[0] == "http://server/tasks/T-1/fail"
    assert client.post.call_args_list[0].kwargs["json"] == {"reason": "boom"}
    assert client.post.call_args_list[1].args[0] == "http://server/tasks/T-1/complete"
    assert client.post.call_args_list[1].kwargs["json"] == {"result_summary": "done"}


def test_compute_total_spent_uses_cache_and_updates_when_file_changes(tmp_path: Path) -> None:
    """compute_total_spent reuses cached file totals until a metrics file changes on disk."""
    total_spent_cache.clear()
    metrics_dir = tmp_path / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True)
    cost_file = metrics_dir / "cost_efficiency_2026-03-31.jsonl"
    cost_file.write_text(
        json.dumps({"value": 1.5, "labels": {"task_id": "T-1"}})
        + "\n"
        + json.dumps({"value": 7.0, "labels": {"agent_id": "A-1"}})
        + "\n",
        encoding="utf-8",
    )

    first = compute_total_spent(tmp_path)
    with patch("bernstein.core.orchestration.tick_pipeline._parse_file_total") as mock_parse:
        second = compute_total_spent(tmp_path)
    cost_file.write_text(json.dumps({"value": 2.0, "labels": {"task_id": "T-2"}}) + "\n", encoding="utf-8")
    third = compute_total_spent(tmp_path)

    assert first == pytest.approx(1.5)
    assert second == pytest.approx(1.5)
    mock_parse.assert_not_called()
    assert third == pytest.approx(2.0)


def test_compute_total_spent_removes_deleted_file_contribution(tmp_path: Path) -> None:
    """compute_total_spent subtracts cached contribution when a cost file is removed."""
    total_spent_cache.clear()
    metrics_dir = tmp_path / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True)
    cost_file = metrics_dir / "cost_efficiency_2026-03-31.jsonl"
    cost_file.write_text(json.dumps({"value": 3.0, "labels": {"task_id": "T-1"}}) + "\n", encoding="utf-8")

    assert compute_total_spent(tmp_path) == pytest.approx(3.0)
    cost_file.unlink()

    assert compute_total_spent(tmp_path) == pytest.approx(0.0)
