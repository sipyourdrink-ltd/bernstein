"""Tests for historical cost comparison across runs (COST-009)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.cost_comparison import (
    CostComparison,
    CostTrendSummary,
    RunCostSnapshot,
    compare_runs,
    compute_trend,
    load_run_snapshots,
)


def _write_cost_file(metrics_dir: Path, run_id: str, total_spent: float, task_count: int = 3) -> None:
    """Write a mock cost report file."""
    metrics_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "run_id": run_id,
        "total_spent_usd": total_spent,
        "budget_usd": 10.0,
        "timestamp": 1000000.0 + hash(run_id) % 10000,
        "per_agent": [
            {"agent_id": f"a-{i}", "task_count": 1, "total_cost_usd": total_spent / task_count, "model_breakdown": {}}
            for i in range(task_count)
        ],
        "per_model": [],
        "projection": None,
    }
    (metrics_dir / f"costs_{run_id}.json").write_text(json.dumps(data))


def test_load_run_snapshots(tmp_path: Path) -> None:
    """load_run_snapshots reads cost files from disk."""
    metrics = tmp_path / "metrics"
    _write_cost_file(metrics, "run-1", 1.50)
    _write_cost_file(metrics, "run-2", 2.00)

    snapshots = load_run_snapshots(metrics)
    assert len(snapshots) == 2


def test_load_empty_dir(tmp_path: Path) -> None:
    """load_run_snapshots returns empty list for non-existent dir."""
    snapshots = load_run_snapshots(tmp_path / "nonexistent")
    assert snapshots == []


def test_compare_runs_basic(tmp_path: Path) -> None:
    """compare_runs computes delta vs most recent baseline."""
    metrics = tmp_path / "metrics"
    _write_cost_file(metrics, "run-old", 1.00, task_count=5)

    result = compare_runs("run-new", 1.50, 5, metrics)
    assert result is not None
    assert result.current_cost_usd == pytest.approx(1.50)
    assert result.baseline_cost_usd == pytest.approx(1.00)
    assert result.delta_usd == pytest.approx(0.50)
    assert result.delta_pct == pytest.approx(50.0)


def test_compare_runs_no_baseline(tmp_path: Path) -> None:
    """compare_runs returns None when no historical data exists."""
    metrics = tmp_path / "metrics"
    metrics.mkdir(parents=True, exist_ok=True)

    result = compare_runs("run-new", 1.50, 5, metrics)
    assert result is None


def test_compare_runs_specific_baseline(tmp_path: Path) -> None:
    """compare_runs uses a specific baseline when requested."""
    metrics = tmp_path / "metrics"
    _write_cost_file(metrics, "run-a", 1.00)
    _write_cost_file(metrics, "run-b", 2.00)

    result = compare_runs("run-new", 1.50, 3, metrics, baseline_run_id="run-a")
    assert result is not None
    assert result.baseline_run_id == "run-a"
    assert result.baseline_cost_usd == pytest.approx(1.00)


def test_compare_runs_excludes_self(tmp_path: Path) -> None:
    """compare_runs excludes the current run from baselines."""
    metrics = tmp_path / "metrics"
    _write_cost_file(metrics, "run-current", 1.50)

    result = compare_runs("run-current", 1.50, 3, metrics)
    assert result is None


def test_compute_trend_basic(tmp_path: Path) -> None:
    """compute_trend produces a trend summary."""
    metrics = tmp_path / "metrics"
    for i in range(5):
        _write_cost_file(metrics, f"run-{i}", 1.0 + i * 0.1)

    trend = compute_trend(metrics)
    assert isinstance(trend, CostTrendSummary)
    assert trend.run_count == 5
    assert trend.avg_cost_usd > 0
    assert trend.min_cost_usd <= trend.avg_cost_usd <= trend.max_cost_usd


def test_compute_trend_empty(tmp_path: Path) -> None:
    """compute_trend on empty metrics produces stable/zero."""
    trend = compute_trend(tmp_path / "nonexistent")
    assert trend.run_count == 0
    assert trend.trend_direction == "stable"


def test_comparison_to_dict() -> None:
    """CostComparison.to_dict has expected keys."""
    comp = CostComparison(
        current_run_id="r1",
        baseline_run_id="r0",
        current_cost_usd=2.0,
        baseline_cost_usd=1.0,
        delta_usd=1.0,
        delta_pct=100.0,
        current_task_count=5,
        baseline_task_count=5,
        cost_per_task_current=0.4,
        cost_per_task_baseline=0.2,
    )
    d = comp.to_dict()
    assert d["delta_usd"] == pytest.approx(1.0)
    assert d["delta_pct"] == pytest.approx(100.0)


def test_snapshot_to_dict() -> None:
    """RunCostSnapshot.to_dict roundtrip."""
    snap = RunCostSnapshot(
        run_id="r1",
        total_spent_usd=1.5,
        budget_usd=10.0,
        task_count=5,
        plan_path="plan.yaml",
        timestamp=1000.0,
    )
    d = snap.to_dict()
    assert d["run_id"] == "r1"
    assert d["total_spent_usd"] == pytest.approx(1.5)
