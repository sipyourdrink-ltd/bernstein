"""Tests for the golden test suite."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bernstein.benchmark.golden import GoldenEvalRunner, GoldenTask, load_golden_suite


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


@pytest.mark.asyncio
@pytest.mark.parametrize("second_passed", [True, False])
async def test_reused_runner_keeps_suite_summaries_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second_passed: bool
) -> None:
    runner = GoldenEvalRunner(tmp_path, "")
    task = GoldenTask("case", "Case", "", "dev")
    monkeypatch.setattr("bernstein.benchmark.golden.load_golden_suite", lambda: [task])
    first_result = {"task_id": task.id, "passed": True, "cost_usd": 0.25}
    second_result = {"task_id": task.id, "passed": second_passed, "cost_usd": 0.5}
    monkeypatch.setattr(runner, "run_task", AsyncMock(side_effect=[first_result, second_result]))

    first = await runner.run_suite()
    second = await runner.run_suite()

    for summary, result, passed in ((first, first_result, True), (second, second_result, second_passed)):
        assert summary["total_tasks"] == 1
        assert summary["passed"] == int(passed)
        assert summary["failed"] == int(not passed)
        assert summary["total_cost_usd"] == result["cost_usd"]
        assert summary["tasks"] == [result]
    assert runner.results == [second_result]
