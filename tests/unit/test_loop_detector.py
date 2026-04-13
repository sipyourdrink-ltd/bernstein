"""Unit tests for LoopDetector — loop and deadlock detection."""

from __future__ import annotations

import dataclasses
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bernstein.core.loop_detector import (
    LOOP_EDIT_THRESHOLD,
    LOOP_WINDOW_SECONDS,
    EditEvent,
    LoopDetector,
    _find_cycles,
    _oldest_lock_holder,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lock_mgr(locks: list[dict]) -> MagicMock:
    """Build a mock FileLockManager with pre-configured locks."""
    from bernstein.core.file_locks import FileLock

    mgr = MagicMock()
    mgr.all_locks.return_value = [
        FileLock(
            file_path=lock["file_path"],
            agent_id=lock["agent_id"],
            task_id=lock.get("task_id", "t-1"),
            task_title=lock.get("task_title", ""),
            locked_at=lock.get("locked_at", time.time()),
        )
        for lock in locks
    ]
    return mgr


# ---------------------------------------------------------------------------
# EditEvent construction
# ---------------------------------------------------------------------------


def test_edit_event_is_frozen() -> None:
    e = EditEvent(agent_id="a1", file_path="src/foo.py", timestamp=1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.agent_id = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# record_edit / detect_loops
# ---------------------------------------------------------------------------


class TestDetectLoops:
    def test_no_edits_returns_empty(self) -> None:
        detector = LoopDetector()
        assert detector.detect_loops() == []

    def test_below_threshold_not_flagged(self) -> None:
        detector = LoopDetector()
        now = time.time()
        for i in range(LOOP_EDIT_THRESHOLD):  # exactly at threshold, NOT over
            detector.record_edit("a1", "src/foo.py", now - i)
        assert detector.detect_loops() == []

    def test_above_threshold_flagged(self) -> None:
        detector = LoopDetector()
        now = time.time()
        for i in range(LOOP_EDIT_THRESHOLD + 1):
            detector.record_edit("a1", "src/foo.py", now - i)
        loops = detector.detect_loops()
        assert len(loops) == 1
        assert loops[0].agent_id == "a1"
        assert loops[0].file_path == "src/foo.py"
        assert loops[0].edit_count == LOOP_EDIT_THRESHOLD + 1

    def test_different_files_tracked_independently(self) -> None:
        detector = LoopDetector()
        now = time.time()
        # 4 edits to foo.py → loop
        for i in range(4):
            detector.record_edit("a1", "src/foo.py", now - i)
        # 2 edits to bar.py → no loop
        for i in range(2):
            detector.record_edit("a1", "src/bar.py", now - i)
        loops = detector.detect_loops()
        assert len(loops) == 1
        assert loops[0].file_path == "src/foo.py"

    def test_different_agents_tracked_independently(self) -> None:
        detector = LoopDetector()
        now = time.time()
        for i in range(4):
            detector.record_edit("a1", "src/foo.py", now - i)
            detector.record_edit("a2", "src/foo.py", now - i)
        loops = detector.detect_loops()
        agent_ids = {loop.agent_id for loop in loops}
        assert agent_ids == {"a1", "a2"}

    def test_stale_events_outside_window_pruned(self) -> None:
        detector = LoopDetector()
        old_ts = time.time() - LOOP_WINDOW_SECONDS - 10
        for i in range(4):
            detector.record_edit("a1", "src/foo.py", old_ts + i)
        loops = detector.detect_loops()
        assert loops == []

    def test_window_boundary_respected(self) -> None:
        detector = LoopDetector()
        now = time.time()
        # 3 edits just inside window, 1 just outside
        for i in range(3):
            detector.record_edit("a1", "src/foo.py", now - i)
        detector.record_edit("a1", "src/foo.py", now - LOOP_WINDOW_SECONDS - 1)
        loops = detector.detect_loops()
        assert loops == []  # only 3 inside window → not over threshold of 3

    def test_loop_detection_returns_window_seconds(self) -> None:
        detector = LoopDetector()
        now = time.time()
        for i in range(4):
            detector.record_edit("a1", "src/foo.py", now - i)
        loops = detector.detect_loops(window_seconds=300.0)
        assert loops[0].window_seconds == pytest.approx(300.0)

    def test_custom_threshold(self) -> None:
        detector = LoopDetector()
        now = time.time()
        for i in range(2):
            detector.record_edit("a1", "src/foo.py", now - i)
        loops_default = detector.detect_loops(threshold=LOOP_EDIT_THRESHOLD)
        loops_strict = detector.detect_loops(threshold=1)
        assert loops_default == []
        assert len(loops_strict) == 1


# ---------------------------------------------------------------------------
# record_lock_wait / clear_wait / detect_deadlocks
# ---------------------------------------------------------------------------


class TestDetectDeadlocks:
    def test_no_waits_returns_empty(self) -> None:
        detector = LoopDetector()
        mgr = _make_lock_mgr([])
        assert detector.detect_deadlocks(mgr) == []

    def test_one_way_wait_not_a_deadlock(self) -> None:
        """A → B wait without B → A does not form a cycle."""
        detector = LoopDetector()
        detector.record_lock_wait(
            waiting_agent_id="a1",
            wanted_files=["src/foo.py"],
            held_by={"src/foo.py": "a2"},
        )
        mgr = _make_lock_mgr(
            [
                {"file_path": "src/foo.py", "agent_id": "a2", "locked_at": 1000.0},
            ]
        )
        assert detector.detect_deadlocks(mgr) == []

    def test_two_way_deadlock_detected(self) -> None:
        """A waits for file held by B; B waits for file held by A → deadlock."""
        detector = LoopDetector()
        now = time.time()
        detector.record_lock_wait(
            waiting_agent_id="a1",
            wanted_files=["src/bar.py"],
            held_by={"src/bar.py": "a2"},
            lock_timestamps={"a2": now - 10},
        )
        detector.record_lock_wait(
            waiting_agent_id="a2",
            wanted_files=["src/foo.py"],
            held_by={"src/foo.py": "a1"},
            lock_timestamps={"a1": now - 5},
        )
        mgr = _make_lock_mgr(
            [
                {"file_path": "src/foo.py", "agent_id": "a1", "locked_at": now - 5},
                {"file_path": "src/bar.py", "agent_id": "a2", "locked_at": now - 10},
            ]
        )
        deadlocks = detector.detect_deadlocks(mgr)
        assert len(deadlocks) == 1
        dl = deadlocks[0]
        assert set(dl.agents) == {"a1", "a2"}
        assert "Deadlock:" in dl.description

    def test_victim_is_older_lock_holder(self) -> None:
        """The agent with the older lock is chosen as victim."""
        detector = LoopDetector()
        now = time.time()
        detector.record_lock_wait(
            waiting_agent_id="a1",
            wanted_files=["src/bar.py"],
            held_by={"src/bar.py": "a2"},
        )
        detector.record_lock_wait(
            waiting_agent_id="a2",
            wanted_files=["src/foo.py"],
            held_by={"src/foo.py": "a1"},
        )
        mgr = _make_lock_mgr(
            [
                {"file_path": "src/foo.py", "agent_id": "a1", "locked_at": now - 100},  # older
                {"file_path": "src/bar.py", "agent_id": "a2", "locked_at": now - 50},
            ]
        )
        deadlocks = detector.detect_deadlocks(mgr)
        assert len(deadlocks) == 1
        assert deadlocks[0].victim_agent_id == "a1"

    def test_clear_wait_removes_deadlock(self) -> None:
        detector = LoopDetector()
        now = time.time()
        detector.record_lock_wait("a1", ["src/bar.py"], {"src/bar.py": "a2"})
        detector.record_lock_wait("a2", ["src/foo.py"], {"src/foo.py": "a1"})
        mgr = _make_lock_mgr(
            [
                {"file_path": "src/foo.py", "agent_id": "a1", "locked_at": now},
                {"file_path": "src/bar.py", "agent_id": "a2", "locked_at": now},
            ]
        )
        assert len(detector.detect_deadlocks(mgr)) == 1
        detector.clear_wait("a1")
        assert detector.detect_deadlocks(mgr) == []

    def test_no_self_wait_recorded(self) -> None:
        """An agent cannot be blocked by its own lock."""
        detector = LoopDetector()
        detector.record_lock_wait(
            waiting_agent_id="a1",
            wanted_files=["src/foo.py"],
            held_by={"src/foo.py": "a1"},
        )
        assert not detector._wait_for.get("a1")

    def test_duplicate_cycle_not_returned_twice(self) -> None:
        detector = LoopDetector()
        now = time.time()
        detector.record_lock_wait("a1", ["src/bar.py"], {"src/bar.py": "a2"})
        detector.record_lock_wait("a2", ["src/foo.py"], {"src/foo.py": "a1"})
        mgr = _make_lock_mgr(
            [
                {"file_path": "src/foo.py", "agent_id": "a1", "locked_at": now},
                {"file_path": "src/bar.py", "agent_id": "a2", "locked_at": now},
            ]
        )
        deadlocks = detector.detect_deadlocks(mgr)
        # One cycle {a1, a2} regardless of traversal order
        assert len(deadlocks) == 1


# ---------------------------------------------------------------------------
# _find_cycles
# ---------------------------------------------------------------------------


class TestFindCycles:
    def test_empty_graph(self) -> None:
        assert _find_cycles({}) == []

    def test_no_cycle(self) -> None:
        graph: dict[str, set[str]] = {"a": {"b"}, "b": {"c"}}
        assert _find_cycles(graph) == []

    def test_self_loop_ignored(self) -> None:
        # Self-loop (a→a) requires path length > 1 to trigger; ignored.
        graph: dict[str, set[str]] = {"a": {"a"}}
        cycles = _find_cycles(graph)
        # A self-loop does NOT count (path [a] has length 1)
        assert cycles == []

    def test_simple_two_node_cycle(self) -> None:
        graph: dict[str, set[str]] = {"a": {"b"}, "b": {"a"}}
        cycles = _find_cycles(graph)
        assert len(cycles) == 1
        assert set(cycles[0]) == {"a", "b"}

    def test_three_node_cycle(self) -> None:
        graph: dict[str, set[str]] = {"a": {"b"}, "b": {"c"}, "c": {"a"}}
        cycles = _find_cycles(graph)
        assert any(set(c) == {"a", "b", "c"} for c in cycles)


# ---------------------------------------------------------------------------
# _oldest_lock_holder
# ---------------------------------------------------------------------------


class TestOldestLockHolder:
    def test_returns_oldest(self) -> None:
        held = {
            "src/foo.py": ("a1", 1000.0),  # older
            "src/bar.py": ("a2", 2000.0),
        }
        assert _oldest_lock_holder(["a1", "a2"], held) == "a1"

    def test_fallback_when_no_timestamps(self) -> None:
        assert _oldest_lock_holder(["a1", "a2"], {}) == "a1"

    def test_agent_not_in_held_is_skipped(self) -> None:
        held = {"src/foo.py": ("a3", 500.0)}  # a3 not in cycle
        assert _oldest_lock_holder(["a1", "a2"], held) == "a1"


# ---------------------------------------------------------------------------
# Integration: check_loops_and_deadlocks via orchestrator mock
# ---------------------------------------------------------------------------


class TestCheckLoopsAndDeadlocks:
    def test_noop_when_no_detector(self) -> None:
        """No error when _loop_detector is absent."""
        from bernstein.core.agent_lifecycle import check_loops_and_deadlocks

        orch = SimpleNamespace()
        # No _loop_detector attribute → function must not raise
        check_loops_and_deadlocks(orch)

    def test_kills_looping_agent(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from bernstein.core.agent_lifecycle import check_loops_and_deadlocks
        from bernstein.core.models import AgentSession, ModelConfig

        detector = LoopDetector()
        session = AgentSession(
            id="loop-agent",
            role="backend",
            provider="claude",
            model_config=ModelConfig("sonnet", "high"),
            task_ids=["t-1"],
        )

        spawner = MagicMock()
        lock_mgr = MagicMock()
        lock_mgr.all_locks.return_value = []

        orch = SimpleNamespace(
            _loop_detector=detector,
            _lock_manager=lock_mgr,
            _workdir=tmp_path,
            _agents={"loop-agent": session},
            _spawner=spawner,
        )

        # Inject 4 edit events to trigger the loop
        now = time.time()
        for i in range(LOOP_EDIT_THRESHOLD + 1):
            detector.record_edit("loop-agent", "src/foo.py", now - i)

        check_loops_and_deadlocks(orch)

        spawner.kill.assert_called_once_with(session)
        lock_mgr.release.assert_called_with("loop-agent")

    def test_breaks_deadlock(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from bernstein.core.agent_lifecycle import check_loops_and_deadlocks
        from bernstein.core.file_locks import FileLock

        now = time.time()
        detector = LoopDetector()
        detector.record_lock_wait("a1", ["src/bar.py"], {"src/bar.py": "a2"})
        detector.record_lock_wait("a2", ["src/foo.py"], {"src/foo.py": "a1"})

        lock_mgr = MagicMock()
        lock_mgr.all_locks.return_value = [
            FileLock("src/foo.py", "a1", "t1", "", now - 100),  # a1 older
            FileLock("src/bar.py", "a2", "t2", "", now - 50),
        ]

        orch = SimpleNamespace(
            _loop_detector=detector,
            _lock_manager=lock_mgr,
            _workdir=tmp_path,
            _agents={},
            _spawner=MagicMock(),
        )

        check_loops_and_deadlocks(orch)

        # Victim (older holder a1) should have its lock released
        lock_mgr.release.assert_any_call("a1")
