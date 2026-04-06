"""Tests for atomic batch transitions (TASK-011)."""

from __future__ import annotations

from bernstein.core.batch_transitions import (
    TransitionSpec,
    apply_batch_transition,
    complete_stage,
    fail_stage,
)
from bernstein.core.models import Complexity, Scope, Task, TaskStatus


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
