"""Tests for the nudge manager extracted from orchestrator.py (ORCH-009)."""

from __future__ import annotations

import time

from bernstein.core.nudge_manager import (
    OrchestratorNudge,
    OrchestratorNudgeManager,
    get_orchestrator_nudges,
    nudge_orchestrator,
)


class TestOrchestratorNudge:
    def test_dataclass_defaults(self) -> None:
        nudge = OrchestratorNudge(nudge_type="scale_up", message="need more agents")
        assert nudge.nudge_type == "scale_up"
        assert nudge.message == "need more agents"
        assert nudge.priority == 1
        assert nudge.acknowledged is False
        assert isinstance(nudge.metadata, dict)
        assert nudge.timestamp > 0

    def test_custom_fields(self) -> None:
        nudge = OrchestratorNudge(
            nudge_type="reprioritize",
            message="bump priority",
            priority=3,
            metadata={"task_id": "T-001"},
        )
        assert nudge.priority == 3
        assert nudge.metadata["task_id"] == "T-001"


class TestOrchestratorNudgeManager:
    def test_add_and_get_nudges(self) -> None:
        mgr = OrchestratorNudgeManager()
        mgr.add_nudge("scale_up", "need more agents", priority=2)
        mgr.add_nudge("scale_down", "too many agents", priority=1)

        all_nudges = mgr.get_pending_nudges()
        assert len(all_nudges) == 2

    def test_priority_threshold(self) -> None:
        mgr = OrchestratorNudgeManager()
        mgr.add_nudge("low", "low priority", priority=1)
        mgr.add_nudge("high", "high priority", priority=3)

        high = mgr.get_pending_nudges(priority_threshold=3)
        assert len(high) == 1
        assert high[0].nudge_type == "high"

    def test_acknowledge_nudge(self) -> None:
        mgr = OrchestratorNudgeManager()
        nudge = mgr.add_nudge("test", "test nudge")
        assert len(mgr.get_pending_nudges()) == 1

        mgr.acknowledge_nudge(nudge)
        assert len(mgr.get_pending_nudges()) == 0

    def test_clear_acknowledged(self) -> None:
        mgr = OrchestratorNudgeManager()
        n1 = mgr.add_nudge("a", "first")
        mgr.add_nudge("b", "second")

        mgr.acknowledge_nudge(n1)
        assert len(mgr.nudges) == 2

        mgr.clear_acknowledged()
        assert len(mgr.nudges) == 1
        assert mgr.nudges[0].nudge_type == "b"

    def test_empty_manager(self) -> None:
        mgr = OrchestratorNudgeManager()
        assert mgr.get_pending_nudges() == []

    def test_metadata_preserved(self) -> None:
        mgr = OrchestratorNudgeManager()
        nudge = mgr.add_nudge("test", "test", metadata={"key": "value"})
        assert nudge.metadata == {"key": "value"}

    def test_none_metadata_becomes_empty_dict(self) -> None:
        mgr = OrchestratorNudgeManager()
        nudge = mgr.add_nudge("test", "test", metadata=None)
        assert nudge.metadata == {}


class TestModuleLevelFunctions:
    def test_nudge_orchestrator(self) -> None:
        nudge = nudge_orchestrator("test_type", "test message", priority=2)
        assert nudge.nudge_type == "test_type"
        assert nudge.priority == 2

    def test_get_orchestrator_nudges(self) -> None:
        # Module-level singleton already has nudges from prior test
        all_nudges = get_orchestrator_nudges()
        assert isinstance(all_nudges, list)
