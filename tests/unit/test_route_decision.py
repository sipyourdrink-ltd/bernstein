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

    def test_decision_with_model_metadata(self) -> None:
        """Test decision includes model_reported and model_version fields."""
        decision = RouteDecision(
            task_id="task-123",
            adapter="anthropic",
            model="claude-3-5-sonnet",
            effort="high",
            reasons=["complexity=high"],
            timestamp=1234567890.0,
            model_reported="claude-3-5-sonnet-20241022",
            model_version="claude-3.5-sonnet-20241022",
            routing_decision_hash="sha256:" + "a" * 64,
        )

        data = decision.to_dict()

        assert data["model_reported"] == "claude-3-5-sonnet-20241022"
        assert data["model_version"] == "claude-3.5-sonnet-20241022"
        assert data["routing_decision_hash"].startswith("sha256:")

    def test_decision_from_dict_with_model_metadata(self) -> None:
        """Test deserializing decision with all model metadata fields."""
        data = {
            "task_id": "task-789",
            "adapter": "openai",
            "model": "gpt-4o",
            "effort": "max",
            "reasons": ["priority=critical"],
            "timestamp": 1111111111.0,
            "model_reported": "gpt-4o-2024-08-06",
            "model_version": "2024-08-06",
            "routing_decision_hash": "sha256:" + "b" * 64,
        }

        decision = RouteDecision.from_dict(data)

        assert decision.task_id == "task-789"
        assert decision.model_reported == "gpt-4o-2024-08-06"
        assert decision.model_version == "2024-08-06"
        assert decision.routing_decision_hash == "sha256:" + "b" * 64

    def test_from_response_computes_hash(self) -> None:
        """Test from_response computes routing_decision_hash."""
        decision = RouteDecision.from_response(
            task_id="task-resp",
            adapter="anthropic",
            model_requested="claude-3-opus",
            model_reported="claude-3-opus-20240229",
            model_version="opus-20240229",
            reasons=["complexity=high"],
            effort="max",
            timestamp=1234567890.0,
        )

        assert decision.task_id == "task-resp"
        assert decision.model == "claude-3-opus"
        assert decision.model_reported == "claude-3-opus-20240229"
        assert decision.model_version == "opus-20240229"
        assert decision.routing_decision_hash is not None
        assert decision.routing_decision_hash.startswith("sha256:")
        assert len(decision.routing_decision_hash) == 71  # sha256: + 64 hex chars

    def test_from_response_none_metadata(self) -> None:
        """Test from_response handles None model_reported and model_version."""
        decision = RouteDecision.from_response(
            task_id="task-none",
            adapter="local",
            model_requested="llama-3",
            model_reported=None,
            model_version=None,
            reasons=["low complexity"],
            effort="low",
            timestamp=1000000000.0,
        )

        assert decision.model_reported is None
        assert decision.model_version is None
        assert decision.routing_decision_hash is not None

    def test_routing_decision_hash_deterministic(self) -> None:
        """Test that routing_decision_hash is deterministic for same inputs."""
        d1 = RouteDecision.from_response(
            task_id="task-det",
            adapter="anthropic",
            model_requested="claude-3-sonnet",
            model_reported="claude-3-sonnet-20240620",
            model_version="20240620",
            reasons=["test reason"],
            effort="high",
            timestamp=1000000000.0,
        )
        d2 = RouteDecision.from_response(
            task_id="task-det",
            adapter="anthropic",
            model_requested="claude-3-sonnet",
            model_reported="claude-3-sonnet-20240620",
            model_version="20240620",
            reasons=["test reason"],
            effort="high",
            timestamp=1000000000.0,
        )

        assert d1.routing_decision_hash == d2.routing_decision_hash

    def test_routing_decision_hash_differs_for_different_inputs(self) -> None:
        """Test that routing_decision_hash differs for different task_id."""
        d1 = RouteDecision.from_response(
            task_id="task-a",
            adapter="anthropic",
            model_requested="claude-3-sonnet",
            model_reported="claude-3-sonnet-20240620",
            model_version="20240620",
            reasons=["test reason"],
            effort="high",
            timestamp=1000000000.0,
        )
        d2 = RouteDecision.from_response(
            task_id="task-b",
            adapter="anthropic",
            model_requested="claude-3-sonnet",
            model_reported="claude-3-sonnet-20240620",
            model_version="20240620",
            reasons=["test reason"],
            effort="high",
            timestamp=1000000000.0,
        )

        assert d1.routing_decision_hash != d2.routing_decision_hash

    def test_to_dict_roundtrip_with_model_metadata(self) -> None:
        """Test to_dict/from_dict roundtrip preserves all fields."""
        original = RouteDecision(
            task_id="task-rt",
            adapter="google",
            model="gemini-2.0-flash",
            effort="high",
            reasons=["fast response"],
            timestamp=1234567890.0,
            model_reported="gemini-2.0-flash-exp",
            model_version="2.0-flash-exp-0820",
            routing_decision_hash="sha256:" + "c" * 64,
        )

        data = original.to_dict()
        restored = RouteDecision.from_dict(data)

        assert restored.task_id == original.task_id
        assert restored.adapter == original.adapter
        assert restored.model == original.model
        assert restored.effort == original.effort
        assert restored.reasons == original.reasons
        assert restored.timestamp == original.timestamp
        assert restored.model_reported == original.model_reported
        assert restored.model_version == original.model_version
        assert restored.routing_decision_hash == original.routing_decision_hash

    def test_model_reported_none_omitted_from_to_dict(self) -> None:
        """Test model_reported None is preserved in to_dict (not stripped)."""
        decision = RouteDecision(
            task_id="task-none",
            adapter="local",
            model="llama-3",
            effort="low",
            reasons=[],
            timestamp=1000000000.0,
            model_reported=None,
            model_version=None,
            routing_decision_hash=None,
        )

        data = decision.to_dict()

        assert data["model_reported"] is None
        assert data["model_version"] is None
        assert data["routing_decision_hash"] is None

    def test_from_response_defaults_timestamp(self) -> None:
        """Test from_response uses current time when timestamp is None."""
        decision = RouteDecision.from_response(
            task_id="task-ts",
            adapter="anthropic",
            model_requested="claude-3-sonnet",
            model_reported=None,
            model_version=None,
            reasons=["test"],
            effort="medium",
            timestamp=None,
        )

        assert decision.timestamp > 0
        assert decision.routing_decision_hash is not None


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
