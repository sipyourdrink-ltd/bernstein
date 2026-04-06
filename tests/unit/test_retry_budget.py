"""Tests for task retry budget tracking (TASK-010)."""

from __future__ import annotations

import pytest

from bernstein.core.retry_budget import RetryBudget, RetryBudgetConfig, TaskRetryState


class TestRetryBudgetConfig:
    def test_defaults(self) -> None:
        config = RetryBudgetConfig()
        assert config.max_retries_per_task == 3
        assert config.max_retries_per_run == 50
        assert config.backoff_base_seconds == 5.0
        assert config.backoff_max_seconds == 300.0


class TestTaskRetryState:
    def test_not_exhausted_initially(self) -> None:
        state = TaskRetryState(task_id="t1", max_retries=3)
        assert not state.exhausted
        assert state.remaining == 3

    def test_exhausted_after_max(self) -> None:
        state = TaskRetryState(task_id="t1", retries=3, max_retries=3)
        assert state.exhausted
        assert state.remaining == 0


class TestRetryBudget:
    def test_can_retry_initially(self) -> None:
        budget = RetryBudget(RetryBudgetConfig(max_retries_per_task=3))
        assert budget.can_retry("t1")

    def test_record_retry(self) -> None:
        budget = RetryBudget(RetryBudgetConfig(max_retries_per_task=3))
        record = budget.record_retry("t1", reason="test failure", timestamp=100.0)
        assert record.task_id == "t1"
        assert record.attempt == 1
        assert record.reason == "test failure"
        assert budget.total_retries == 1

    def test_per_task_exhaustion(self) -> None:
        budget = RetryBudget(RetryBudgetConfig(max_retries_per_task=2, max_retries_per_run=0))
        budget.record_retry("t1", timestamp=100.0)
        budget.record_retry("t1", timestamp=200.0)
        assert not budget.can_retry("t1")
        with pytest.raises(ValueError, match="retry budget exhausted"):
            budget.record_retry("t1", timestamp=300.0)

    def test_per_run_exhaustion(self) -> None:
        budget = RetryBudget(RetryBudgetConfig(max_retries_per_task=5, max_retries_per_run=3))
        budget.record_retry("t1", timestamp=100.0)
        budget.record_retry("t2", timestamp=200.0)
        budget.record_retry("t3", timestamp=300.0)
        assert budget.run_budget_exhausted
        assert not budget.can_retry("t4")

    def test_unlimited_run_budget(self) -> None:
        budget = RetryBudget(RetryBudgetConfig(max_retries_per_task=2, max_retries_per_run=0))
        for i in range(100):
            budget.record_retry(f"t{i}", timestamp=float(i))
        assert not budget.run_budget_exhausted
        assert budget.run_budget_remaining == -1

    def test_backoff_increases(self) -> None:
        budget = RetryBudget(RetryBudgetConfig(backoff_base_seconds=5.0, max_retries_per_task=5))
        assert budget.backoff_seconds("t1") == 5.0  # 5 * 2^0
        budget.record_retry("t1", timestamp=100.0)
        assert budget.backoff_seconds("t1") == 10.0  # 5 * 2^1
        budget.record_retry("t1", timestamp=200.0)
        assert budget.backoff_seconds("t1") == 20.0  # 5 * 2^2

    def test_backoff_capped(self) -> None:
        config = RetryBudgetConfig(
            backoff_base_seconds=100.0,
            backoff_max_seconds=300.0,
            max_retries_per_task=10,
        )
        budget = RetryBudget(config)
        for i in range(5):
            budget.record_retry("t1", timestamp=float(i))
        assert budget.backoff_seconds("t1") <= 300.0

    def test_get_state(self) -> None:
        budget = RetryBudget(RetryBudgetConfig(max_retries_per_task=3))
        budget.record_retry("t1", reason="test", timestamp=100.0)
        state = budget.get_state("t1")
        assert state.retries == 1
        assert state.max_retries == 3
        assert len(state.records) == 1

    def test_summary(self) -> None:
        budget = RetryBudget(RetryBudgetConfig(max_retries_per_task=3, max_retries_per_run=10))
        budget.record_retry("t1", timestamp=100.0)
        budget.record_retry("t2", timestamp=200.0)
        summary = budget.summary()
        assert summary["total_retries"] == 2
        assert summary["run_budget_remaining"] == 8
        assert not summary["run_budget_exhausted"]
        per_task = summary["per_task"]
        assert isinstance(per_task, dict)
        assert "t1" in per_task
        assert "t2" in per_task

    def test_different_tasks_independent_budgets(self) -> None:
        budget = RetryBudget(RetryBudgetConfig(max_retries_per_task=2, max_retries_per_run=0))
        budget.record_retry("t1", timestamp=100.0)
        budget.record_retry("t1", timestamp=200.0)
        assert not budget.can_retry("t1")
        assert budget.can_retry("t2")  # t2 has its own budget
