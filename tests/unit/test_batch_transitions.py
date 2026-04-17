"""Tests for atomic batch transitions (TASK-011, audit-024).

Every batch transition MUST flow through the FSM ``transition_task``.
These tests verify that:
  * Legal batch transitions succeed and emit ``LifecycleEvent``s.
  * Illegal batch transitions fail, trigger rollback, and surface the
    FSM error reason — never silently mutate ``task.status``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bernstein.core.batch_transitions import (
    TransitionSpec,
    apply_batch_transition,
    complete_stage,
    fail_stage,
)
from bernstein.core.models import Complexity, Scope, Task, TaskStatus

from bernstein.core.tasks.lifecycle import (
    IllegalTransitionError,
    add_listener,
    remove_listener,
    transition_task,
)

if TYPE_CHECKING:
    from bernstein.core.tasks.models import LifecycleEvent


def _t(id: str, status: str = "open") -> Task:
    return Task(
        id=id,
        title=f"Task {id}",
        description="desc",
        role="backend",
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus(status),
    )


class TestApplyBatchTransition:
    def test_all_succeed(self) -> None:
        tasks = [_t("t1", "open"), _t("t2", "open")]
        specs = [
            TransitionSpec("t1", TaskStatus.OPEN, TaskStatus.CLAIMED),
            TransitionSpec("t2", TaskStatus.OPEN, TaskStatus.CLAIMED),
        ]
        result = apply_batch_transition(tasks, specs)
        assert result.success
        assert set(result.transitioned) == {"t1", "t2"}
        assert tasks[0].status == TaskStatus.CLAIMED
        assert tasks[1].status == TaskStatus.CLAIMED

    def test_precondition_failure_aborts_all(self) -> None:
        tasks = [_t("t1", "open"), _t("t2", "done")]
        specs = [
            TransitionSpec("t1", TaskStatus.OPEN, TaskStatus.CLAIMED),
            TransitionSpec("t2", TaskStatus.OPEN, TaskStatus.CLAIMED),  # Wrong from_status
        ]
        result = apply_batch_transition(tasks, specs)
        assert not result.success
        assert len(result.failed) == 1
        assert result.failed[0][0] == "t2"
        # t1 should NOT have been transitioned
        assert tasks[0].status == TaskStatus.OPEN

    def test_missing_task_fails(self) -> None:
        tasks = [_t("t1", "open")]
        specs = [
            TransitionSpec("t1", TaskStatus.OPEN, TaskStatus.CLAIMED),
            TransitionSpec("missing", TaskStatus.OPEN, TaskStatus.CLAIMED),
        ]
        result = apply_batch_transition(tasks, specs)
        assert not result.success
        assert any("missing" in f[0] for f in result.failed)

    def test_empty_specs(self) -> None:
        tasks = [_t("t1")]
        result = apply_batch_transition(tasks, [])
        assert result.success
        assert result.transitioned == []


class TestCompleteStage:
    def test_complete_in_progress_tasks(self) -> None:
        tasks = [_t("t1", "in_progress"), _t("t2", "claimed")]
        result = complete_stage(tasks, ["t1", "t2"])
        assert result.success
        assert set(result.transitioned) == {"t1", "t2"}
        assert tasks[0].status == TaskStatus.DONE
        assert tasks[1].status == TaskStatus.DONE
        assert tasks[0].completed_at is not None
        assert tasks[1].completed_at is not None

    def test_skip_already_done(self) -> None:
        tasks = [_t("t1", "in_progress"), _t("t2", "done")]
        result = complete_stage(tasks, ["t1", "t2"])
        assert result.success
        assert result.transitioned == ["t1"]
        assert tasks[0].status == TaskStatus.DONE

    def test_skip_unknown_task_ids(self) -> None:
        tasks = [_t("t1", "in_progress")]
        result = complete_stage(tasks, ["t1", "nonexistent"])
        assert result.success
        assert result.transitioned == ["t1"]

    def test_empty_stage(self) -> None:
        tasks = [_t("t1", "open")]
        result = complete_stage(tasks, [])
        assert result.success
        assert result.transitioned == []

    def test_nothing_to_complete(self) -> None:
        tasks = [_t("t1", "open"), _t("t2", "done")]
        result = complete_stage(tasks, ["t1", "t2"])
        assert result.success
        # t1 is open (not claimed/in_progress), t2 is already done
        assert result.transitioned == []


class TestFailStage:
    def test_fail_non_terminal_tasks(self) -> None:
        tasks = [_t("t1", "open"), _t("t2", "in_progress"), _t("t3", "done")]
        result = fail_stage(tasks, ["t1", "t2", "t3"], reason="test failure")
        assert result.success
        assert set(result.transitioned) == {"t1", "t2"}
        assert tasks[0].status == TaskStatus.FAILED
        assert tasks[1].status == TaskStatus.FAILED
        assert tasks[2].status == TaskStatus.DONE  # Not touched
        assert tasks[0].result_summary == "test failure"

    def test_skip_already_terminal(self) -> None:
        tasks = [_t("t1", "done"), _t("t2", "failed"), _t("t3", "cancelled")]
        result = fail_stage(tasks, ["t1", "t2", "t3"])
        assert result.success
        assert result.transitioned == []

    def test_empty_stage_fail(self) -> None:
        result = fail_stage([], [])
        assert result.success


# ---------------------------------------------------------------------------
# FSM routing contract (audit-024)
# ---------------------------------------------------------------------------


class TestBatchFsmRouting:
    """Batch transitions must go through transition_task and the FSM."""

    def test_fsm_batch_emits_lifecycle_events_for_each_transition(self) -> None:
        events: list[LifecycleEvent] = []
        add_listener(events.append)
        try:
            tasks = [_t("t1", "claimed"), _t("t2", "in_progress")]
            result = complete_stage(tasks, ["t1", "t2"])
            assert result.success
        finally:
            remove_listener(events.append)

        # One LifecycleEvent per transitioned task, all marked entity_type=task
        # and with to_status=done
        task_events = [e for e in events if e.entity_type == "task" and e.to_status == "done"]
        emitted_ids = {e.entity_id for e in task_events}
        assert emitted_ids == {"t1", "t2"}
        assert all(e.actor == "batch" for e in task_events)

    def test_fsm_batch_rejects_illegal_transition_and_rolls_back(self) -> None:
        # CLOSED is terminal; there is no CLOSED -> OPEN edge.  An
        # apply_batch_transition attempting this must NOT bypass the FSM;
        # the first spec succeeds (open -> claimed) then the second fails
        # and triggers rollback so t1 returns to OPEN.
        tasks = [_t("t1", "open"), _t("t2", "closed")]
        specs = [
            TransitionSpec("t1", TaskStatus.OPEN, TaskStatus.CLAIMED),
            TransitionSpec("t2", TaskStatus.CLOSED, TaskStatus.OPEN),  # illegal FSM edge
        ]

        result = apply_batch_transition(tasks, specs)

        assert not result.success
        assert result.rolled_back
        assert len(result.failed) == 1
        assert result.failed[0][0] == "t2"
        assert "Illegal" in result.failed[0][1] or "illegal" in result.failed[0][1].lower()
        # Rollback must restore t1 to OPEN and leave t2 CLOSED
        assert tasks[0].status == TaskStatus.OPEN
        assert tasks[1].status == TaskStatus.CLOSED

    def test_fsm_batch_direct_illegal_transition_raises(self) -> None:
        # Direct transition_task on CLOSED -> OPEN must raise — we rely on
        # this contract inside apply_batch_transition.
        task = _t("t1", "closed")
        with pytest.raises(IllegalTransitionError):
            transition_task(task, TaskStatus.OPEN, actor="test")

    def test_fsm_batch_fail_stage_allows_open_to_failed(self) -> None:
        # audit-024 adds the OPEN -> FAILED edge so fail_stage can legitimately
        # fail unclaimed tasks without bypassing the FSM.
        tasks = [_t("t1", "open"), _t("t2", "blocked"), _t("t3", "waiting_for_subtasks")]
        result = fail_stage(tasks, ["t1", "t2", "t3"], reason="ci gate failed")
        assert result.success
        assert set(result.transitioned) == {"t1", "t2", "t3"}
        assert tasks[0].status == TaskStatus.FAILED
        assert tasks[1].status == TaskStatus.FAILED
        assert tasks[2].status == TaskStatus.FAILED

    def test_fsm_batch_complete_stage_refuses_open_to_done(self) -> None:
        # complete_stage only targets CLAIMED/IN_PROGRESS — an OPEN task must
        # be skipped, not force-transitioned to DONE by bypassing the FSM.
        tasks = [_t("t1", "open"), _t("t2", "in_progress")]
        result = complete_stage(tasks, ["t1", "t2"])
        assert result.success
        # Only t2 transitioned; t1 stayed OPEN (complete_stage filter).
        assert result.transitioned == ["t2"]
        assert tasks[0].status == TaskStatus.OPEN
        assert tasks[1].status == TaskStatus.DONE

    def test_batch_transition_mode_rollback_emits_events(self) -> None:
        # Ensure the rollback path also routes through transition_task
        # where possible, producing a second set of lifecycle events.
        events: list[LifecycleEvent] = []
        add_listener(events.append)
        try:
            tasks = [_t("t1", "open"), _t("t2", "closed")]
            specs = [
                TransitionSpec("t1", TaskStatus.OPEN, TaskStatus.CLAIMED),
                TransitionSpec("t2", TaskStatus.CLOSED, TaskStatus.OPEN),  # illegal
            ]
            result = apply_batch_transition(tasks, specs)
            assert not result.success
            assert result.rolled_back
        finally:
            remove_listener(events.append)

        # Forward: t1 open->claimed, rollback: t1 claimed->open — both legal.
        t1_events = [e for e in events if e.entity_id == "t1" and e.entity_type == "task"]
        forward = [e for e in t1_events if e.from_status == "open" and e.to_status == "claimed"]
        rollback = [e for e in t1_events if e.from_status == "claimed" and e.to_status == "open"]
        assert len(forward) == 1
        assert len(rollback) == 1
        assert rollback[0].reason.endswith(":rollback")
