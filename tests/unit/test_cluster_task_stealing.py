"""Tests for ENT-007: Cluster task stealing for load balancing."""

from __future__ import annotations

import time

from bernstein.core.cluster_task_stealing import (
    NodeLoad,
    StealableTask,
    StealConfig,
    StealResult,
    TaskStealingEngine,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node(node_id: str, queued: int = 0, slots: int = 4) -> NodeLoad:
    return NodeLoad(
        node_id=node_id,
        queued_tasks=queued,
        running_tasks=0,
        available_slots=slots,
        total_slots=4,
    )


def _make_task(task_id: str, node_id: str, priority: int = 5) -> StealableTask:
    return StealableTask(
        task_id=task_id,
        node_id=node_id,
        queued_at=time.time(),
        priority=priority,
    )


# ---------------------------------------------------------------------------
# find_victim
# ---------------------------------------------------------------------------


class TestFindVictim:
    def test_finds_busiest_node(self) -> None:
        engine = TaskStealingEngine(StealConfig(steal_threshold=3))
        nodes = [
            _make_node("thief", queued=0, slots=4),
            _make_node("busy-a", queued=5),
            _make_node("busy-b", queued=8),
        ]
        victim = engine.find_victim("thief", nodes)
        assert victim is not None
        assert victim.node_id == "busy-b"

    def test_skips_self(self) -> None:
        engine = TaskStealingEngine(StealConfig(steal_threshold=1))
        nodes = [_make_node("thief", queued=10)]
        assert engine.find_victim("thief", nodes) is None

    def test_skips_below_threshold(self) -> None:
        engine = TaskStealingEngine(StealConfig(steal_threshold=5))
        nodes = [
            _make_node("thief", queued=0),
            _make_node("other", queued=3),
        ]
        assert engine.find_victim("thief", nodes) is None

    def test_respects_cooldown(self) -> None:
        engine = TaskStealingEngine(StealConfig(steal_threshold=1, cooldown_s=999))
        nodes = [
            _make_node("thief", queued=0),
            _make_node("victim", queued=10),
        ]
        victim_tasks = {
            "victim": [_make_task("t1", "victim"), _make_task("t2", "victim")],
        }
        # First steal triggers cooldown
        r1 = engine.attempt_steal("thief", nodes, victim_tasks)
        assert r1.result == StealResult.SUCCESS

        # Second attempt should skip due to cooldown
        assert engine.find_victim("thief", nodes) is None


# ---------------------------------------------------------------------------
# select_tasks_to_steal
# ---------------------------------------------------------------------------


class TestSelectTasks:
    def test_selects_up_to_batch_size(self) -> None:
        engine = TaskStealingEngine(StealConfig(max_steal_batch=2))
        tasks = [
            _make_task("t1", "victim", priority=3),
            _make_task("t2", "victim", priority=1),
            _make_task("t3", "victim", priority=5),
        ]
        selected = engine.select_tasks_to_steal(tasks, "thief")
        assert len(selected) == 2
        # Highest priority (lowest number) first
        assert selected[0].task_id == "t2"
        assert selected[1].task_id == "t1"

    def test_skips_pinned_tasks(self) -> None:
        engine = TaskStealingEngine(StealConfig(max_steal_batch=5))
        tasks = [
            StealableTask(
                task_id="pinned",
                node_id="victim",
                pinned_node="victim",
                queued_at=time.time(),
            ),
            _make_task("free", "victim"),
        ]
        selected = engine.select_tasks_to_steal(tasks, "thief")
        assert len(selected) == 1
        assert selected[0].task_id == "free"

    def test_allows_task_pinned_to_thief(self) -> None:
        engine = TaskStealingEngine(StealConfig(max_steal_batch=5))
        tasks = [
            StealableTask(
                task_id="pinned-to-thief",
                node_id="victim",
                pinned_node="thief",
                queued_at=time.time(),
            ),
        ]
        selected = engine.select_tasks_to_steal(tasks, "thief")
        assert len(selected) == 1


# ---------------------------------------------------------------------------
# attempt_steal
# ---------------------------------------------------------------------------


class TestAttemptSteal:
    def test_successful_steal(self) -> None:
        engine = TaskStealingEngine(StealConfig(steal_threshold=2))
        nodes = [
            _make_node("thief", queued=0, slots=4),
            _make_node("victim", queued=5),
        ]
        victim_tasks = {
            "victim": [
                _make_task("t1", "victim"),
                _make_task("t2", "victim"),
                _make_task("t3", "victim"),
            ],
        }
        result = engine.attempt_steal("thief", nodes, victim_tasks)
        assert result.result == StealResult.SUCCESS
        assert len(result.tasks_stolen) == 1  # default batch=1
        assert result.thief_node == "thief"
        assert result.victim_node == "victim"

    def test_no_candidates(self) -> None:
        engine = TaskStealingEngine(StealConfig(steal_threshold=10))
        nodes = [
            _make_node("thief", queued=0),
            _make_node("other", queued=2),
        ]
        result = engine.attempt_steal("thief", nodes, {})
        assert result.result == StealResult.NO_CANDIDATES

    def test_disabled_returns_no_candidates(self) -> None:
        engine = TaskStealingEngine(StealConfig(enabled=False))
        result = engine.attempt_steal("thief", [], {})
        assert result.result == StealResult.NO_CANDIDATES

    def test_history_recorded(self) -> None:
        engine = TaskStealingEngine(StealConfig(steal_threshold=1))
        nodes = [
            _make_node("thief", queued=0),
            _make_node("victim", queued=5),
        ]
        victim_tasks = {
            "victim": [_make_task("t1", "victim"), _make_task("t2", "victim")],
        }
        engine.attempt_steal("thief", nodes, victim_tasks)
        assert len(engine.history) == 1

    def test_victim_below_threshold_with_empty_tasks(self) -> None:
        engine = TaskStealingEngine(StealConfig(steal_threshold=3))
        nodes = [
            _make_node("thief", queued=0),
            _make_node("victim", queued=5),
        ]
        # Victim has enough queued per NodeLoad, but actual task list is short
        victim_tasks: dict[str, list[StealableTask]] = {
            "victim": [_make_task("t1", "victim")],
        }
        result = engine.attempt_steal("thief", nodes, victim_tasks)
        assert result.result == StealResult.VICTIM_BELOW_THRESHOLD


# ---------------------------------------------------------------------------
# Reset / clear
# ---------------------------------------------------------------------------


class TestResetClear:
    def test_reset_cooldowns(self) -> None:
        engine = TaskStealingEngine(StealConfig(steal_threshold=1, cooldown_s=9999))
        nodes = [_make_node("thief", queued=0), _make_node("victim", queued=5)]
        victim_tasks = {"victim": [_make_task("t1", "victim"), _make_task("t2", "victim")]}
        # First steal succeeds
        r1 = engine.attempt_steal("thief", nodes, victim_tasks)
        assert r1.result == StealResult.SUCCESS
        # Second blocked by cooldown
        r2 = engine.attempt_steal("thief", nodes, victim_tasks)
        assert r2.result == StealResult.NO_CANDIDATES
        # After reset, stealing works again
        engine.reset_cooldowns()
        r3 = engine.attempt_steal("thief", nodes, victim_tasks)
        assert r3.result == StealResult.SUCCESS

    def test_clear_history(self) -> None:
        engine = TaskStealingEngine(StealConfig(steal_threshold=100))
        engine.attempt_steal("thief", [], {})
        assert len(engine.history) > 0
        engine.clear_history()
        assert len(engine.history) == 0
