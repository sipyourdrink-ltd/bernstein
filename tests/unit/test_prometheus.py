"""Tests for Prometheus metrics."""

from __future__ import annotations

from prometheus_client import generate_latest

from bernstein.core.prometheus import (
    agent_spawn_duration,
    merge_duration,
    registry,
    update_metrics_from_status,
)


def test_prometheus_task_metrics() -> None:
    """Test task-related Prometheus metrics."""
    status_data = {
        "done": 5,
        "failed": 2,
        "open": 3,
        "claimed": 1,
        "per_role": [
            {"role": "backend", "done": 3, "failed": 1, "claimed": 1},
            {"role": "frontend", "done": 2, "failed": 1, "claimed": 0},
        ],
    }

    update_metrics_from_status(status_data)

    output = generate_latest(registry).decode("utf-8")

    assert 'bernstein_tasks_total{role="backend",status="done"} 3.0' in output
    assert 'bernstein_tasks_total{role="backend",status="failed"} 1.0' in output
    assert 'bernstein_tasks_total{role="all",status="done"} 5.0' in output
    assert 'bernstein_tasks_active{role="backend"} 1.0' in output
    assert 'bernstein_agents_active{role="backend"} 1.0' in output


def test_prometheus_cost_metrics() -> None:
    """Test cost-related Prometheus metrics."""
    status_data = {
        "total_cost_usd": 1.25,
        "cost_by_model_usd": {
            "gpt-4o": 0.75,
            "claude-3-5-sonnet": 0.50,
        },
    }

    update_metrics_from_status(status_data)

    output = generate_latest(registry).decode("utf-8")

    assert 'bernstein_cost_usd_total{adapter="total"} 1.25' in output
    assert 'bernstein_cost_usd_by_model_total{adapter="unknown",model="gpt-4o"} 0.75' in output


def test_prometheus_histograms() -> None:
    """Test histogram metrics exist."""
    agent_spawn_duration.labels(adapter="claude").observe(5.5)
    merge_duration.observe(12.3)

    output = generate_latest(registry).decode("utf-8")

    assert "bernstein_agent_spawn_duration_seconds_bucket" in output
    assert "bernstein_merge_duration_seconds_bucket" in output
