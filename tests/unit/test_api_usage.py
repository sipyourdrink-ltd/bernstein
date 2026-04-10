"""Unit tests for API usage aggregation and persistence."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import bernstein.core.api_usage as api_usage
from bernstein.core.api_usage import ApiUsageTracker


def test_record_call_updates_session_summary_and_persists(tmp_path: Path) -> None:
    tracker = ApiUsageTracker(tmp_path)

    tracker.record_call(
        provider="openai",
        model="gpt-5.4-mini",
        input_tokens=120,
        output_tokens=30,
        cost_usd=0.42,
        task_id="task-1",
        agent_id="agent-1",
    )

    summary = tracker.session_summary("agent-1")
    assert summary["calls"] == 1
    assert summary["total_tokens"] == 150
    assert summary["total_cost_usd"] == pytest.approx(0.42)

    calls_path = tmp_path / "api_calls.jsonl"
    summary_path = tmp_path / "summary.json"
    sessions_path = tmp_path / "sessions.json"
    assert calls_path.exists()
    assert summary_path.exists()
    assert sessions_path.exists()
    persisted = json.loads(summary_path.read_text(encoding="utf-8"))
    assert persisted["providers"]["openai"]["calls"] == 1


def test_record_tier_usage_and_budget_checks(tmp_path: Path) -> None:
    tracker = ApiUsageTracker(tmp_path)
    tracker.record_call("anthropic", "sonnet", 100, 50, 2.0, agent_id="agent-2")
    tracker.record_tier_usage("fast", "task-1", 0.5)
    tracker.record_tier_usage("fast", "task-2", 1.5)

    tier = tracker.tier_summary()["fast"]
    assert tier.task_count == 2
    assert tier.total_cost_usd == pytest.approx(2.0)
    assert tier.avg_cost_per_task == pytest.approx(1.0)
    assert tracker.monthly_budget_remaining(10.0) == pytest.approx(8.0)
    assert tracker.is_over_budget(1.0) is True


def test_get_usage_tracker_returns_singleton(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(api_usage, "_default_usage_tracker", None)

    first = api_usage.get_usage_tracker(tmp_path)
    second = api_usage.get_usage_tracker(tmp_path / "other")

    assert first is second


def test_provider_summary_aggregates_multiple_providers(tmp_path: Path) -> None:
    tracker = ApiUsageTracker(tmp_path)
    tracker.record_call("openai", "gpt-5.4-mini", 10, 5, 0.1)
    tracker.record_call("openai", "gpt-5.4-mini", 20, 10, 0.2)
    tracker.record_call("anthropic", "sonnet", 30, 15, 0.3)

    summary = tracker.provider_summary()

    assert summary["openai"].calls == 2
    assert summary["openai"].total_input_tokens == 30
    assert math.isclose(summary["openai"].total_cost_usd, 0.3)
    assert summary["anthropic"].calls == 1


def test_session_summary_returns_empty_for_unknown_agent(tmp_path: Path) -> None:
    tracker = ApiUsageTracker(tmp_path)

    assert tracker.session_summary("missing-agent") == {}
