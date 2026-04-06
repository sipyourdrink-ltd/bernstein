"""Tests for cost anomaly root cause analysis (COST-011)."""

from __future__ import annotations

import pytest

from bernstein.core.cost_root_cause import (
    CostContributor,
    RootCauseReport,
    analyse_cost_overshoot,
)
from bernstein.core.cost_tracker import CostTracker


def test_no_overshoot_returns_none() -> None:
    """When actual cost <= estimate, returns None."""
    tracker = CostTracker(run_id="r1")
    tracker.record("a1", "t1", "sonnet", 1000, 500)
    # Estimate higher than actual
    result = analyse_cost_overshoot(tracker, estimated_cost_usd=100.0)
    assert result is None


def test_overshoot_detected() -> None:
    """When actual cost > estimate, returns a RootCauseReport."""
    tracker = CostTracker(run_id="r1")
    tracker.record("a1", "t1", "sonnet", 100000, 50000)  # some real cost
    actual = tracker.spent_usd
    # Set estimate lower than actual
    estimate = actual * 0.5

    result = analyse_cost_overshoot(tracker, estimated_cost_usd=estimate)
    assert result is not None
    assert isinstance(result, RootCauseReport)
    assert result.overshoot_usd > 0
    assert result.overshoot_pct > 0


def test_top_contributors_populated() -> None:
    """Top contributors are identified from usage data."""
    tracker = CostTracker(run_id="r1")
    # Agent a1 is expensive
    tracker.record("a1", "t1", "opus", 100000, 50000)
    # Agent a2 is cheap
    tracker.record("a2", "t2", "haiku", 1000, 500)

    actual = tracker.spent_usd
    estimate = actual * 0.3

    result = analyse_cost_overshoot(tracker, estimated_cost_usd=estimate)
    assert result is not None
    assert len(result.top_contributors) > 0
    # Most expensive contributor should be first
    assert result.top_contributors[0].cost_usd >= result.top_contributors[-1].cost_usd


def test_both_agent_and_task_contributors() -> None:
    """Contributors include both agent and task entities."""
    tracker = CostTracker(run_id="r1")
    tracker.record("a1", "t1", "sonnet", 50000, 25000)
    tracker.record("a2", "t2", "sonnet", 50000, 25000)

    actual = tracker.spent_usd
    estimate = actual * 0.1

    result = analyse_cost_overshoot(tracker, estimated_cost_usd=estimate)
    assert result is not None
    entity_types = {c.entity_type for c in result.top_contributors}
    assert "agent" in entity_types
    assert "task" in entity_types


def test_summary_is_readable() -> None:
    """Summary contains run ID and cost numbers."""
    tracker = CostTracker(run_id="my-run")
    tracker.record("a1", "t1", "sonnet", 100000, 50000)
    actual = tracker.spent_usd
    estimate = actual * 0.5

    result = analyse_cost_overshoot(tracker, estimated_cost_usd=estimate)
    assert result is not None
    assert "my-run" in result.summary
    assert "$" in result.summary


def test_top_n_limits_contributors() -> None:
    """top_n parameter limits the number of contributors."""
    tracker = CostTracker(run_id="r1")
    for i in range(10):
        tracker.record(f"a{i}", f"t{i}", "sonnet", 10000, 5000)

    actual = tracker.spent_usd
    estimate = actual * 0.1

    result = analyse_cost_overshoot(tracker, estimated_cost_usd=estimate, top_n=3)
    assert result is not None
    assert len(result.top_contributors) <= 3


def test_report_to_dict() -> None:
    """RootCauseReport.to_dict has expected keys."""
    tracker = CostTracker(run_id="r1")
    tracker.record("a1", "t1", "sonnet", 100000, 50000)
    actual = tracker.spent_usd

    result = analyse_cost_overshoot(tracker, estimated_cost_usd=actual * 0.5)
    assert result is not None
    d = result.to_dict()
    assert "run_id" in d
    assert "overshoot_usd" in d
    assert "top_contributors" in d
    assert "summary" in d


def test_contributor_to_dict() -> None:
    """CostContributor.to_dict has expected keys."""
    c = CostContributor(
        entity_type="agent",
        entity_id="a1",
        cost_usd=0.50,
        share_pct=75.0,
        reason="5 invocations, expensive model",
    )
    d = c.to_dict()
    assert d["entity_type"] == "agent"
    assert d["share_pct"] == pytest.approx(75.0)
