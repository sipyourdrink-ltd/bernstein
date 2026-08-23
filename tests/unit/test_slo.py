"""Tests for SLO and Error Budget tracking."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bernstein.core.slo import (
    ErrorBudgetAction,
    SLOStatus,
    SLOTracker,
    error_budget_from_task_board,
)


def test_slo_tracker_initial_state() -> None:
    """Test SLO tracker starts with default targets."""
    tracker = SLOTracker()
    assert "task_success" in tracker.targets
    assert "merge_success" in tracker.targets
    assert "p95_duration" in tracker.targets
    assert tracker.error_budget.total_tasks == 0


def test_error_budget_burn_rate() -> None:
    """Test burn rate calculation."""
    tracker = SLOTracker()
    # 90% target. 10 tasks, 4 failed -> 40% failure rate.
    # allowed failure rate = 10%
    # burn rate = 40 / 10 = 4.0
    # budget_total = max(3, round(10 * 0.1)) = 3, remaining = 3 - 4 = -1 -> depleted
    tracker.error_budget.total_tasks = 10
    tracker.error_budget.failed_tasks = 4
    assert tracker.error_budget.burn_rate == pytest.approx(4.0)
    assert tracker.error_budget.is_depleted


def test_error_budget_remediation() -> None:
    """Test remediation actions when budget is depleted."""
    tracker = SLOTracker()
    tracker.error_budget.total_tasks = 10
    tracker.error_budget.failed_tasks = 4  # 60% success, exceeds floor of 3

    actions = tracker.error_budget_policy.get_actions(tracker.error_budget)
    assert ErrorBudgetAction.REDUCE_AGENTS in actions
    assert tracker.error_budget.status == SLOStatus.RED


def test_error_budget_floor_defaults_to_three() -> None:
    """Regression test: budget_total floor is tunable, default unchanged at 3."""
    from bernstein.core import defaults
    from bernstein.core.observability.slo import ErrorBudget

    defaults.reset()
    budget = ErrorBudget(total_tasks=10, failed_tasks=0)
    assert budget.budget_total == 3


def test_error_budget_floor_honors_tuning_override() -> None:
    """tuning.slo.error_budget_min_failures raises the tolerated-failure floor (#run5 audit)."""
    from bernstein.core import defaults
    from bernstein.core.observability.slo import ErrorBudget

    defaults.override("slo", {"error_budget_min_failures": 20})
    try:
        budget = ErrorBudget(total_tasks=10, failed_tasks=10)
        assert budget.budget_total == 20
        assert not budget.is_depleted
    finally:
        defaults.reset()


def test_error_budget_floor_clamps_negative_override() -> None:
    """A misconfigured negative error_budget_min_failures must not suppress the
    SLO-target-derived budget below what an unset floor would allow."""
    from bernstein.core import defaults
    from bernstein.core.observability.slo import ErrorBudget

    defaults.override("slo", {"error_budget_min_failures": -5})
    try:
        budget = ErrorBudget(total_tasks=10, failed_tasks=0)
        # round(10 * (1 - 0.90)) == 1, clamped floor of 0 must not pull this below 1.
        assert budget.budget_total == 1
    finally:
        defaults.reset()


def test_slo_tracker_update_from_collector() -> None:
    """Test updating SLO values from metrics collector."""
    tracker = SLOTracker()
    collector = MagicMock()

    # Mock task metrics
    m1 = MagicMock(success=True, start_time=100, end_time=200, janitor_passed=True)
    m2 = MagicMock(success=False, start_time=100, end_time=300, janitor_passed=False)
    collector._task_metrics = {"t1": m1, "t2": m2}

    tracker.update_from_collector(collector)

    assert tracker.targets["task_success"].current == pytest.approx(0.5)
    assert tracker.targets["merge_success"].current == pytest.approx(0.5)
    assert tracker.error_budget.total_tasks == 2
    assert tracker.error_budget.failed_tasks == 1


# ---------------------------------------------------------------------------
# error_budget_from_task_board (#4310)
# ---------------------------------------------------------------------------


def _task(task_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=task_id)


def test_error_budget_from_task_board_counts_terminal_states_only() -> None:
    """Only done/failed count; open/claimed tasks are not terminal yet."""
    tasks_by_status = {
        "open": [_task("T-4"), _task("T-5")],
        "claimed": [_task("T-6")],
        "done": [_task("T-1"), _task("T-2")],
        "failed": [_task("T-3")],
    }
    budget = error_budget_from_task_board(tasks_by_status)
    assert budget.total_tasks == 3  # 2 done + 1 failed, not all 6 on the board
    assert budget.failed_tasks == 1


def test_error_budget_from_task_board_agent_death_after_done_not_counted() -> None:
    """Reproduces the issue: complete a task, then SIGTERM its agent.

    The board only knows the task reached `done`; agent process liveness
    is not an input to this function at all, so there is no way for an
    agent death to move the ratio.
    """
    tasks_by_status = {"open": [], "claimed": [], "done": [_task("T-1")], "failed": []}
    budget = error_budget_from_task_board(tasks_by_status)
    assert budget.total_tasks == 1
    assert budget.failed_tasks == 0
    assert not budget.is_depleted


def test_error_budget_from_task_board_matches_board_not_collector() -> None:
    """Regression for the reported evidence: board showed 1 failed of 12
    (2 done, 9 open/claimed) while the collector-derived ratio claimed
    4 failures out of 6. The board-derived budget must match the board."""
    tasks_by_status = {
        "open": [_task(f"T-open-{i}") for i in range(6)],
        "claimed": [_task(f"T-claimed-{i}") for i in range(3)],
        "done": [_task("T-done-1"), _task("T-done-2")],
        "failed": [_task("T-failed-1")],
    }
    budget = error_budget_from_task_board(tasks_by_status)
    assert budget.total_tasks == 3
    assert budget.failed_tasks == 1


def test_error_budget_from_task_board_missing_keys_default_to_empty() -> None:
    """Defensive: a board snapshot without done/failed keys counts as zero,
    not an error (mirrors fetch_all_tasks' early-return {} on server errors)."""
    budget = error_budget_from_task_board({})
    assert budget.total_tasks == 0
    assert budget.failed_tasks == 0
    assert not budget.is_depleted


def test_error_budget_from_task_board_honors_slo_target() -> None:
    budget = error_budget_from_task_board({"done": [], "failed": []}, slo_target=0.5)
    assert budget.slo_target == 0.5
