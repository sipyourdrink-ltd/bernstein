"""Tests for per-adapter health monitoring (AGENT-009)."""

from __future__ import annotations

import pytest
from bernstein.core.adapter_health import (
    AdapterHealthConfig,
    AdapterHealthMonitor,
    AdapterStats,
)


class TestAdapterStats:
    def test_initial_state(self) -> None:
        stats = AdapterStats(adapter_name="claude")
        assert stats.total == 0
        assert stats.failure_rate == 0.0
        assert not stats.disabled

    def test_failure_rate_calculation(self) -> None:
        stats = AdapterStats(adapter_name="claude", successes=7, failures=3)
        assert stats.total == 10
        assert stats.failure_rate == pytest.approx(0.3)


class TestAdapterHealthMonitor:
    def test_unknown_adapter_is_healthy(self) -> None:
        monitor = AdapterHealthMonitor()
        assert monitor.is_healthy("nonexistent")

    def test_record_success(self) -> None:
        monitor = AdapterHealthMonitor()
        monitor.record_success("claude")
        stats = monitor.get_stats("claude")
        assert stats is not None
        assert stats.successes == 1
        assert stats.failures == 0

    def test_record_failure(self) -> None:
        monitor = AdapterHealthMonitor()
        monitor.record_failure("codex")
        stats = monitor.get_stats("codex")
        assert stats is not None
        assert stats.failures == 1

    def test_auto_disable_on_high_failure_rate(self) -> None:
        config = AdapterHealthConfig(
            failure_threshold=0.5,
            min_samples=3,
            cooldown_seconds=300,
        )
        monitor = AdapterHealthMonitor(config=config)
        monitor.record_failure("codex")
        monitor.record_failure("codex")
        monitor.record_failure("codex")
        assert not monitor.is_healthy("codex")

    def test_no_disable_below_min_samples(self) -> None:
        config = AdapterHealthConfig(
            failure_threshold=0.5,
            min_samples=5,
        )
        monitor = AdapterHealthMonitor(config=config)
        monitor.record_failure("codex")
        monitor.record_failure("codex")
        monitor.record_failure("codex")
        assert monitor.is_healthy("codex")

    def test_no_disable_below_threshold(self) -> None:
        config = AdapterHealthConfig(
            failure_threshold=0.5,
            min_samples=3,
        )
        monitor = AdapterHealthMonitor(config=config)
        monitor.record_failure("claude")
        monitor.record_success("claude")
        monitor.record_success("claude")
        assert monitor.is_healthy("claude")

    def test_re_enable_after_cooldown(self) -> None:
        config = AdapterHealthConfig(
            failure_threshold=0.5,
            min_samples=3,
            cooldown_seconds=0.0,
        )
        monitor = AdapterHealthMonitor(config=config)
        monitor.record_failure("codex")
        monitor.record_failure("codex")
        monitor.record_failure("codex")
        stats = monitor.get_stats("codex")
        assert stats is not None
        assert stats.disabled
        # Cooldown is 0 so is_healthy re-enables immediately
        assert monitor.is_healthy("codex")
        stats2 = monitor.get_stats("codex")
        assert stats2 is not None
        assert not stats2.disabled

    def test_all_stats(self) -> None:
        monitor = AdapterHealthMonitor()
        monitor.record_success("claude")
        monitor.record_failure("codex")
        all_stats = monitor.all_stats()
        assert "claude" in all_stats
        assert "codex" in all_stats

    def test_reset_clears_stats(self) -> None:
        monitor = AdapterHealthMonitor()
        monitor.record_success("claude")
        monitor.reset("claude")
        assert monitor.get_stats("claude") is None

    def test_config_property(self) -> None:
        config = AdapterHealthConfig(cooldown_seconds=600)
        monitor = AdapterHealthMonitor(config=config)
        assert monitor.config.cooldown_seconds == 600

    def test_windowed_pruning(self) -> None:
        config = AdapterHealthConfig(window_seconds=0.0)
        monitor = AdapterHealthMonitor(config=config)
        monitor.record_success("claude")
        stats = monitor.get_stats("claude")
        assert stats is not None
        assert stats.total == 0

    def test_latency_tracking(self) -> None:
        monitor = AdapterHealthMonitor()
        monitor.record_success("claude", latency_ms=150.0)
        monitor.record_success("claude", latency_ms=250.0)
        stats = monitor.get_stats("claude")
        assert stats is not None
        assert stats.avg_latency_ms == pytest.approx(200.0)

    def test_latency_without_data(self) -> None:
        stats = AdapterStats(adapter_name="claude")
        assert stats.avg_latency_ms == pytest.approx(0.0)

    def test_record_success_no_latency(self) -> None:
        monitor = AdapterHealthMonitor()
        monitor.record_success("claude")
        stats = monitor.get_stats("claude")
        assert stats is not None
        assert stats.avg_latency_ms == pytest.approx(0.0)
        assert stats.successes == 1
