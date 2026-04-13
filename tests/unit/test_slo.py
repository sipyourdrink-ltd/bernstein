"""Tests for SLO and Error Budget tracking."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from bernstein.core.slo import ErrorBudgetAction, SLOStatus, SLOTracker


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
