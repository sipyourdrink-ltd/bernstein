"""Tests for the golden test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.benchmark.golden import GoldenEvalRunner, load_golden_suite


def test_load_golden_suite() -> None:
    """Test loading the golden tasks."""
    tasks = load_golden_suite()
    assert len(tasks) >= 3
    assert tasks[0].id.startswith("golden-")


@pytest.mark.anyio
async def test_golden_eval_runner(tmp_path: Path) -> None:
    """Test running the golden suite (with mocked task results)."""
    runner = GoldenEvalRunner(tmp_path, "http://localhost:8052")

    summary = await runner.run_suite()

    assert summary["total_tasks"] >= 3
    assert summary["passed"] == summary["total_tasks"]
    assert summary["total_cost_usd"] > 0
    assert len(summary["tasks"]) == summary["total_tasks"]
    assert "timestamp" in summary
