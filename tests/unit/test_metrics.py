"""Dedicated tests for MetricsCollector in src/bernstein/core/metrics.py."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from bernstein.core.metrics import (
    AgentMetrics,
    MetricsCollector,
    MetricType,
    ProviderHealth,
    ProviderStatus,
    TaskMetrics,
    get_collector,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def collector(tmp_path: Path) -> MetricsCollector:
    return MetricsCollector(metrics_dir=tmp_path / "metrics")


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInit:
    def test_creates_metrics_dir(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "new" / "nested" / "metrics"
        assert not metrics_dir.exists()
        MetricsCollector(metrics_dir=metrics_dir)
        assert metrics_dir.exists()

    def test_default_metrics_dir_uses_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        c = MetricsCollector()
        assert c._metrics_dir == tmp_path / ".sdd" / "metrics"

    def test_empty_in_memory_state(self, collector: MetricsCollector) -> None:
        assert collector._task_metrics == {}
        assert collector._agent_metrics == {}
        assert collector._provider_health == {}
        assert collector._usage_quotas == {}

    def test_custom_metrics_dir(self, tmp_path: Path) -> None:
        d = tmp_path / "custom"
        c = MetricsCollector(metrics_dir=d)
        assert c._metrics_dir == d


# ---------------------------------------------------------------------------
# Task Metrics
# ---------------------------------------------------------------------------


class TestStartTask:
    def test_returns_task_metrics(self, collector: MetricsCollector) -> None:
        m = collector.start_task("t1", "backend", "sonnet", "claude")
        assert isinstance(m, TaskMetrics)
        assert m.task_id == "t1"
        assert m.role == "backend"
        assert m.model == "sonnet"
        assert m.provider == "claude"

    def test_stored_in_dict(self, collector: MetricsCollector) -> None:
        collector.start_task("t1", "backend", "sonnet", "claude")
        assert "t1" in collector._task_metrics

    def test_start_time_set(self, collector: MetricsCollector) -> None:
        before = time.time()
        m = collector.start_task("t1", "backend", "sonnet", "claude")
        after = time.time()
        assert before <= m.start_time <= after

    def test_defaults(self, collector: MetricsCollector) -> None:
        m = collector.start_task("t1", "backend", "sonnet", "claude")
        assert m.end_time is None
        assert m.success is False
        assert m.tokens_used == 0
        assert m.cost_usd == 0.0
        assert m.retry_count == 0


class TestCompleteTask:
    def test_returns_none_for_unknown_task(self, collector: MetricsCollector) -> None:
        assert collector.complete_task("ghost", success=True) is None

    def test_sets_end_time(self, collector: MetricsCollector) -> None:
        collector.start_task("t1", "backend", "sonnet", "claude")
        before = time.time()
        m = collector.complete_task("t1", success=True)
        after = time.time()
        assert m is not None
        assert before <= m.end_time <= after  # type: ignore[operator]

    def test_sets_success_and_tokens(self, collector: MetricsCollector) -> None:
        collector.start_task("t1", "backend", "sonnet", "claude")
        m = collector.complete_task("t1", success=True, tokens_used=500, cost_usd=0.01)
        assert m is not None
        assert m.success is True
        assert m.tokens_used == 500
        assert m.cost_usd == 0.01

    def test_records_failure(self, collector: MetricsCollector) -> None:
        collector.start_task("t1", "backend", "sonnet", "claude")
        m = collector.complete_task("t1", success=False, error="timeout")
        assert m is not None
        assert m.success is False
        assert m.error == "timeout"

    def test_writes_jsonl_file(self, collector: MetricsCollector, tmp_path: Path) -> None:
        collector.start_task("t1", "backend", "sonnet", "claude")
        collector.complete_task("t1", success=True)
        today = datetime.now().strftime("%Y-%m-%d")
        jsonl = collector._metrics_dir / f"task_completion_time_{today}.jsonl"
        assert jsonl.exists()
        lines = jsonl.read_text().strip().splitlines()
        assert len(lines) >= 1
        data = json.loads(lines[0])
        assert data["metric_type"] == MetricType.TASK_COMPLETION_TIME.value
        assert "value" in data
        assert data["labels"]["task_id"] == "t1"

    def test_also_writes_cost_efficiency_file(self, collector: MetricsCollector) -> None:
        collector.start_task("t1", "backend", "sonnet", "claude")
        collector.complete_task("t1", success=True, cost_usd=0.05)
        today = datetime.now().strftime("%Y-%m-%d")
        cost_file = collector._metrics_dir / f"cost_efficiency_{today}.jsonl"
        assert cost_file.exists()

    def test_janitor_fields(self, collector: MetricsCollector) -> None:
        collector.start_task("t1", "backend", "sonnet", "claude")
        m = collector.complete_task(
            "t1",
            success=True,
            janitor_passed=True,
            files_modified=3,
            lines_added=20,
            lines_deleted=5,
        )
        assert m is not None
        assert m.janitor_passed is True
        assert m.files_modified == 3
        assert m.lines_added == 20
        assert m.lines_deleted == 5


class TestIncrementTaskRetry:
    def test_increments_retry(self, collector: MetricsCollector) -> None:
        collector.start_task("t1", "backend", "sonnet", "claude")
        collector.increment_task_retry("t1")
        collector.increment_task_retry("t1")
        assert collector._task_metrics["t1"].retry_count == 2

    def test_noop_for_unknown_task(self, collector: MetricsCollector) -> None:
        collector.increment_task_retry("ghost")  # Should not raise


# ---------------------------------------------------------------------------
# Agent Metrics
# ---------------------------------------------------------------------------


class TestAgentMetrics:
    def test_start_agent(self, collector: MetricsCollector) -> None:
        m = collector.start_agent("a1", "backend", "sonnet", "claude")
        assert isinstance(m, AgentMetrics)
        assert m.agent_id == "a1"
        assert "a1" in collector._agent_metrics

    def test_complete_agent_task_success(self, collector: MetricsCollector) -> None:
        collector.start_agent("a1", "backend", "sonnet", "claude")
        collector.complete_agent_task("a1", success=True, tokens_used=100, cost_usd=0.01)
        m = collector._agent_metrics["a1"]
        assert m.tasks_completed == 1
        assert m.tasks_failed == 0
        assert m.total_tokens == 100
        assert m.total_cost_usd == pytest.approx(0.01)

    def test_complete_agent_task_failure(self, collector: MetricsCollector) -> None:
        collector.start_agent("a1", "backend", "sonnet", "claude")
        collector.complete_agent_task("a1", success=False)
        m = collector._agent_metrics["a1"]
        assert m.tasks_failed == 1
        assert m.tasks_completed == 0

    def test_complete_agent_task_unknown_noop(self, collector: MetricsCollector) -> None:
        collector.complete_agent_task("ghost", success=True)  # should not raise

    def test_end_agent_sets_end_time(self, collector: MetricsCollector) -> None:
        collector.start_agent("a1", "backend", "sonnet", "claude")
        collector.complete_agent_task("a1", success=True)
        before = time.time()
        m = collector.end_agent("a1")
        after = time.time()
        assert m is not None
        assert before <= m.end_time <= after  # type: ignore[operator]

    def test_end_agent_writes_success_rate(self, collector: MetricsCollector) -> None:
        collector.start_agent("a1", "backend", "sonnet", "claude")
        collector.complete_agent_task("a1", success=True)
        collector.end_agent("a1")
        today = datetime.now().strftime("%Y-%m-%d")
        f = collector._metrics_dir / f"agent_success_{today}.jsonl"
        assert f.exists()
        data = json.loads(f.read_text().strip().splitlines()[0])
        assert data["value"] == pytest.approx(1.0)

    def test_end_agent_unknown_returns_none(self, collector: MetricsCollector) -> None:
        assert collector.end_agent("ghost") is None

    def test_end_agent_no_tasks_skips_write(self, collector: MetricsCollector) -> None:
        collector.start_agent("a1", "backend", "sonnet", "claude")
        collector.end_agent("a1")
        today = datetime.now().strftime("%Y-%m-%d")
        f = collector._metrics_dir / f"agent_success_{today}.jsonl"
        assert not f.exists()


# ---------------------------------------------------------------------------
# Provider Health
# ---------------------------------------------------------------------------


class TestProviderHealth:
    def test_get_creates_default_healthy(self, collector: MetricsCollector) -> None:
        h = collector.get_provider_health("claude")
        assert isinstance(h, ProviderHealth)
        assert h.status == ProviderStatus.HEALTHY

    def test_get_returns_same_object(self, collector: MetricsCollector) -> None:
        h1 = collector.get_provider_health("claude")
        h2 = collector.get_provider_health("claude")
        assert h1 is h2

    def test_consecutive_failures_degrade_then_unhealthy(self, collector: MetricsCollector) -> None:
        collector._update_provider_health("claude", False)
        assert collector.get_provider_health("claude").status == ProviderStatus.DEGRADED
        collector._update_provider_health("claude", False)
        collector._update_provider_health("claude", False)
        assert collector.get_provider_health("claude").status == ProviderStatus.UNHEALTHY

    def test_consecutive_successes_restore_healthy(self, collector: MetricsCollector) -> None:
        h = collector.get_provider_health("claude")
        h.status = ProviderStatus.UNHEALTHY
        h.consecutive_failures = 5
        for _ in range(3):
            collector._update_provider_health("claude", True)
        assert h.status == ProviderStatus.HEALTHY

    def test_mark_rate_limited(self, collector: MetricsCollector) -> None:
        collector.mark_provider_rate_limited("claude", remaining=10, reset_time=9999.0)
        h = collector.get_provider_health("claude")
        assert h.status == ProviderStatus.RATE_LIMITED
        assert h.rate_limit_remaining == 10
        assert h.rate_limit_reset == 9999.0

    def test_mark_healthy(self, collector: MetricsCollector) -> None:
        h = collector.get_provider_health("claude")
        h.status = ProviderStatus.UNHEALTHY
        h.consecutive_failures = 5
        collector.mark_provider_healthy("claude")
        assert h.status == ProviderStatus.HEALTHY
        assert h.consecutive_failures == 0


# ---------------------------------------------------------------------------
# Usage Quotas
# ---------------------------------------------------------------------------


class TestUsageQuotas:
    def test_set_and_get_quota(self, collector: MetricsCollector) -> None:
        collector.set_usage_quota("claude", "sonnet", "requests_per_day", 100, used=20)
        quotas = collector.get_quota_status("claude", "sonnet")
        assert "requests_per_day" in quotas
        q = quotas["requests_per_day"]
        assert q.used == 20
        assert q.limit == 100
        assert q.percentage_used == pytest.approx(20.0)

    def test_quota_key_isolation(self, collector: MetricsCollector) -> None:
        collector.set_usage_quota("claude", "sonnet", "requests_per_day", 100)
        collector.set_usage_quota("gemini", "flash", "requests_per_day", 200)
        claude_q = collector.get_quota_status("claude", "sonnet")
        gemini_q = collector.get_quota_status("gemini", "flash")
        assert "requests_per_day" in claude_q
        assert "requests_per_day" in gemini_q
        assert claude_q["requests_per_day"].limit == 100
        assert gemini_q["requests_per_day"].limit == 200

    def test_is_quota_available_true(self, collector: MetricsCollector) -> None:
        collector.set_usage_quota("claude", "sonnet", "tokens_per_month", 1000, used=500)
        assert collector.is_quota_available("claude", "sonnet") is True

    def test_is_quota_available_false_when_exhausted(self, collector: MetricsCollector) -> None:
        collector.set_usage_quota("claude", "sonnet", "tokens_per_month", 100, used=100)
        assert collector.is_quota_available("claude", "sonnet") is False

    def test_is_quota_available_no_quotas(self, collector: MetricsCollector) -> None:
        assert collector.is_quota_available("claude", "sonnet") is True

    def test_update_usage_quota_via_complete_task(self, collector: MetricsCollector) -> None:
        collector.set_usage_quota("claude", "sonnet", "tokens_per_month", 10000, used=0)
        collector.start_task("t1", "backend", "sonnet", "claude")
        collector.complete_task("t1", success=True, tokens_used=200)
        key = "claude:sonnet:tokens_per_month"
        assert collector._usage_quotas[key].used == 200


# ---------------------------------------------------------------------------
# Quality / Janitor / Error / Free-tier
# ---------------------------------------------------------------------------


class TestQualityAndErrors:
    def test_record_janitor_result_writes_metric(self, collector: MetricsCollector) -> None:
        collector.record_janitor_result("t1", passed=True, role="backend", model="sonnet", provider="claude")
        today = datetime.now().strftime("%Y-%m-%d")
        f = collector._metrics_dir / f"agent_success_{today}.jsonl"
        assert f.exists()
        data = json.loads(f.read_text().strip().splitlines()[0])
        assert data["value"] == 1.0
        assert data["labels"]["verification"] == "janitor"

    def test_record_janitor_result_updates_task_metrics(self, collector: MetricsCollector) -> None:
        collector.start_task("t1", "backend", "sonnet", "claude")
        collector.record_janitor_result("t1", passed=True, role="backend", model="sonnet", provider="claude")
        assert collector._task_metrics["t1"].janitor_passed is True

    def test_record_error_writes_metric(self, collector: MetricsCollector) -> None:
        collector.record_error("timeout", "claude", model="sonnet", role="backend")
        today = datetime.now().strftime("%Y-%m-%d")
        f = collector._metrics_dir / f"error_rate_{today}.jsonl"
        assert f.exists()
        data = json.loads(f.read_text().strip().splitlines()[0])
        assert data["labels"]["error_type"] == "timeout"

    def test_record_error_updates_provider_health(self, collector: MetricsCollector) -> None:
        collector.record_error("timeout", "claude")
        h = collector.get_provider_health("claude")
        assert h.consecutive_failures >= 1

    def test_record_free_tier_usage(self, collector: MetricsCollector) -> None:
        collector.record_free_tier_usage(
            "claude",
            "sonnet",
            requests_used=50,
            requests_limit=100,
            tokens_used=1000,
            tokens_limit=10000,
        )
        today = datetime.now().strftime("%Y-%m-%d")
        f = collector._metrics_dir / f"free_tier_usage_{today}.jsonl"
        assert f.exists()
        lines = f.read_text().strip().splitlines()
        assert len(lines) == 2  # one for requests, one for tokens

    def test_record_free_tier_usage_skips_zero_limits(self, collector: MetricsCollector) -> None:
        collector.record_free_tier_usage("claude", "sonnet", requests_used=0, requests_limit=0)
        today = datetime.now().strftime("%Y-%m-%d")
        f = collector._metrics_dir / f"free_tier_usage_{today}.jsonl"
        assert not f.exists()


# ---------------------------------------------------------------------------
# JSONL File Writing
# ---------------------------------------------------------------------------


class TestJsonlFileWriting:
    def test_metric_point_structure(self, collector: MetricsCollector) -> None:
        collector._write_metric_point(MetricType.ERROR_RATE, 1.0, {"key": "val"})
        collector.flush()
        today = datetime.now().strftime("%Y-%m-%d")
        f = collector._metrics_dir / f"error_rate_{today}.jsonl"
        data = json.loads(f.read_text().strip())
        assert "timestamp" in data
        assert data["metric_type"] == "error_rate"
        assert data["value"] == 1.0
        assert data["labels"] == {"key": "val"}

    def test_multiple_points_appended(self, collector: MetricsCollector) -> None:
        for i in range(5):
            collector._write_metric_point(MetricType.ERROR_RATE, float(i), {})
        collector.flush()
        today = datetime.now().strftime("%Y-%m-%d")
        f = collector._metrics_dir / f"error_rate_{today}.jsonl"
        lines = f.read_text().strip().splitlines()
        assert len(lines) == 5

    def test_file_named_by_date(self, collector: MetricsCollector) -> None:
        collector._write_metric_point(MetricType.API_USAGE, 1.0, {})
        collector.flush()
        today = datetime.now().strftime("%Y-%m-%d")
        expected = collector._metrics_dir / f"api_usage_{today}.jsonl"
        assert expected.exists()


# ---------------------------------------------------------------------------
# Record API Call
# ---------------------------------------------------------------------------


class TestRecordApiCall:
    def test_writes_api_usage_metric(self, collector: MetricsCollector) -> None:
        collector.record_api_call("claude", "sonnet", latency_ms=200, tokens=300, cost_usd=0.01, success=True)
        today = datetime.now().strftime("%Y-%m-%d")
        f = collector._metrics_dir / f"api_usage_{today}.jsonl"
        assert f.exists()

    def test_updates_avg_latency(self, collector: MetricsCollector) -> None:
        collector.record_api_call("claude", "sonnet", latency_ms=100, tokens=0, cost_usd=0.0, success=True)
        h = collector.get_provider_health("claude")
        assert h.avg_latency_ms > 0

    def test_exponential_moving_average(self, collector: MetricsCollector) -> None:
        collector.record_api_call("claude", "sonnet", latency_ms=100, tokens=0, cost_usd=0.0, success=True)
        h = collector.get_provider_health("claude")
        # alpha=0.3: 0.3*100 + 0.7*0 = 30
        assert h.avg_latency_ms == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# Query Methods
# ---------------------------------------------------------------------------


class TestQueryMethods:
    def test_get_agent_success_rate_no_agents(self, collector: MetricsCollector) -> None:
        assert collector.get_agent_success_rate() == 1.0

    def test_get_agent_success_rate_mixed(self, collector: MetricsCollector) -> None:
        collector.start_agent("a1", "backend", "sonnet", "claude")
        collector.complete_agent_task("a1", success=True)
        collector.complete_agent_task("a1", success=True)
        collector.complete_agent_task("a1", success=False)
        rate = collector.get_agent_success_rate(agent_id="a1")
        assert rate == pytest.approx(2 / 3)

    def test_get_agent_success_rate_by_role(self, collector: MetricsCollector) -> None:
        collector.start_agent("a1", "backend", "sonnet", "claude")
        collector.complete_agent_task("a1", success=True)
        collector.start_agent("a2", "qa", "sonnet", "claude")
        collector.complete_agent_task("a2", success=False)
        rate = collector.get_agent_success_rate(role="qa")
        assert rate == pytest.approx(0.0)

    def test_get_avg_completion_time_no_tasks(self, collector: MetricsCollector) -> None:
        assert collector.get_avg_completion_time() == 0.0

    def test_get_avg_completion_time(self, collector: MetricsCollector) -> None:
        collector.start_task("t1", "backend", "sonnet", "claude")
        collector.complete_task("t1", success=True)
        collector.start_task("t2", "backend", "sonnet", "claude")
        collector.complete_task("t2", success=True)
        avg = collector.get_avg_completion_time()
        assert avg >= 0.0

    def test_get_avg_completion_time_by_role(self, collector: MetricsCollector) -> None:
        collector.start_task("t1", "backend", "sonnet", "claude")
        collector.complete_task("t1", success=True)
        collector.start_task("t2", "qa", "sonnet", "claude")
        collector.complete_task("t2", success=True)
        avg = collector.get_avg_completion_time(role="qa")
        assert avg >= 0.0

    def test_get_total_cost(self, collector: MetricsCollector) -> None:
        collector.start_agent("a1", "backend", "sonnet", "claude")
        collector.complete_agent_task("a1", success=True, cost_usd=0.05)
        collector.complete_agent_task("a1", success=True, cost_usd=0.10)
        assert collector.get_total_cost() == pytest.approx(0.15)

    def test_get_total_cost_by_agent(self, collector: MetricsCollector) -> None:
        collector.start_agent("a1", "backend", "sonnet", "claude")
        collector.complete_agent_task("a1", success=True, cost_usd=0.05)
        collector.start_agent("a2", "qa", "sonnet", "claude")
        collector.complete_agent_task("a2", success=True, cost_usd=0.20)
        assert collector.get_total_cost(agent_id="a1") == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Metrics Summary
# ---------------------------------------------------------------------------


class TestMetricsSummary:
    def test_empty_summary(self, collector: MetricsCollector) -> None:
        s = collector.get_metrics_summary()
        assert s["total_tasks"] == 0
        assert s["success_rate"] == 1.0

    def test_summary_counts(self, collector: MetricsCollector) -> None:
        collector.start_task("t1", "backend", "sonnet", "claude")
        collector.complete_task("t1", success=True)
        collector.start_task("t2", "backend", "sonnet", "claude")
        collector.complete_task("t2", success=False)
        s = collector.get_metrics_summary()
        assert s["total_tasks"] == 2
        assert s["successful_tasks"] == 1
        assert s["failed_tasks"] == 1
        assert s["success_rate"] == pytest.approx(0.5)

    def test_summary_includes_provider_stats(self, collector: MetricsCollector) -> None:
        collector.start_task("t1", "backend", "sonnet", "claude")
        collector.complete_task("t1", success=True)
        s = collector.get_metrics_summary()
        assert "claude" in s["provider_stats"]


# ---------------------------------------------------------------------------
# Export Metrics
# ---------------------------------------------------------------------------


# TestExportMetrics removed — export_metrics method was removed


# ---------------------------------------------------------------------------
# Global Collector
# ---------------------------------------------------------------------------


class TestGetCollector:
    def test_returns_metrics_collector(self, tmp_path: Path) -> None:
        import bernstein.core.metric_collector as mc_module

        mc_module._default_collector = None
        c = get_collector(tmp_path / "metrics")
        assert isinstance(c, MetricsCollector)

    def test_returns_same_instance(self, tmp_path: Path) -> None:
        import bernstein.core.metric_collector as mc_module

        mc_module._default_collector = None
        c1 = get_collector(tmp_path / "metrics")
        c2 = get_collector()
        assert c1 is c2

    def test_reset_allows_new_instance(self, tmp_path: Path) -> None:
        import bernstein.core.metric_collector as mc_module

        mc_module._default_collector = None
        c1 = get_collector(tmp_path / "m1")
        mc_module._default_collector = None
        c2 = get_collector(tmp_path / "m2")
        assert c1 is not c2
