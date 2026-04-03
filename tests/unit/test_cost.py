from __future__ import annotations

from pathlib import Path

from bernstein.core.cost import forecast_planned_backlog
from bernstein.core.cost_tracker import CostTracker
from bernstein.core.models import Complexity, Scope, Task, TaskStatus


def test_cost_tracker_accumulate() -> None:
    """Test that cost and token usage accumulate correctly."""
    tracker = CostTracker(run_id="test-run", budget_usd=1.0)

    # Record first usage
    # sonnet: input $3, output $15 per 1M tokens
    status = tracker.record(agent_id="agent-1", task_id="task-1", model="sonnet", input_tokens=1000, output_tokens=500)

    expected_cost = (1000 / 1_000_000.0 * 3.0) + (500 / 1_000_000.0 * 15.0)
    assert round(tracker.spent_usd, 6) == round(expected_cost, 6)
    assert len(tracker.usages) == 1
    assert status.spent_usd == tracker.spent_usd

    # Record second usage
    tracker.record("agent-1", "task-2", "sonnet", 1000, 500)
    assert round(tracker.spent_usd, 6) == round(expected_cost * 2, 6)
    assert len(tracker.usages) == 2


def test_cost_tracker_per_model_breakdown() -> None:
    """Test per-model cost breakdown reporting."""
    tracker = CostTracker(run_id="test-run")
    # sonnet: 1000 input = 0.003
    tracker.record("a1", "t1", "sonnet", 1000, 0)
    # opus: 1000 input = 0.005
    tracker.record("a2", "t2", "opus", 1000, 0)

    breakdown = tracker.model_breakdowns()
    # Should be sorted by cost descending: opus (0.005) then sonnet (0.003)
    assert len(breakdown) == 2
    assert breakdown[0].model == "opus"
    assert round(breakdown[0].total_cost_usd, 6) == 0.005
    assert breakdown[1].model == "sonnet"
    assert round(breakdown[1].total_cost_usd, 6) == 0.003


def test_cost_tracker_budget_check() -> None:
    """Test budget threshold detection (warn/stop)."""
    # Budget of $0.01, warn at 50% ($0.005)
    tracker = CostTracker(run_id="test-run", budget_usd=0.01, warn_threshold=0.5)

    # 1. Below warning: 1000 sonnet input tokens = $0.003 < $0.005
    status = tracker.record("a1", "t1", "sonnet", 1000, 0)
    assert not status.should_warn
    assert not status.should_stop

    # 2. Above warning: 2000 sonnet tokens = $0.006 > $0.005
    status = tracker.record("a1", "t2", "sonnet", 1000, 0)
    assert status.should_warn
    assert not status.should_stop

    # 3. Above hard stop: 4000 sonnet tokens = $0.012 > $0.01
    status = tracker.record("a1", "t3", "sonnet", 2000, 0)
    assert status.should_stop


def test_cost_tracker_agent_summaries() -> None:
    """Test per-agent cost summaries."""
    tracker = CostTracker(run_id="test-run")
    tracker.record("agent-A", "t1", "sonnet", 1000, 0)  # 0.003
    tracker.record("agent-A", "t2", "sonnet", 500, 0)  # 0.0015
    tracker.record("agent-B", "t3", "opus", 1000, 0)  # 0.005

    summaries = tracker.agent_summaries()
    # Sorted by cost descending: agent-B (0.005) then agent-A (0.0045)
    assert len(summaries) == 2
    assert summaries[0].agent_id == "agent-B"
    assert round(summaries[0].total_cost_usd, 6) == 0.005
    assert summaries[1].agent_id == "agent-A"
    assert round(summaries[1].total_cost_usd, 6) == 0.0045
    assert summaries[1].task_count == 2


def test_cost_tracker_persistence(tmp_path: Path) -> None:
    """Test saving and loading cost tracker state."""
    tracker = CostTracker(run_id="run-123", budget_usd=5.0)
    tracker.record("a1", "t1", "sonnet", 100, 100)

    save_path = tracker.save(tmp_path)
    assert save_path.exists()
    assert save_path.name == "run-123.json"

    loaded = CostTracker.load(tmp_path, "run-123")
    assert loaded is not None
    assert loaded.run_id == "run-123"
    assert loaded.budget_usd == 5.0
    assert round(loaded.spent_usd, 6) == round(tracker.spent_usd, 6)
    assert len(loaded.usages) == 1
    assert loaded.usages[0].agent_id == "a1"


def test_cost_tracker_projection() -> None:
    """Test cost projection logic."""
    tracker = CostTracker(run_id="test-run", budget_usd=0.10)

    # 1 task done, cost $0.01 (approx)
    # Using sonnet: $3/$15 per 1M. 2500 input + 500 output = 0.0075 + 0.0075 = 0.015
    tracker.record("a1", "t1", "sonnet", 2500, 500)
    cost = tracker.spent_usd

    # Project with 5 tasks remaining
    proj = tracker.project(tasks_done=1, tasks_remaining=5)
    # Projected total = 0.015 + (0.015 * 5) = 0.015 + 0.075 = 0.09
    assert round(proj.projected_total_usd, 6) == round(cost * 6, 6)
    assert proj.within_budget is True

    # Project with more tasks remaining -> should exceed budget
    # Projected total = 0.015 + (0.015 * 10) = 0.015 + 0.15 = 0.165
    proj2 = tracker.project(tasks_done=1, tasks_remaining=10)
    assert proj2.within_budget is False
    assert proj2.confidence == 0.2  # 1/5


def test_forecast_planned_backlog_uses_non_terminal_tasks_only(tmp_path: Path) -> None:
    tasks = [
        Task(
            id="open-1",
            title="Implement API",
            description="Add backend endpoint",
            role="backend",
            scope=Scope.MEDIUM,
            complexity=Complexity.MEDIUM,
            status=TaskStatus.OPEN,
        ),
        Task(
            id="done-1",
            title="Ship docs",
            description="Completed work",
            role="docs",
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
            status=TaskStatus.DONE,
        ),
    ]

    forecast = forecast_planned_backlog(tasks, metrics_dir=tmp_path, current_spend_usd=0.5, budget_usd=2.0)

    assert forecast.task_count == 1
    assert forecast.current_spend_usd == 0.5
    assert forecast.projected_total_cost_usd > 0.5
    assert forecast.within_budget is True


def test_forecast_planned_backlog_rolls_up_by_role(tmp_path: Path) -> None:
    tasks = [
        Task(
            id="t-1",
            title="Backend change",
            description="Add API",
            role="backend",
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
            status=TaskStatus.OPEN,
        ),
        Task(
            id="t-2",
            title="UI change",
            description="Polish dashboard",
            role="frontend",
            scope=Scope.MEDIUM,
            complexity=Complexity.MEDIUM,
            status=TaskStatus.PLANNED,
        ),
    ]

    forecast = forecast_planned_backlog(tasks, metrics_dir=tmp_path)

    assert {entry.role for entry in forecast.per_role} == {"backend", "frontend"}
    assert sum(entry.task_count for entry in forecast.per_role) == 2
