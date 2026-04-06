"""Tests for per-agent cost attribution and leaderboard (COST-008)."""

from __future__ import annotations

import math

import pytest

from bernstein.core.agent_cost_ledger import AgentCostEntry, AgentCostLedger


def test_record_cost_creates_entry() -> None:
    """Recording cost creates a new entry for unknown agents."""
    ledger = AgentCostLedger(run_id="r1")
    ledger.record_cost("a1", role="backend", model="sonnet", cost_usd=0.05)
    entry = ledger.get_entry("a1")
    assert entry is not None
    assert entry.total_cost_usd == pytest.approx(0.05)


def test_record_cost_accumulates() -> None:
    """Multiple recordings accumulate on the same entry."""
    ledger = AgentCostLedger(run_id="r1")
    ledger.record_cost("a1", role="backend", model="sonnet", cost_usd=0.05)
    ledger.record_cost("a1", role="backend", model="sonnet", cost_usd=0.03)
    entry = ledger.get_entry("a1")
    assert entry is not None
    assert entry.total_cost_usd == pytest.approx(0.08)


def test_record_task_result_success() -> None:
    """Recording a success increments tasks_completed."""
    ledger = AgentCostLedger(run_id="r1")
    ledger.record_cost("a1", role="backend", model="sonnet", cost_usd=0.05)
    ledger.record_task_result("a1", success=True, duration_s=10.0)
    entry = ledger.get_entry("a1")
    assert entry is not None
    assert entry.tasks_completed == 1
    assert entry.tasks_failed == 0


def test_record_task_result_failure() -> None:
    """Recording a failure increments tasks_failed."""
    ledger = AgentCostLedger(run_id="r1")
    ledger.record_cost("a1", role="backend", model="sonnet", cost_usd=0.05)
    ledger.record_task_result("a1", success=False)
    entry = ledger.get_entry("a1")
    assert entry is not None
    assert entry.tasks_failed == 1


def test_cost_per_task() -> None:
    """cost_per_task divides total cost by completed tasks."""
    entry = AgentCostEntry(agent_id="a1", role="backend", model="sonnet", total_cost_usd=0.10, tasks_completed=2)
    assert entry.cost_per_task == pytest.approx(0.05)


def test_cost_per_task_no_completions() -> None:
    """cost_per_task is inf when no tasks completed."""
    entry = AgentCostEntry(agent_id="a1", role="backend", model="sonnet", total_cost_usd=0.10)
    assert math.isinf(entry.cost_per_task)


def test_success_rate() -> None:
    """success_rate = completed / (completed + failed)."""
    entry = AgentCostEntry(agent_id="a1", role="backend", model="sonnet", tasks_completed=3, tasks_failed=1)
    assert entry.success_rate == pytest.approx(0.75)


def test_success_rate_no_tasks() -> None:
    """success_rate is 0 when no tasks recorded."""
    entry = AgentCostEntry(agent_id="a1", role="backend", model="sonnet")
    assert entry.success_rate == pytest.approx(0.0)


def test_efficiency_score() -> None:
    """Agents with higher success rate and lower cost score higher."""
    cheap_good = AgentCostEntry(agent_id="a1", role="backend", model="haiku", total_cost_usd=0.01, tasks_completed=5)
    expensive_good = AgentCostEntry(agent_id="a2", role="backend", model="opus", total_cost_usd=0.10, tasks_completed=5)
    assert cheap_good.efficiency_score > expensive_good.efficiency_score


def test_leaderboard_ranking() -> None:
    """Leaderboard ranks by efficiency score descending."""
    ledger = AgentCostLedger(run_id="r1")
    # Agent a1: cheap and successful
    ledger.record_cost("a1", role="backend", model="haiku", cost_usd=0.01)
    ledger.record_task_result("a1", success=True)
    # Agent a2: expensive and successful
    ledger.record_cost("a2", role="backend", model="opus", cost_usd=0.10)
    ledger.record_task_result("a2", success=True)

    board = ledger.leaderboard()
    assert len(board) == 2
    assert board[0].rank == 1
    assert board[0].agent_id == "a1"  # cheaper = more efficient
    assert board[1].rank == 2


def test_leaderboard_min_tasks() -> None:
    """min_tasks filter excludes agents with too few completions."""
    ledger = AgentCostLedger(run_id="r1")
    ledger.record_cost("a1", role="backend", model="sonnet", cost_usd=0.05)
    ledger.record_task_result("a1", success=True)
    ledger.record_cost("a2", role="qa", model="haiku", cost_usd=0.01)
    # a2 has no completed tasks

    board = ledger.leaderboard(min_tasks=1)
    assert len(board) == 1
    assert board[0].agent_id == "a1"


def test_total_cost() -> None:
    """total_cost sums all agents."""
    ledger = AgentCostLedger(run_id="r1")
    ledger.record_cost("a1", role="backend", model="sonnet", cost_usd=0.05)
    ledger.record_cost("a2", role="qa", model="haiku", cost_usd=0.02)
    assert ledger.total_cost() == pytest.approx(0.07)


def test_to_dict() -> None:
    """to_dict serialises the full ledger."""
    ledger = AgentCostLedger(run_id="r1")
    ledger.record_cost("a1", role="backend", model="sonnet", cost_usd=0.05)
    d = ledger.to_dict()
    assert d["run_id"] == "r1"
    assert "agents" in d
    assert "a1" in d["agents"]


def test_entry_to_dict() -> None:
    """AgentCostEntry.to_dict has expected keys."""
    entry = AgentCostEntry(
        agent_id="a1",
        role="backend",
        model="sonnet",
        total_cost_usd=0.05,
        tasks_completed=2,
        tasks_failed=1,
    )
    d = entry.to_dict()
    assert d["agent_id"] == "a1"
    assert d["success_rate"] == pytest.approx(0.6667, abs=0.001)
    assert d["cost_per_task"] is not None
