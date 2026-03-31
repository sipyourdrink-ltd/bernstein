from pathlib import Path



from bernstein.core.cost_tracker import CostTracker


def test_cost_tracker_accumulate() -> None:
    """Test that cost and token usage accumulate correctly."""
    tracker = CostTracker(run_id="test-run", budget_usd=1.0)
    
    # Record first usage
    # sonnet rate is 0.009 per 1k tokens
    status = tracker.record(
        agent_id="agent-1",
        task_id="task-1",
        model="sonnet",
        input_tokens=1000,
        output_tokens=500
    )
    
    expected_cost = (1500 / 1000.0) * 0.009
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
    # sonnet: 1000 tokens = 0.009
    tracker.record("a1", "t1", "sonnet", 1000, 0)
    # opus: 1000 tokens = 0.015
    tracker.record("a2", "t2", "opus", 1000, 0)
    
    breakdown = tracker.model_breakdowns()
    # Should be sorted by cost descending: opus (0.015) then sonnet (0.009)
    assert len(breakdown) == 2
    assert breakdown[0].model == "opus"
    assert round(breakdown[0].total_cost_usd, 6) == 0.015
    assert breakdown[1].model == "sonnet"
    assert round(breakdown[1].total_cost_usd, 6) == 0.009


def test_cost_tracker_budget_check() -> None:
    """Test budget threshold detection (warn/stop)."""
    # Budget of $0.02, warn at 50% ($0.01)
    tracker = CostTracker(run_id="test-run", budget_usd=0.02, warn_threshold=0.5)
    
    # 1. Below warning: 1000 sonnet tokens = $0.009 < $0.01
    status = tracker.record("a1", "t1", "sonnet", 1000, 0)
    assert not status.should_warn
    assert not status.should_stop
    
    # 2. Above warning: 2000 sonnet tokens = $0.018 > $0.01
    status = tracker.record("a1", "t2", "sonnet", 1000, 0)
    assert status.should_warn
    assert not status.should_stop
    
    # 3. Above hard stop: 3000 sonnet tokens = $0.027 > $0.02
    status = tracker.record("a1", "t3", "sonnet", 1000, 0)
    assert status.should_stop


def test_cost_tracker_agent_summaries() -> None:
    """Test per-agent cost summaries."""
    tracker = CostTracker(run_id="test-run")
    tracker.record("agent-A", "t1", "sonnet", 1000, 0)
    tracker.record("agent-A", "t2", "sonnet", 500, 0)
    tracker.record("agent-B", "t3", "opus", 1000, 0)
    
    summaries = tracker.agent_summaries()
    # Sorted by cost descending: agent-B (0.015) then agent-A (0.0135)
    assert len(summaries) == 2
    assert summaries[0].agent_id == "agent-B"
    assert round(summaries[0].total_cost_usd, 6) == 0.015
    assert summaries[1].agent_id == "agent-A"
    assert round(summaries[1].total_cost_usd, 6) == 0.0135
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
    
    # 1 task done, cost $0.01
    tracker.record("a1", "t1", "sonnet", 1000, 111) # approx 0.01
    cost = tracker.spent_usd
    
    # Project with 9 tasks remaining
    proj = tracker.project(tasks_done=1, tasks_remaining=9)
    # Projected total = 0.01 + (0.01 * 9) = 0.10
    assert round(proj.projected_total_usd, 6) == round(cost * 10, 6)
    assert proj.within_budget is True
    
    # Project with more tasks remaining -> should exceed budget
    proj2 = tracker.project(tasks_done=1, tasks_remaining=10)
    assert proj2.within_budget is False
    assert proj2.confidence == 0.2 # 1/5
