"""Tests for analytics privacy level presets in MetricsCollector."""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.metric_collector import MetricsCollector, PrivacyLevel

# ---------------------------------------------------------------------------
# PrivacyLevel.apply_privacy_filter
# ---------------------------------------------------------------------------


def test_privacy_full_passes_all_labels() -> None:
    """FULL privacy level preserves every label key unchanged."""
    collector = MetricsCollector(privacy_level=PrivacyLevel.FULL)
    labels = {
        "task_id": "T-001",
        "agent_id": "A-xyz",
        "session_id": "S-abc",
        "role": "backend",
        "model": "sonnet",
        "cost_usd": "1.25",
    }
    filtered = collector.apply_privacy_filter(labels)
    assert filtered == labels


def test_privacy_standard_strips_individual_identifiers() -> None:
    """STANDARD level removes task_id, agent_id, and session_id from labels."""
    collector = MetricsCollector(privacy_level=PrivacyLevel.STANDARD)
    labels = {
        "task_id": "T-001",
        "agent_id": "A-xyz",
        "session_id": "S-abc",
        "role": "backend",
        "model": "sonnet",
    }
    filtered = collector.apply_privacy_filter(labels)
    assert "task_id" not in filtered
    assert "agent_id" not in filtered
    assert "session_id" not in filtered
    # non-identifying operational data is kept
    assert filtered["role"] == "backend"
    assert filtered["model"] == "sonnet"


def test_privacy_minimal_strips_ids_and_cost_and_tokens() -> None:
    """MINIMAL level removes identifiers and cost/token labels."""
    collector = MetricsCollector(privacy_level=PrivacyLevel.MINIMAL)
    labels = {
        "task_id": "T-001",
        "agent_id": "A-xyz",
        "session_id": "S-abc",
        "role": "backend",
        "model": "sonnet",
        "cost_usd": "1.25",
        "tokens_used": "500",
        "latency_ms": "320",
    }
    filtered = collector.apply_privacy_filter(labels)
    assert "task_id" not in filtered
    assert "agent_id" not in filtered
    assert "session_id" not in filtered
    assert "cost_usd" not in filtered
    assert "tokens_used" not in filtered
    # structural labels still present
    assert "role" in filtered


def test_privacy_level_property_returns_configured_value() -> None:
    """privacy_level property reflects the value passed at construction."""
    for level in PrivacyLevel:
        collector = MetricsCollector(privacy_level=level)
        assert collector.privacy_level is level


def test_privacy_default_is_full() -> None:
    """MetricsCollector defaults to PrivacyLevel.FULL when not specified."""
    collector = MetricsCollector()
    assert collector.privacy_level is PrivacyLevel.FULL


# ---------------------------------------------------------------------------
# get_metrics_summary respects privacy level
# ---------------------------------------------------------------------------


def test_privacy_standard_preserves_cost_in_summary(tmp_path: Path) -> None:
    """STANDARD privacy level keeps cost data in get_metrics_summary."""
    collector = MetricsCollector(metrics_dir=tmp_path / "m", privacy_level=PrivacyLevel.STANDARD)
    collector.start_agent("A-1", role="backend", model="sonnet", provider="openai")
    collector.complete_agent_task("A-1", success=True, tokens_used=100, cost_usd=2.50)
    collector.end_agent("A-1")

    summary = collector.get_metrics_summary()
    assert summary["total_cost_usd"] == pytest.approx(2.50)


def test_privacy_minimal_zeros_cost_in_summary(tmp_path: Path) -> None:
    """MINIMAL privacy level suppresses cost data in get_metrics_summary."""
    collector = MetricsCollector(metrics_dir=tmp_path / "m", privacy_level=PrivacyLevel.MINIMAL)
    collector.start_agent("A-1", role="backend", model="sonnet", provider="openai")
    collector.complete_agent_task("A-1", success=True, tokens_used=100, cost_usd=2.50)
    collector.end_agent("A-1")

    summary = collector.get_metrics_summary()
    assert summary["total_cost_usd"] == pytest.approx(0.0)


def test_privacy_minimal_suppresses_cost_metric_writes(tmp_path: Path) -> None:
    """MINIMAL level does not write COST_EFFICIENCY or API_USAGE metric files."""
    metrics_dir = tmp_path / "metrics"
    collector = MetricsCollector(metrics_dir=metrics_dir, privacy_level=PrivacyLevel.MINIMAL)
    collector.start_task("T-1", role="backend", model="sonnet", provider="openai")
    collector.complete_task("T-1", success=True, tokens_used=200, cost_usd=3.00)

    collector.flush()

    written_files = list(metrics_dir.glob("*.jsonl"))
    written_names = {f.name for f in written_files}
    # No cost or token metric files should be present
    assert not any("cost_efficiency" in n for n in written_names)
    assert not any("api_usage" in n for n in written_names)
