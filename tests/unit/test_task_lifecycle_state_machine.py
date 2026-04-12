"""Tests for Task Lifecycle & State Machine enhancements (TASK-001 through TASK-005).

Covers:
- TASK-001: Idempotency tokens for state transitions
- TASK-002: WAITING_FOR_SUBTASKS timeout with escalation
- TASK-003: File ownership validation before claim
- TASK-004: Guard claimed->done requires completion data
- TASK-005: Cascading cancellation for subtasks
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from bernstein.core.lifecycle import (
    DuplicateTransitionError,
    _LRUSet,
    _seen_transition_ids,
    transition_agent,
    transition_task,
)
from bernstein.core.models import AgentSession, Task, TaskStatus
from bernstein.core.task_store import TaskStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task_request(
    *,
    title: str = "Implement parser",
    description: str = "Write the parser module.",
    role: str = "backend",
    priority: int = 1,
    scope: str = "medium",
    complexity: str = "medium",
    depends_on: list[str] | None = None,
    parent_task_id: str | None = None,
    owned_files: list[str] | None = None,
) -> Any:
    """Build a create-task request SimpleNamespace matching TaskCreateRequest protocol."""
    return SimpleNamespace(
        title=title,
        description=description,
        role=role,
        priority=priority,
        scope=scope,
        complexity=complexity,
        estimated_minutes=30,
        depends_on=depends_on or [],
        parent_task_id=parent_task_id,
        owned_files=owned_files or [],
        cell_id=None,
        task_type="standard",
        upgrade_details=None,
        model=None,
        effort=None,
        batch_eligible=False,
        completion_signals=[],
        slack_context=None,
        tenant_id="default",
        repo=None,
        depends_on_repo=None,
        eu_ai_act_risk="minimal",
        approval_required=False,
        risk_level="low",
        parent_session_id=None,
        metadata=None,
    )


def _make_task(
    *,
    task_id: str = "t-1",
    status: TaskStatus = TaskStatus.OPEN,
    owned_files: list[str] | None = None,
    parent_task_id: str | None = None,
) -> Task:
    """Create a minimal Task for unit tests."""
    return Task(
        id=task_id,
        title="Test",
        description="desc",
        role="backend",
        status=status,
        owned_files=owned_files or [],
        parent_task_id=parent_task_id,
    )


# ===========================================================================
# TASK-001: Idempotency tokens
# ===========================================================================


class TestIdempotencyTokens:
    """Idempotency tokens prevent duplicate state transitions."""

    def setup_method(self) -> None:
        """Clear the global seen-IDs set before each test."""
        _seen_transition_ids._data.clear()

    def test_transition_task_accepts_unique_transition_id(self) -> None:
        task = _make_task()
        tid = uuid.uuid4().hex
        with patch("bernstein.core.observability.telemetry.start_span"):
            event = transition_task(task, TaskStatus.CLAIMED, actor="test", transition_id=tid)
        assert task.status == TaskStatus.CLAIMED
        assert event.to_status == "claimed"

    def test_transition_task_rejects_duplicate_transition_id(self) -> None:
        task1 = _make_task(task_id="t-1")
        task2 = _make_task(task_id="t-2")
        tid = uuid.uuid4().hex
        with patch("bernstein.core.observability.telemetry.start_span"):
            transition_task(task1, TaskStatus.CLAIMED, actor="test", transition_id=tid)
        with pytest.raises(DuplicateTransitionError, match=tid):
            transition_task(task2, TaskStatus.CLAIMED, actor="test", transition_id=tid)
        # task2 should remain OPEN (not mutated)
        assert task2.status == TaskStatus.OPEN

    def test_transition_task_without_id_is_not_checked(self) -> None:
        """Transitions without a transition_id bypass idempotency checks."""
        task = _make_task()
        with patch("bernstein.core.observability.telemetry.start_span"):
            transition_task(task, TaskStatus.CLAIMED, actor="test")
        assert task.status == TaskStatus.CLAIMED

    def test_transition_agent_accepts_unique_transition_id(self) -> None:
        agent = AgentSession(id="a-1", role="backend", status="starting")
        tid = uuid.uuid4().hex
        with patch("bernstein.core.observability.telemetry.start_span"):
            event = transition_agent(agent, "working", actor="test", transition_id=tid)
        assert agent.status == "working"
        assert event.to_status == "working"

    def test_transition_agent_rejects_duplicate_transition_id(self) -> None:
        agent1 = AgentSession(id="a-1", role="backend", status="starting")
        agent2 = AgentSession(id="a-2", role="backend", status="starting")
        tid = uuid.uuid4().hex
        with patch("bernstein.core.observability.telemetry.start_span"):
            transition_agent(agent1, "working", actor="test", transition_id=tid)
        with pytest.raises(DuplicateTransitionError, match=tid):
            transition_agent(agent2, "working", actor="test", transition_id=tid)
        assert agent2.status == "starting"

    def test_lru_eviction_allows_reuse_after_overflow(self) -> None:
        """After exceeding the max capacity, the oldest ID is evicted."""
        lru = _LRUSet(maxsize=3)
        lru.add("a")
        lru.add("b")
        lru.add("c")
        assert "a" in lru
        lru.add("d")  # evicts "a"
        assert "a" not in lru
        assert "b" in lru
        assert len(lru) == 3


# ===========================================================================
# TASK-002: WAITING_FOR_SUBTASKS timeout with escalation
# ===========================================================================


class TestSubtaskTimeout:
    """WAITING_FOR_SUBTASKS tasks are escalated after a configurable timeout."""

    @pytest.mark.anyio
    async def test_timed_out_task_is_escalated_to_blocked(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
        parent = await store.create(_task_request(title="parent"))
        await store.claim_by_id(parent.id, expected_version=parent.version)
        await store.wait_for_subtasks(parent.id, subtask_count=2)

        # Simulate time passing (backdate the wait start)
        parent_task = store.get_task(parent.id)
        assert parent_task is not None
        parent_task.subtask_wait_started_at = time.time() - 2000

        escalated = await store.check_subtask_timeouts(timeout_s=1800)

        assert len(escalated) == 1
        assert escalated[0].id == parent.id
        assert escalated[0].status == TaskStatus.BLOCKED
        assert "ESCALATION" in (escalated[0].result_summary or "")

    @pytest.mark.anyio
    async def test_non_timed_out_task_is_not_escalated(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
        parent = await store.create(_task_request(title="parent"))
        await store.claim_by_id(parent.id, expected_version=parent.version)
        await store.wait_for_subtasks(parent.id, subtask_count=1)

        escalated = await store.check_subtask_timeouts(timeout_s=3600)

        assert len(escalated) == 0
        assert store.get_task(parent.id) is not None
        assert store.get_task(parent.id).status == TaskStatus.WAITING_FOR_SUBTASKS  # type: ignore[union-attr]

    @pytest.mark.anyio
    async def test_wait_for_subtasks_sets_timestamp(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
        parent = await store.create(_task_request(title="parent"))
        await store.claim_by_id(parent.id, expected_version=parent.version)

        before = time.time()
        await store.wait_for_subtasks(parent.id, subtask_count=3)
        after = time.time()

        task = store.get_task(parent.id)
        assert task is not None
        assert task.subtask_wait_started_at is not None
        assert before <= task.subtask_wait_started_at <= after

    @pytest.mark.anyio
    async def test_custom_timeout_value(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
        parent = await store.create(_task_request(title="parent"))
        await store.claim_by_id(parent.id, expected_version=parent.version)
        await store.wait_for_subtasks(parent.id, subtask_count=1)

        task = store.get_task(parent.id)
        assert task is not None
        task.subtask_wait_started_at = time.time() - 10

        # Short timeout should trigger escalation
        escalated = await store.check_subtask_timeouts(timeout_s=5)
        assert len(escalated) == 1

    @pytest.mark.anyio
    async def test_default_timeout_is_30_minutes(self) -> None:
        """Verify the default constant value."""
        assert TaskStore.SUBTASK_WAIT_TIMEOUT_S == 30 * 60


# ===========================================================================
# TASK-003: File ownership validation before claim
# ===========================================================================


class TestFileOwnershipValidation:
    """Tasks with overlapping owned_files cannot be claimed concurrently."""

    @pytest.mark.anyio
    async def test_claim_next_skips_task_with_overlapping_files(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

        # First task claims some files
        t1 = await store.create(_task_request(title="task-1", owned_files=["src/foo.py", "src/bar.py"]))
        await store.claim_next("backend")
        assert store.get_task(t1.id) is not None
        assert store.get_task(t1.id).status == TaskStatus.CLAIMED  # type: ignore[union-attr]

        # Second task overlaps on src/foo.py
        t2 = await store.create(_task_request(title="task-2", priority=1, owned_files=["src/foo.py"]))
        # Third task has no overlap
        t3 = await store.create(_task_request(title="task-3", priority=2, owned_files=["src/baz.py"]))

        # claim_next should skip t2 (overlap) and claim t3
        claimed = await store.claim_next("backend")
        assert claimed is not None
        assert claimed.id == t3.id

        # t2 should still be open
        assert store.get_task(t2.id) is not None
        assert store.get_task(t2.id).status == TaskStatus.OPEN  # type: ignore[union-attr]

    @pytest.mark.anyio
    async def test_claim_by_id_rejects_overlapping_files(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

        t1 = await store.create(_task_request(title="task-1", owned_files=["src/module.py"]))
        await store.claim_by_id(t1.id, expected_version=t1.version)

        t2 = await store.create(_task_request(title="task-2", owned_files=["src/module.py"]))

        with pytest.raises(ValueError, match="File ownership conflict"):
            await store.claim_by_id(t2.id, expected_version=t2.version)

    @pytest.mark.anyio
    async def test_claim_batch_rejects_overlapping_files(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

        t1 = await store.create(_task_request(title="task-1", owned_files=["src/a.py"]))
        await store.claim_by_id(t1.id, expected_version=t1.version)

        t2 = await store.create(_task_request(title="task-2", owned_files=["src/a.py"]))
        t3 = await store.create(_task_request(title="task-3", owned_files=["src/b.py"]))

        claimed, failed = await store.claim_batch([t2.id, t3.id], agent_id="agent-1")

        assert t2.id in failed
        assert t3.id in claimed

    @pytest.mark.anyio
    async def test_no_overlap_allows_claim(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

        t1 = await store.create(_task_request(title="task-1", owned_files=["src/x.py"]))
        await store.claim_by_id(t1.id, expected_version=t1.version)

        t2 = await store.create(_task_request(title="task-2", owned_files=["src/y.py"]))
        result = await store.claim_by_id(t2.id, expected_version=t2.version)
        assert result.status == TaskStatus.CLAIMED

    @pytest.mark.anyio
    async def test_empty_owned_files_always_allowed(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

        t1 = await store.create(_task_request(title="task-1", owned_files=["src/x.py"]))
        await store.claim_by_id(t1.id, expected_version=t1.version)

        t2 = await store.create(_task_request(title="task-2"))
        result = await store.claim_by_id(t2.id, expected_version=t2.version)
        assert result.status == TaskStatus.CLAIMED


# ===========================================================================
# TASK-004: Guard claimed->done requires completion data
# ===========================================================================


class TestCompletionDataGuard:
    """Completing a task requires non-empty result_summary."""

    @pytest.mark.anyio
    async def test_complete_rejects_empty_string(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
        task = await store.create(_task_request())
        await store.claim_by_id(task.id, expected_version=task.version)

        with pytest.raises(ValueError, match="result_summary must be non-empty"):
            await store.complete(task.id, "")

    @pytest.mark.anyio
    async def test_complete_rejects_whitespace_only(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
        task = await store.create(_task_request())
        await store.claim_by_id(task.id, expected_version=task.version)

        with pytest.raises(ValueError, match="result_summary must be non-empty"):
            await store.complete(task.id, "   \n\t  ")

    @pytest.mark.anyio
    async def test_complete_accepts_valid_summary(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
        task = await store.create(_task_request())
        await store.claim_by_id(task.id, expected_version=task.version)

        result = await store.complete(task.id, "Implemented parser with 95% coverage")
        assert result.status == TaskStatus.DONE
        assert result.result_summary == "Implemented parser with 95% coverage"

    @pytest.mark.anyio
    async def test_complete_accepts_diff_reference(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
        task = await store.create(_task_request())
        await store.claim_by_id(task.id, expected_version=task.version)

        result = await store.complete(task.id, "diff: +42 -10 in src/parser.py")
        assert result.status == TaskStatus.DONE


# ===========================================================================
# TASK-005: Cascading cancellation for subtasks
# ===========================================================================


class TestCascadingCancellation:
    """Cancelling a parent propagates cancellation to all descendants."""

    @pytest.mark.anyio
    async def test_cancel_cascade_cancels_parent_and_children(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

        parent = await store.create(_task_request(title="parent"))
        child1 = await store.create(_task_request(title="child-1", parent_task_id=parent.id))
        child2 = await store.create(_task_request(title="child-2", parent_task_id=parent.id))

        cancelled = await store.cancel_cascade(parent.id, "project cancelled")

        assert len(cancelled) == 3
        cancelled_ids = {t.id for t in cancelled}
        assert parent.id in cancelled_ids
        assert child1.id in cancelled_ids
        assert child2.id in cancelled_ids

        for t in cancelled:
            assert t.status == TaskStatus.CANCELLED

    @pytest.mark.anyio
    async def test_cancel_cascade_propagates_to_grandchildren(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

        root = await store.create(_task_request(title="root"))
        child = await store.create(_task_request(title="child", parent_task_id=root.id))
        grandchild = await store.create(_task_request(title="grandchild", parent_task_id=child.id))

        cancelled = await store.cancel_cascade(root.id, "abort")

        assert len(cancelled) == 3
        gc = store.get_task(grandchild.id)
        assert gc is not None
        assert gc.status == TaskStatus.CANCELLED
        assert "Cascade" in (gc.result_summary or "")

    @pytest.mark.anyio
    async def test_cancel_cascade_skips_already_done_tasks(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

        parent = await store.create(_task_request(title="parent"))
        child_done = await store.create(_task_request(title="child-done", parent_task_id=parent.id))
        child_open = await store.create(_task_request(title="child-open", parent_task_id=parent.id))

        # Complete child_done first
        await store.claim_by_id(child_done.id, expected_version=child_done.version)
        await store.complete(child_done.id, "finished")

        cancelled = await store.cancel_cascade(parent.id, "abort")

        # child_done should NOT be cancelled (already DONE)
        cancelled_ids = {t.id for t in cancelled}
        assert child_done.id not in cancelled_ids
        assert parent.id in cancelled_ids
        assert child_open.id in cancelled_ids

        # Verify the done child is still done
        done_task = store.get_task(child_done.id)
        assert done_task is not None
        assert done_task.status == TaskStatus.DONE

    @pytest.mark.anyio
    async def test_cancel_cascade_handles_leaf_task(self, tmp_path: Path) -> None:
        """Cancelling a leaf task (no children) works like regular cancel."""
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

        leaf = await store.create(_task_request(title="leaf"))
        cancelled = await store.cancel_cascade(leaf.id, "unwanted")

        assert len(cancelled) == 1
        assert cancelled[0].id == leaf.id
        assert cancelled[0].status == TaskStatus.CANCELLED

    @pytest.mark.anyio
    async def test_cancel_cascade_raises_on_unknown_task(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

        with pytest.raises(KeyError):
            await store.cancel_cascade("nonexistent", "reason")

    @pytest.mark.anyio
    async def test_cancel_cascade_includes_blocked_and_waiting(self, tmp_path: Path) -> None:
        """Blocked and waiting_for_subtasks children should also be cancelled."""
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

        parent = await store.create(_task_request(title="parent"))
        child_blocked = await store.create(_task_request(title="blocked-child", parent_task_id=parent.id))
        # Manually set to BLOCKED via claim then block
        await store.claim_by_id(child_blocked.id, expected_version=child_blocked.version)
        await store.block(child_blocked.id, "needs input")

        cancelled = await store.cancel_cascade(parent.id, "abort")

        cancelled_ids = {t.id for t in cancelled}
        assert child_blocked.id in cancelled_ids
        assert parent.id in cancelled_ids
