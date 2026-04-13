"""Unit tests for completion budgets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bernstein.core.completion_budget import CompletionBudget


def test_first_attempt_within_budget(tmp_path: Path, make_task: Any) -> None:
    status = CompletionBudget(tmp_path).check(make_task(title="Original task"))

    assert status.budget_remaining == 5
    assert status.is_exhausted is False


def test_exhausted_after_max_attempts(tmp_path: Path, make_task: Any) -> None:
    budget = CompletionBudget(tmp_path)
    task = make_task(title="Original task")
    for _ in range(5):
        budget.record_attempt(task)

    status = budget.check(task)

    assert status.is_exhausted is True
    assert status.recommendation == "abandon"


def test_max_fix_tasks(tmp_path: Path, make_task: Any) -> None:
    budget = CompletionBudget(tmp_path)
    task = make_task(title="Original task")
    budget.record_attempt(task, is_fix=True)
    budget.record_attempt(task, is_fix=True)

    should_create, reason = budget.should_create_fix_task(task)

    assert should_create is False
    assert reason == "max fix tasks reached"


def test_lineage_tracking(tmp_path: Path, make_task: Any) -> None:
    budget = CompletionBudget(tmp_path)
    original = make_task(title="Original task")
    retry_fix = make_task(title="[RETRY 2] [FIX 1] Original task")
    budget.record_attempt(retry_fix)

    assert budget.check(original).total_attempts == 1


def test_record_and_check(tmp_path: Path, make_task: Any) -> None:
    budget = CompletionBudget(tmp_path)
    task = make_task(title="Original task")
    for _ in range(3):
        budget.record_attempt(task)

    status = budget.check(task)

    assert status.total_attempts == 3
    assert status.budget_remaining == 2


def test_independent_tasks_separate_budgets(tmp_path: Path, make_task: Any) -> None:
    budget = CompletionBudget(tmp_path)
    budget.record_attempt(make_task(title="Task A"))
    budget.record_attempt(make_task(title="Task B"))

    assert budget.check(make_task(title="Task A")).total_attempts == 1
    assert budget.check(make_task(title="Task B")).total_attempts == 1


def test_cost_tracking(tmp_path: Path, make_task: Any) -> None:
    budget = CompletionBudget(tmp_path)
    task = make_task(title="Original task")
    budget.record_attempt(task, cost_usd=1.2)
    budget.record_attempt(task, cost_usd=0.8)
    budget.record_attempt(task, cost_usd=0.5)

    assert budget.check(task).total_cost_usd == pytest.approx(2.5)
