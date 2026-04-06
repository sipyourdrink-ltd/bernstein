"""Tests for MCP-011: MCP server metrics collection."""

from __future__ import annotations

import pytest

from bernstein.core.mcp_metrics import (
    AvailabilityRecord,
    MCPMetricsCollector,
    MCPServerMetrics,
    ToolCallRecord,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def collector() -> MCPMetricsCollector:
    return MCPMetricsCollector(max_latency_samples=10, max_availability_records=5)


# ---------------------------------------------------------------------------
# Tests — MCPServerMetrics
# ---------------------------------------------------------------------------


class TestMCPServerMetrics:
    def test_error_rate_no_calls(self) -> None:
        m = MCPServerMetrics(server_name="test")
        assert m.error_rate == 0.0

    def test_error_rate_with_errors(self) -> None:
        m = MCPServerMetrics(server_name="test", total_calls=10, error_count=3)
        assert abs(m.error_rate - 0.3) < 0.01

    def test_availability_rate_all_alive(self) -> None:
        m = MCPServerMetrics(
            server_name="test",
            availability_checks=[AvailabilityRecord(alive=True) for _ in range(5)],
        )
        assert m.availability_rate == 1.0

    def test_availability_rate_mixed(self) -> None:
        m = MCPServerMetrics(
            server_name="test",
            availability_checks=[
                AvailabilityRecord(alive=True),
                AvailabilityRecord(alive=False),
                AvailabilityRecord(alive=True),
                AvailabilityRecord(alive=False),
            ],
        )
        assert m.availability_rate == 0.5

    def test_availability_rate_no_checks(self) -> None:
        m = MCPServerMetrics(server_name="test")
        assert m.availability_rate == 1.0

    def test_latency_percentile_empty(self) -> None:
        m = MCPServerMetrics(server_name="test")
        assert m.latency_percentile(0.95) == 0.0

    def test_latency_percentile(self) -> None:
        m = MCPServerMetrics(
            server_name="test",
            latency_samples=[10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0],
        )
        p50 = m.latency_percentile(0.50)
        p95 = m.latency_percentile(0.95)
        assert p50 <= p95

    def test_to_dict(self) -> None:
        m = MCPServerMetrics(
            server_name="github",
            total_calls=100,
            error_count=5,
            latency_samples=[10.0, 20.0, 30.0],
        )
        d = m.to_dict()
        assert d["server_name"] == "github"
        assert d["total_calls"] == 100
        assert d["error_rate"] == 0.05
        assert "latency_p50" in d
        assert "latency_p95" in d
        assert "latency_p99" in d


# ---------------------------------------------------------------------------
# Tests — MCPMetricsCollector recording
# ---------------------------------------------------------------------------


class TestCollectorRecording:
    def test_record_call(self, collector: MCPMetricsCollector) -> None:
        collector.record_call("github", "create_issue", 142.3)
        m = collector.get_metrics("github")
        assert m is not None
        assert m.total_calls == 1
        assert m.error_count == 0
        assert len(m.latency_samples) == 1

    def test_record_error_call(self, collector: MCPMetricsCollector) -> None:
        collector.record_call("github", "create_issue", 500.0, error=True)
        m = collector.get_metrics("github")
        assert m is not None
        assert m.error_count == 1

    def test_record_multiple_calls(self, collector: MCPMetricsCollector) -> None:
        for i in range(5):
            collector.record_call("github", "list_repos", float(i * 10))
        m = collector.get_metrics("github")
        assert m is not None
        assert m.total_calls == 5
        assert len(m.latency_samples) == 5

    def test_latency_trimming(self, collector: MCPMetricsCollector) -> None:
        # max_latency_samples=10
        for i in range(15):
            collector.record_call("github", "tool", float(i))
        m = collector.get_metrics("github")
        assert m is not None
        assert len(m.latency_samples) <= 10

    def test_record_availability(self, collector: MCPMetricsCollector) -> None:
        collector.record_availability("github", alive=True)
        collector.record_availability("github", alive=False)
        m = collector.get_metrics("github")
        assert m is not None
        assert len(m.availability_checks) == 2

    def test_availability_trimming(self, collector: MCPMetricsCollector) -> None:
        # max_availability_records=5
        for i in range(10):
            collector.record_availability("github", alive=(i % 2 == 0))
        m = collector.get_metrics("github")
        assert m is not None
        assert len(m.availability_checks) <= 5


# ---------------------------------------------------------------------------
# Tests — MCPMetricsCollector queries
# ---------------------------------------------------------------------------


class TestCollectorQueries:
    def test_get_unknown_returns_none(self, collector: MCPMetricsCollector) -> None:
        assert collector.get_metrics("nonexistent") is None

    def test_summary(self, collector: MCPMetricsCollector) -> None:
        collector.record_call("github", "tool", 100.0)
        s = collector.summary("github")
        assert s is not None
        assert s["total_calls"] == 1

    def test_summary_unknown(self, collector: MCPMetricsCollector) -> None:
        assert collector.summary("nonexistent") is None

    def test_all_summaries(self, collector: MCPMetricsCollector) -> None:
        collector.record_call("github", "t1", 100.0)
        collector.record_call("database", "t2", 200.0)
        summaries = collector.all_summaries()
        assert "github" in summaries
        assert "database" in summaries

    def test_servers_above_error_rate(self, collector: MCPMetricsCollector) -> None:
        for _ in range(10):
            collector.record_call("good", "tool", 10.0)
        for _ in range(5):
            collector.record_call("bad", "tool", 10.0, error=True)
        for _ in range(5):
            collector.record_call("bad", "tool", 10.0)
        result = collector.servers_above_error_rate(0.40)
        assert "bad" in result
        assert "good" not in result

    def test_servers_below_availability(self, collector: MCPMetricsCollector) -> None:
        for _ in range(5):
            collector.record_availability("healthy", alive=True)
        collector.record_availability("unhealthy", alive=False)
        collector.record_availability("unhealthy", alive=False)
        result = collector.servers_below_availability(0.5)
        assert "unhealthy" in result
        assert "healthy" not in result


# ---------------------------------------------------------------------------
# Tests — Reset
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_single(self, collector: MCPMetricsCollector) -> None:
        collector.record_call("github", "t", 10.0)
        collector.reset("github")
        assert collector.get_metrics("github") is None

    def test_reset_all(self, collector: MCPMetricsCollector) -> None:
        collector.record_call("github", "t", 10.0)
        collector.record_call("database", "t", 20.0)
        collector.reset()
        assert collector.all_summaries() == {}


# ---------------------------------------------------------------------------
# Tests — Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_to_dict(self, collector: MCPMetricsCollector) -> None:
        collector.record_call("github", "t", 10.0)
        d = collector.to_dict()
        assert d["total_servers"] == 1
        assert "github" in d["servers"]
