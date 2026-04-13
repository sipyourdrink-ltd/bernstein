"""Tests for route decision tracking."""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.route_decision import (
    RouteDecision,
    RouteDecisionTracker,
    format_routing_reasons,
)


class TestRouteDecision:
    """Test RouteDecision dataclass."""

    def test_decision_creation(self) -> None:
        """Test creating a route decision."""
        decision = RouteDecision(
            task_id="task-123",
            adapter="claude",
            model="sonnet",
            effort="high",
            reasons=["complexity=high → sonnet"],
        )

        assert decision.task_id == "task-123"
        assert decision.adapter == "claude"
        assert decision.model == "sonnet"
        assert len(decision.reasons) == 1

    def test_decision_to_dict(self) -> None:
        """Test serializing decision."""
        decision = RouteDecision(
            task_id="task-123",
            adapter="claude",
            model="sonnet",
            effort="high",
            reasons=["reason1"],
            timestamp=1234567890.0,
        )

        data = decision.to_dict()

        assert data["task_id"] == "task-123"
        assert data["reasons"] == ["reason1"]
        assert data["timestamp"] == pytest.approx(1234567890.0)

    def test_decision_from_dict(self) -> None:
        """Test deserializing decision."""
        data = {
            "task_id": "task-456",
            "adapter": "codex",
            "model": "gpt-4",
            "effort": "max",
            "reasons": ["priority=critical"],
            "timestamp": 9876543210.0,
        }

        decision = RouteDecision.from_dict(data)

        assert decision.task_id == "task-456"
        assert decision.adapter == "codex"
        assert decision.effort == "max"


class TestRouteDecisionTracker:
    """Test RouteDecisionTracker class."""

    def test_tracker_creation(self, tmp_path: Path) -> None:
        """Test tracker initializes correctly."""
        tracker = RouteDecisionTracker(tmp_path)

        assert tracker._metrics_dir.exists()
        assert tracker._decisions == []

    def test_record_decision(self, tmp_path: Path) -> None:
        """Test recording a decision."""
        tracker = RouteDecisionTracker(tmp_path)

        decision = RouteDecision(
            task_id="task-123",
            adapter="claude",
            model="sonnet",
            effort="high",
            reasons=["test reason"],
        )
        tracker.record(decision)

        assert len(tracker._decisions) == 1
        assert tracker._filepath.exists()

    def test_get_decision(self, tmp_path: Path) -> None:
        """Test retrieving a decision."""
        tracker = RouteDecisionTracker(tmp_path)

        decision = RouteDecision(
            task_id="task-123",
            adapter="claude",
            model="sonnet",
            effort="high",
            reasons=["test"],
        )
        tracker.record(decision)

        retrieved = tracker.get_decision("task-123")

        assert retrieved is not None
        assert retrieved.task_id == "task-123"

    def test_get_decision_not_found(self, tmp_path: Path) -> None:
        """Test retrieving non-existent decision."""
        tracker = RouteDecisionTracker(tmp_path)

        retrieved = tracker.get_decision("nonexistent")

        assert retrieved is None

    def test_get_all_decisions(self, tmp_path: Path) -> None:
        """Test retrieving all decisions."""
        tracker = RouteDecisionTracker(tmp_path)

        for i in range(5):
            decision = RouteDecision(
                task_id=f"task-{i}",
                adapter="claude",
                model="sonnet",
                effort="high",
                reasons=[f"reason {i}"],
            )
            tracker.record(decision)

        all_decisions = tracker.get_all_decisions()

        assert len(all_decisions) == 5

    def test_get_all_decisions_limit(self, tmp_path: Path) -> None:
        """Test retrieving decisions with limit."""
        tracker = RouteDecisionTracker(tmp_path)

        for i in range(10):
            decision = RouteDecision(
                task_id=f"task-{i}",
                adapter="claude",
                model="sonnet",
                effort="high",
                reasons=[f"reason {i}"],
            )
            tracker.record(decision)

        limited = tracker.get_all_decisions(limit=5)

        assert len(limited) == 5

    def test_load_from_file(self, tmp_path: Path) -> None:
        """Test loading decisions from file."""
        tracker = RouteDecisionTracker(tmp_path)

        # Record some decisions
        for i in range(3):
            decision = RouteDecision(
                task_id=f"task-{i}",
                adapter="claude",
                model="sonnet",
                effort="high",
                reasons=[f"reason {i}"],
            )
            tracker.record(decision)

        # Create new tracker and load
        tracker2 = RouteDecisionTracker(tmp_path)
        count = tracker2.load_from_file()

        assert count == 3
        assert len(tracker2._decisions) == 3

    def test_load_from_file_empty(self, tmp_path: Path) -> None:
        """Test loading from non-existent file."""
        tracker = RouteDecisionTracker(tmp_path)

        count = tracker.load_from_file()

        assert count == 0


class TestFormatRoutingReasons:
    """Test format_routing_reasons function."""

    def test_high_complexity(self) -> None:
        """Test high complexity reasoning."""
        reasons = format_routing_reasons(
            task_id="task-1",
            adapter="claude",
            model="opus",
            effort="max",
            complexity="high",
            role="backend",
            priority=2,
        )

        assert any("complexity=high" in r for r in reasons)

    def test_low_complexity(self) -> None:
        """Test low complexity reasoning."""
        reasons = format_routing_reasons(
            task_id="task-1",
            adapter="claude",
            model="haiku",
            effort="low",
            complexity="low",
            role="backend",
            priority=2,
        )

        assert any("complexity=low" in r for r in reasons)

    def test_security_role(self) -> None:
        """Test security role reasoning."""
        reasons = format_routing_reasons(
            task_id="task-1",
            adapter="claude",
            model="opus",
            effort="max",
            complexity="medium",
            role="security",
            priority=2,
        )

        assert any("role=security" in r for r in reasons)

    def test_critical_priority(self) -> None:
        """Test critical priority reasoning."""
        reasons = format_routing_reasons(
            task_id="task-1",
            adapter="claude",
            model="opus",
            effort="max",
            complexity="medium",
            role="backend",
            priority=1,
        )

        assert any("priority=critical" in r for r in reasons)

    def test_skill_profile(self) -> None:
        """Test skill profile reasoning."""
        reasons = format_routing_reasons(
            task_id="task-1",
            adapter="claude",
            model="sonnet",
            effort="high",
            complexity="medium",
            role="backend",
            priority=2,
            skill_profile_success_rate=92.5,
        )

        assert any("92%" in r for r in reasons)

    def test_effort_reasoning(self) -> None:
        """Test effort level reasoning."""
        reasons = format_routing_reasons(
            task_id="task-1",
            adapter="claude",
            model="sonnet",
            effort="max",
            complexity="medium",
            role="backend",
            priority=2,
        )

        assert any("effort=max" in r for r in reasons)

    def test_multiple_reasons(self) -> None:
        """Test multiple reasons are generated."""
        reasons = format_routing_reasons(
            task_id="task-1",
            adapter="claude",
            model="opus",
            effort="max",
            complexity="high",
            role="security",
            priority=1,
            skill_profile_success_rate=95.0,
        )

        # Should have multiple reasons
        assert len(reasons) >= 3
