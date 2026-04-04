"""Tests for event sink kill-switch config in MetricsCollector and Prometheus."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.metric_collector import EventSink, MetricsCollector, PrivacyLevel
from bernstein.core import prometheus as prom_module


# ---------------------------------------------------------------------------
# EventSink enum
# ---------------------------------------------------------------------------


def test_event_sink_values_exist() -> None:
    """EventSink enum must expose FILE and PLUGIN members."""
    assert EventSink.FILE.value == "file"
    assert EventSink.PLUGIN.value == "plugin"


# ---------------------------------------------------------------------------
# MetricsCollector — disabled_sinks property and is_sink_enabled
# ---------------------------------------------------------------------------


def test_all_sinks_enabled_by_default() -> None:
    """All sinks are enabled when disabled_sinks is not provided."""
    collector = MetricsCollector()
    assert collector.is_sink_enabled(EventSink.FILE)
    assert collector.is_sink_enabled(EventSink.PLUGIN)
    assert collector.disabled_sinks == frozenset()


def test_file_sink_disabled() -> None:
    """FILE sink reports as disabled when included in disabled_sinks."""
    collector = MetricsCollector(disabled_sinks=frozenset({EventSink.FILE}))
    assert not collector.is_sink_enabled(EventSink.FILE)
    assert collector.is_sink_enabled(EventSink.PLUGIN)


def test_plugin_sink_disabled() -> None:
    """PLUGIN sink reports as disabled when included in disabled_sinks."""
    collector = MetricsCollector(disabled_sinks=frozenset({EventSink.PLUGIN}))
    assert collector.is_sink_enabled(EventSink.FILE)
    assert not collector.is_sink_enabled(EventSink.PLUGIN)


def test_both_sinks_disabled() -> None:
    """Both sinks can be disabled simultaneously."""
    all_sinks = frozenset({EventSink.FILE, EventSink.PLUGIN})
    collector = MetricsCollector(disabled_sinks=all_sinks)
    assert not collector.is_sink_enabled(EventSink.FILE)
    assert not collector.is_sink_enabled(EventSink.PLUGIN)
    assert collector.disabled_sinks == all_sinks


# ---------------------------------------------------------------------------
# File sink kill-switch — no files written when EventSink.FILE is disabled
# ---------------------------------------------------------------------------


def test_file_sink_disabled_prevents_file_writes(tmp_path: Path) -> None:
    """Disabling FILE sink stops all JSONL file creation."""
    metrics_dir = tmp_path / "metrics"
    collector = MetricsCollector(
        metrics_dir=metrics_dir,
        disabled_sinks=frozenset({EventSink.FILE}),
    )
    collector.start_task("T-1", role="backend", model="sonnet", provider="openai")
    collector.complete_task("T-1", success=True, tokens_used=100, cost_usd=1.0)
    collector.flush()

    assert not metrics_dir.exists() or not list(metrics_dir.glob("*.jsonl"))


def test_file_sink_enabled_writes_files(tmp_path: Path) -> None:
    """With FILE sink enabled (default), JSONL files are created on flush."""
    metrics_dir = tmp_path / "metrics"
    collector = MetricsCollector(metrics_dir=metrics_dir)
    collector.start_task("T-1", role="backend", model="sonnet", provider="openai")
    collector.complete_task("T-1", success=True, tokens_used=100, cost_usd=1.0)
    collector.flush()

    assert list(metrics_dir.glob("*.jsonl")), "Expected at least one JSONL file"


# ---------------------------------------------------------------------------
# Plugin sink kill-switch — hook not called when EventSink.PLUGIN is disabled
# ---------------------------------------------------------------------------


def test_plugin_sink_disabled_suppresses_hook(tmp_path: Path) -> None:
    """Disabling PLUGIN sink prevents on_metric_record hook from being called."""
    collector = MetricsCollector(
        metrics_dir=tmp_path / "m",
        disabled_sinks=frozenset({EventSink.PLUGIN}),
    )
    with patch.object(collector, "_emit_metric_hook") as mock_hook:
        collector.start_task("T-1", role="qa", model="haiku", provider="anthropic")
        collector.complete_task("T-1", success=True)
        # _emit_metric_hook is called directly inside _write_metric_point,
        # but since PLUGIN is disabled the collector must not invoke it.
        mock_hook.assert_not_called()


def test_plugin_sink_enabled_calls_hook(tmp_path: Path) -> None:
    """With PLUGIN sink enabled, _emit_metric_hook is called on metric writes."""
    collector = MetricsCollector(metrics_dir=tmp_path / "m")
    with patch.object(collector, "_emit_metric_hook") as mock_hook:
        collector.start_task("T-1", role="qa", model="haiku", provider="anthropic")
        collector.complete_task("T-1", success=True)
        assert mock_hook.call_count > 0


# ---------------------------------------------------------------------------
# In-memory metrics are unaffected by sink kill-switches
# ---------------------------------------------------------------------------


def test_metrics_summary_unaffected_by_file_sink_kill(tmp_path: Path) -> None:
    """In-memory get_metrics_summary still works when FILE sink is disabled."""
    collector = MetricsCollector(
        metrics_dir=tmp_path / "m",
        disabled_sinks=frozenset({EventSink.FILE}),
    )
    collector.start_agent("A-1", role="backend", model="sonnet", provider="openai")
    collector.complete_agent_task("A-1", success=True, tokens_used=50, cost_usd=0.50)
    collector.end_agent("A-1")

    summary = collector.get_metrics_summary()
    assert summary["total_agents"] == 1
    assert summary["total_cost_usd"] == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# Prometheus kill-switch
# ---------------------------------------------------------------------------


def test_set_prometheus_enabled_disables_sink() -> None:
    """set_prometheus_enabled(False) makes update_metrics_from_status a no-op."""
    # Ensure Prometheus starts enabled
    prom_module.set_prometheus_enabled(True)

    status = {
        "open": 5,
        "claimed": 2,
        "done": 10,
        "failed": 1,
        "total_cost_usd": 3.0,
        "per_role": [],
    }
    # Baseline — call once with sink enabled so delta tracking is initialised
    prom_module.update_metrics_from_status(status)
    depth_after_first = prom_module.task_queue_depth._value.get()  # type: ignore[attr-defined]

    # Disable the sink — subsequent calls must not change gauges
    prom_module.set_prometheus_enabled(False)
    status_updated = dict(status, open=99)
    prom_module.update_metrics_from_status(status_updated)

    depth_after_disabled = prom_module.task_queue_depth._value.get()  # type: ignore[attr-defined]
    assert depth_after_first == depth_after_disabled, (
        "queue_depth gauge changed even though Prometheus sink was disabled"
    )


def test_set_prometheus_enabled_re_enables_sink() -> None:
    """set_prometheus_enabled(True) restores normal Prometheus behaviour."""
    prom_module.set_prometheus_enabled(False)
    prom_module.set_prometheus_enabled(True)

    # Should not raise and should process the update
    prom_module.update_metrics_from_status({"open": 1, "done": 0, "failed": 0, "per_role": []})
