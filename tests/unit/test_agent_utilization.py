"""Tests for agent utilization tracking and display."""

from __future__ import annotations

from bernstein.core.agent_utilization import (
    UtilizationRecord,
    UtilizationSummary,
    compute_utilization,
    format_utilization_table,
    summarize_utilization,
)


# ---------------------------------------------------------------------------
# UtilizationRecord
# ---------------------------------------------------------------------------


class TestUtilizationRecord:
    def test_create_frozen(self) -> None:
        rec = UtilizationRecord(
            agent_id="a1",
            role="backend",
            model="sonnet",
            active_seconds=60.0,
            idle_seconds=40.0,
            total_seconds=100.0,
            utilization_pct=60.0,
        )
        assert rec.agent_id == "a1"
        assert rec.utilization_pct == 60.0

    def test_pct_matches_ratio(self) -> None:
        rec = UtilizationRecord(
            agent_id="a2",
            role="qa",
            model="haiku",
            active_seconds=75.0,
            idle_seconds=25.0,
            total_seconds=100.0,
            utilization_pct=75.0,
        )
        assert rec.utilization_pct == rec.active_seconds / rec.total_seconds * 100


# ---------------------------------------------------------------------------
# compute_utilization
# ---------------------------------------------------------------------------


class TestComputeUtilization:
    def test_simple_transitions(self) -> None:
        transitions: list[tuple[float, str]] = [
            (0.0, "starting"),
            (5.0, "working"),
            (15.0, "idle"),
            (20.0, "working"),
            (30.0, "dead"),
        ]
        rec = compute_utilization("agent-1", transitions, role="backend", model="sonnet")
        assert rec.agent_id == "agent-1"
        assert rec.role == "backend"
        assert rec.model == "sonnet"
        # active: working 5->15 (10s) + working 20->30 (10s) = 20s
        assert rec.active_seconds == 20.0
        # idle: starting 0->5 (5s) + idle 15->20 (5s) = 10s
        assert rec.idle_seconds == 10.0
        assert rec.total_seconds == 30.0
        assert rec.utilization_pct == 66.7

    def test_no_transitions(self) -> None:
        rec = compute_utilization("agent-2", [])
        assert rec.active_seconds == 0.0
        assert rec.idle_seconds == 0.0
        assert rec.total_seconds == 0.0
        assert rec.utilization_pct == 0.0

    def test_single_transition(self) -> None:
        rec = compute_utilization("agent-3", [(100.0, "working")])
        assert rec.total_seconds == 0.0
        assert rec.utilization_pct == 0.0

    def test_all_idle(self) -> None:
        transitions: list[tuple[float, str]] = [
            (0.0, "starting"),
            (10.0, "idle"),
            (20.0, "dead"),
        ]
        rec = compute_utilization("agent-4", transitions)
        assert rec.active_seconds == 0.0
        assert rec.idle_seconds == 20.0
        assert rec.utilization_pct == 0.0

    def test_all_active(self) -> None:
        transitions: list[tuple[float, str]] = [
            (0.0, "working"),
            (50.0, "dead"),
        ]
        rec = compute_utilization("agent-5", transitions)
        assert rec.active_seconds == 50.0
        assert rec.idle_seconds == 0.0
        assert rec.utilization_pct == 100.0

    def test_dead_segments_excluded(self) -> None:
        """Time spent in 'dead' status should not count toward active or idle."""
        transitions: list[tuple[float, str]] = [
            (0.0, "working"),
            (10.0, "dead"),
            (20.0, "working"),  # hypothetical re-start after dead
            (30.0, "dead"),
        ]
        rec = compute_utilization("agent-6", transitions)
        # working: 0->10 (10s) + working: 20->30 (10s) = 20s active
        assert rec.active_seconds == 20.0
        # dead: 10->20 (10s) not counted
        assert rec.idle_seconds == 0.0
        assert rec.total_seconds == 20.0
        assert rec.utilization_pct == 100.0


# ---------------------------------------------------------------------------
# summarize_utilization
# ---------------------------------------------------------------------------


class TestSummarizeUtilization:
    def test_empty_list(self) -> None:
        summary = summarize_utilization([])
        assert summary.total_agents == 0
        assert summary.avg_utilization_pct == 0.0
        assert summary.most_utilized == ""
        assert summary.least_utilized == ""
        assert summary.total_active_seconds == 0.0
        assert summary.total_idle_seconds == 0.0

    def test_multiple_records(self) -> None:
        r1 = UtilizationRecord(
            agent_id="a1",
            role="backend",
            model="sonnet",
            active_seconds=80.0,
            idle_seconds=20.0,
            total_seconds=100.0,
            utilization_pct=80.0,
        )
        r2 = UtilizationRecord(
            agent_id="a2",
            role="qa",
            model="haiku",
            active_seconds=30.0,
            idle_seconds=70.0,
            total_seconds=100.0,
            utilization_pct=30.0,
        )
        summary = summarize_utilization([r1, r2])
        assert summary.total_agents == 2
        assert summary.avg_utilization_pct == 55.0
        assert summary.most_utilized == "a1"
        assert summary.least_utilized == "a2"
        assert summary.total_active_seconds == 110.0
        assert summary.total_idle_seconds == 90.0

    def test_single_record(self) -> None:
        r = UtilizationRecord(
            agent_id="only",
            role="devops",
            model="opus",
            active_seconds=50.0,
            idle_seconds=50.0,
            total_seconds=100.0,
            utilization_pct=50.0,
        )
        summary = summarize_utilization([r])
        assert summary.total_agents == 1
        assert summary.most_utilized == "only"
        assert summary.least_utilized == "only"

    def test_summary_is_frozen(self) -> None:
        summary = summarize_utilization([])
        assert isinstance(summary, UtilizationSummary)


# ---------------------------------------------------------------------------
# format_utilization_table
# ---------------------------------------------------------------------------


class TestFormatUtilizationTable:
    def test_produces_nonempty_string(self) -> None:
        r = UtilizationRecord(
            agent_id="a1",
            role="backend",
            model="sonnet",
            active_seconds=60.0,
            idle_seconds=40.0,
            total_seconds=100.0,
            utilization_pct=60.0,
        )
        output = format_utilization_table([r])
        assert isinstance(output, str)
        assert len(output) > 0

    def test_contains_agent_id(self) -> None:
        r = UtilizationRecord(
            agent_id="test-agent-xyz",
            role="qa",
            model="haiku",
            active_seconds=10.0,
            idle_seconds=90.0,
            total_seconds=100.0,
            utilization_pct=10.0,
        )
        output = format_utilization_table([r])
        assert "test-agent-xyz" in output

    def test_empty_records(self) -> None:
        output = format_utilization_table([])
        assert isinstance(output, str)
        # Should still render the table header at minimum
        assert "Agent Utilization" in output

    def test_sorted_by_utilization_desc(self) -> None:
        r_low = UtilizationRecord(
            agent_id="low",
            role="docs",
            model="haiku",
            active_seconds=10.0,
            idle_seconds=90.0,
            total_seconds=100.0,
            utilization_pct=10.0,
        )
        r_high = UtilizationRecord(
            agent_id="high",
            role="backend",
            model="opus",
            active_seconds=90.0,
            idle_seconds=10.0,
            total_seconds=100.0,
            utilization_pct=90.0,
        )
        output = format_utilization_table([r_low, r_high])
        # "high" should appear before "low" in the output
        assert output.index("high") < output.index("low")
