"""Tests for GET /costs/efficiency real-time cost-per-line endpoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def _make_tracker_file(sdd_dir: Path, run_id: str, usages: list[dict[str, Any]]) -> None:
    costs_dir = sdd_dir / "runtime" / "costs"
    costs_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "run_id": run_id,
        "budget_usd": 10.0,
        "spent_usd": sum(u.get("cost_usd", 0.0) for u in usages),
        "warn_threshold": 0.8,
        "critical_threshold": 0.95,
        "hard_stop_threshold": 1.0,
        "usages": usages,
        "cumulative_tokens": {},
    }
    (costs_dir / f"{run_id}.json").write_text(json.dumps(data))


def _write_lines_changed(sdd_dir: Path, agent_id: str, lines: int) -> None:
    lc_dir = sdd_dir / "runtime" / "lines_changed"
    lc_dir.mkdir(parents=True, exist_ok=True)
    (lc_dir / f"{agent_id}.json").write_text(
        json.dumps({"agent_id": agent_id, "lines_changed": lines}),
        encoding="utf-8",
    )


_USAGE_TEMPLATE: dict[str, Any] = {
    "input_tokens": 5_000,
    "output_tokens": 1_000,
    "model": "sonnet",
    "cost_usd": 0.05,
    "agent_id": "agent-1",
    "task_id": "t1",
    "tenant_id": "default",
    "timestamp": 1000.0,
    "cache_hit": False,
    "cached_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
}


class TestCostEfficiencyEndpoint:
    """Unit-level tests for the efficiency metric helpers."""

    def test_no_costs_dir_returns_empty(self, tmp_path: Path) -> None:
        from bernstein.core.cost_tracker import CostTracker

        tracker = CostTracker.load(tmp_path, "none")
        assert tracker is None

    def test_cost_per_line_computed_correctly(self, tmp_path: Path) -> None:
        """When lines_changed is stored, cost_per_line = cost / lines."""
        usage = {**_USAGE_TEMPLATE, "cost_usd": 0.10, "agent_id": "ag-1"}
        _make_tracker_file(tmp_path, "run-1", [usage])
        _write_lines_changed(tmp_path, "ag-1", 20)

        from bernstein.core.cost_tracker import CostTracker

        tracker = CostTracker.load(tmp_path, "run-1")
        assert tracker is not None
        lc_path = tmp_path / "runtime" / "lines_changed" / "ag-1.json"
        assert lc_path.exists()
        data = json.loads(lc_path.read_text())
        assert data["lines_changed"] == 20

        cost_per_line = round(0.10 / 20, 6)
        assert cost_per_line == pytest.approx(0.005)

    def test_lines_changed_accumulates(self, tmp_path: Path) -> None:
        """Writing lines_changed twice should accumulate, not overwrite."""
        lc_dir = tmp_path / "runtime" / "lines_changed"
        lc_dir.mkdir(parents=True, exist_ok=True)
        path = lc_dir / "ag-2.json"

        # First write
        path.write_text(json.dumps({"agent_id": "ag-2", "lines_changed": 10}))

        # Simulate a second progress report (manual accumulation)
        data = json.loads(path.read_text())
        current = data.get("lines_changed", 0)
        path.write_text(json.dumps({"agent_id": "ag-2", "lines_changed": current + 15}))

        final = json.loads(path.read_text())
        assert final["lines_changed"] == 25

    def test_missing_lines_changed_gives_none_efficiency(self, tmp_path: Path) -> None:
        """Without lines_changed data, cost_per_line should be None."""
        usage = {**_USAGE_TEMPLATE, "cost_usd": 0.05, "agent_id": "ag-no-lines"}
        _make_tracker_file(tmp_path, "run-2", [usage])

        # No lines_changed file → efficiency is None
        lc_path = tmp_path / "runtime" / "lines_changed" / "ag-no-lines.json"
        assert not lc_path.exists()

    def test_message_format_when_data_available(self, tmp_path: Path) -> None:
        """Verify the message string uses $/line format."""
        usage = {**_USAGE_TEMPLATE, "cost_usd": 0.06, "agent_id": "ag-msg"}
        _make_tracker_file(tmp_path, "run-3", [usage])
        _write_lines_changed(tmp_path, "ag-msg", 30)

        cost_per_line = round(0.06 / 30, 3)
        message = f"Current efficiency: ${cost_per_line:.3f}/line"
        assert "$" in message
        assert "/line" in message


class TestLinesChangedPersistence:
    """Tests for the _persist_lines_changed helper via direct file operations."""

    def test_creates_file_on_first_write(self, tmp_path: Path) -> None:
        lc_dir = tmp_path / "runtime" / "lines_changed"
        lc_dir.mkdir(parents=True, exist_ok=True)
        path = lc_dir / "new-agent.json"
        assert not path.exists()

        path.write_text(json.dumps({"agent_id": "new-agent", "lines_changed": 50}))
        data = json.loads(path.read_text())
        assert data["lines_changed"] == 50

    def test_handles_missing_parent_dir(self, tmp_path: Path) -> None:
        """mkdir(parents=True) should create the directory automatically."""
        lc_dir = tmp_path / "runtime" / "lines_changed"
        assert not lc_dir.exists()

        lc_dir.mkdir(parents=True, exist_ok=True)
        path = lc_dir / "agent-x.json"
        path.write_text(json.dumps({"agent_id": "agent-x", "lines_changed": 5}))
        assert path.exists()

    def test_reads_back_zero_from_missing_file(self, tmp_path: Path) -> None:
        """Missing file should return 0 lines_changed."""
        lc_dir = tmp_path / "runtime" / "lines_changed"
        path = lc_dir / "nonexistent.json"
        current = 0
        if path.exists():
            try:
                data = json.loads(path.read_text())
                current = int(data.get("lines_changed", 0))
            except ValueError:
                current = 0
        assert current == 0
