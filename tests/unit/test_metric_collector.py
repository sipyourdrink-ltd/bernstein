"""Focused tests for metric_collector.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.metric_collector import MetricsCollector, ProviderStatus


def test_complete_task_writes_metrics_and_updates_provider_health(tmp_path: Path) -> None:
    """complete_task writes task metrics, cost points, and healthy provider state for successful work."""
    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
    collector.start_task("T-1", role="backend", model="sonnet", provider="openai")

    with patch("bernstein.core.observability.metric_collector.time.time", return_value=10.0):
        metrics = collector.complete_task("T-1", success=True, tokens_used=123, cost_usd=1.25, janitor_passed=True)

    assert metrics is not None
    assert metrics.tokens_used == 123
    assert collector.get_provider_health("openai").status == ProviderStatus.HEALTHY
    files = list((tmp_path / "metrics").glob("*.jsonl"))
    assert files


def test_record_error_degrades_and_then_unhealthies_provider(tmp_path: Path) -> None:
    """record_error transitions provider health from degraded to unhealthy after repeated failures."""
    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")

    collector.record_error("timeout", "anthropic")
    assert collector.get_provider_health("anthropic").status == ProviderStatus.DEGRADED
    collector.record_error("timeout", "anthropic")
    collector.record_error("timeout", "anthropic")

    assert collector.get_provider_health("anthropic").status == ProviderStatus.UNHEALTHY


def test_is_quota_available_respects_reset_time(tmp_path: Path) -> None:
    """is_quota_available returns true again once an exhausted quota has reset."""
    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
    collector.set_usage_quota("google", "gemini", "tokens_per_month", limit=100, used=100, reset_time=50.0)

    with patch("bernstein.core.observability.metric_collector.time.time", return_value=40.0):
        assert collector.is_quota_available("google", "gemini") is False
    with patch("bernstein.core.observability.metric_collector.time.time", return_value=60.0):
        assert collector.is_quota_available("google", "gemini") is True


def test_get_metrics_summary_aggregates_tasks_agents_and_provider_stats(tmp_path: Path) -> None:
    """get_metrics_summary reports aggregate counts, costs, and provider-level status."""
    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
    collector.start_agent("A-1", role="backend", model="sonnet", provider="openai")
    collector.complete_agent_task("A-1", success=True, tokens_used=80, cost_usd=2.0)
    collector.end_agent("A-1")
    collector.start_task("T-1", role="backend", model="sonnet", provider="openai")
    collector.complete_task("T-1", success=True, tokens_used=80, cost_usd=2.0, janitor_passed=True)

    summary = collector.get_metrics_summary()

    assert summary["total_tasks"] == 1
    assert summary["successful_tasks"] == 1
    assert summary["total_agents"] == 1
    assert summary["provider_stats"]["openai"]["total_cost_usd"] == pytest.approx(2.0)


def test_get_quality_metrics_groups_completed_tasks_by_model(tmp_path: Path) -> None:
    """get_quality_metrics computes per-model and overall completion aggregates."""
    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
    first = collector.start_task("T-1", role="backend", model="sonnet", provider="openai")
    second = collector.start_task("T-2", role="backend", model="opus", provider="openai")
    first.start_time = 10.0
    second.start_time = 20.0
    first.end_time = 13.0
    second.end_time = 28.0
    first.success = True
    second.success = False
    first.janitor_passed = True
    second.janitor_passed = False
    first.tokens_used = 100
    second.tokens_used = 200

    metrics = collector.get_quality_metrics()

    assert metrics["overall"]["total_tasks"] == 2
    assert metrics["per_model"]["sonnet"]["success_rate"] == pytest.approx(1.0)
    assert metrics["per_model"]["opus"]["success_rate"] == pytest.approx(0.0)
    assert metrics["review_rejection_rate"] == pytest.approx(0.5)
