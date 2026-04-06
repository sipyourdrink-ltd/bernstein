"""Tests for COST-003: /costs/current real-time cost API endpoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def _make_tracker_file(sdd_dir: Path, run_id: str, budget: float, usages: list[dict[str, Any]]) -> None:
    """Write a cost tracker JSON file to the fake sdd_dir."""
    costs_dir = sdd_dir / "runtime" / "costs"
    costs_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "run_id": run_id,
        "budget_usd": budget,
        "spent_usd": sum(u.get("cost_usd", 0.0) for u in usages),
        "warn_threshold": 0.8,
        "critical_threshold": 0.95,
        "hard_stop_threshold": 1.0,
        "usages": usages,
        "cumulative_tokens": {},
    }
    (costs_dir / f"{run_id}.json").write_text(json.dumps(data))


class TestCostsCurrentEndpoint:
    """Test the /costs/current route handler logic."""

    def test_empty_costs_dir(self, tmp_path: Path) -> None:
        from bernstein.core.cost_tracker import CostTracker

        # No costs directory
        tracker = CostTracker.load(tmp_path, "nonexistent")
        assert tracker is None

    def test_single_run_tracker(self, tmp_path: Path) -> None:
        from bernstein.core.cost_tracker import CostTracker

        usages = [
            {
                "input_tokens": 1000,
                "output_tokens": 500,
                "model": "sonnet",
                "cost_usd": 0.05,
                "agent_id": "agent-1",
                "task_id": "task-1",
                "tenant_id": "default",
                "timestamp": 1000.0,
                "cache_hit": False,
                "cached_tokens": 0,
                "cache_read_tokens": 100,
                "cache_write_tokens": 50,
            },
            {
                "input_tokens": 800,
                "output_tokens": 300,
                "model": "opus",
                "cost_usd": 0.10,
                "agent_id": "agent-2",
                "task_id": "task-2",
                "tenant_id": "default",
                "timestamp": 1001.0,
                "cache_hit": False,
                "cached_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            },
        ]
        _make_tracker_file(tmp_path, "run-1", budget=5.0, usages=usages)

        tracker = CostTracker.load(tmp_path, "run-1")
        assert tracker is not None

        # Check budget status
        status = tracker.status()
        assert status.budget_usd == pytest.approx(5.0)
        assert status.spent_usd == pytest.approx(0.15, abs=1e-4)
        assert status.should_stop is False

        # Check model breakdowns include input/output tokens
        breakdowns = tracker.model_breakdowns()
        assert len(breakdowns) == 2
        models = {bd.model: bd for bd in breakdowns}
        assert models["sonnet"].input_tokens == 1000
        assert models["sonnet"].output_tokens == 500
        assert models["sonnet"].cache_read_tokens == 100
        assert models["opus"].input_tokens == 800
        assert models["opus"].output_tokens == 300

    def test_model_breakdown_to_dict(self, tmp_path: Path) -> None:
        """Verify to_dict output is JSON-serializable and complete."""
        from bernstein.core.cost_tracker import CostTracker

        usages = [
            {
                "input_tokens": 500,
                "output_tokens": 200,
                "model": "sonnet",
                "cost_usd": 0.02,
                "agent_id": "agent-1",
                "task_id": "task-1",
                "tenant_id": "default",
                "timestamp": 1000.0,
                "cache_hit": False,
                "cached_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            },
        ]
        _make_tracker_file(tmp_path, "run-2", budget=0.0, usages=usages)

        tracker = CostTracker.load(tmp_path, "run-2")
        assert tracker is not None

        breakdowns = tracker.model_breakdowns()
        assert len(breakdowns) == 1
        d = breakdowns[0].to_dict()
        # Must be JSON serializable
        json_str = json.dumps(d)
        parsed = json.loads(json_str)
        assert parsed["model"] == "sonnet"
        assert parsed["input_tokens"] == 500
        assert parsed["output_tokens"] == 200
        assert "cache_read_tokens" in parsed
        assert "cache_write_tokens" in parsed
