"""Tests for MergeQueue and create_conflict_resolution_task."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from bernstein.core.merge_queue import MergeJob, MergeQueue
from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType
from bernstein.core.task_lifecycle import create_conflict_resolution_task

# ---------------------------------------------------------------------------
# MergeJob
# ---------------------------------------------------------------------------


class TestMergeJob:
    def test_branch_name_derived(self) -> None:
        job = MergeJob(session_id="backend-abc123", task_id="T-1")
        assert job.branch_name == "agent/backend-abc123"

    def test_fields(self) -> None:
        job = MergeJob(session_id="qa-xyz", task_id="T-99", task_title="Fix tests")
        assert job.session_id == "qa-xyz"
        assert job.task_id == "T-99"
        assert job.task_title == "Fix tests"


# ---------------------------------------------------------------------------
# MergeQueue
# ---------------------------------------------------------------------------


class TestMergeQueue:
    def test_empty_dequeue_returns_none(self) -> None:
        q = MergeQueue()
        assert q.dequeue() is None

    def test_empty_peek_returns_none(self) -> None:
        q = MergeQueue()
        assert q.peek() is None

    def test_enqueue_dequeue_fifo(self) -> None:
        q = MergeQueue()
        q.enqueue("session-1", task_id="T-1")
        q.enqueue("session-2", task_id="T-2")
        q.enqueue("session-3", task_id="T-3")

        first = q.dequeue()
        assert first is not None
        assert first.session_id == "session-1"

        second = q.dequeue()
        assert second is not None
        assert second.session_id == "session-2"

    def test_len(self) -> None:
        q = MergeQueue()
        assert len(q) == 0
        q.enqueue("s1", task_id="T-1")
        assert len(q) == 1
        q.enqueue("s2", task_id="T-2")
        assert len(q) == 2
        q.dequeue()
        assert len(q) == 1

    def test_bool_empty(self) -> None:
        q = MergeQueue()
        assert not q

    def test_bool_non_empty(self) -> None:
        q = MergeQueue()
        q.enqueue("s1", task_id="T-1")
        assert q

    def test_peek_does_not_remove(self) -> None:
        q = MergeQueue()
        q.enqueue("session-1", task_id="T-1")
        peeked = q.peek()
        assert peeked is not None
        assert peeked.session_id == "session-1"
        assert len(q) == 1  # still in queue

    def test_drain_to_empty(self) -> None:
        q = MergeQueue()
        q.enqueue("s1", task_id="T-1")
        q.dequeue()
        assert q.dequeue() is None
        assert len(q) == 0

    def test_merge_lock_exists(self) -> None:
        q = MergeQueue()
        assert isinstance(q.merge_lock, type(threading.Lock()))

    def test_thread_safety_concurrent_enqueue(self) -> None:
        """Concurrent enqueues from multiple threads should not lose items."""
        q = MergeQueue()
        threads = [
            threading.Thread(target=q.enqueue, args=(f"session-{i}",), kwargs={"task_id": f"T-{i}"}) for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(q) == 20

    def test_merge_lock_serializes_operations(self) -> None:
        """merge_lock should be acquirable and exclusive."""
        q = MergeQueue()
        acquired = []

        def _acquire() -> None:
            with q.merge_lock:
                acquired.append(threading.current_thread().name)

        threads = [threading.Thread(target=_acquire, name=f"t{i}") for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # All three should have acquired the lock (just not simultaneously)
        assert len(acquired) == 3


# ---------------------------------------------------------------------------
# create_conflict_resolution_task
# ---------------------------------------------------------------------------


def _make_task(
    *,
    id: str = "T-001",
    title: str = "Fix auth bug",
    description: str = "Fix the auth module",
    role: str = "backend",
    priority: int = 2,
) -> Task:
    return Task(
        id=id,
        title=title,
        description=description,
        role=role,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus.DONE,
        task_type=TaskType.STANDARD,
        priority=priority,
        owned_files=[],
        mcp_servers=[],
    )


class TestCreateConflictResolutionTask:
    def test_creates_resolver_task(self) -> None:
        import httpx

        mock_client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "resolver-001"}
        mock_client.post.return_value = mock_resp

        task = _make_task()
        result = create_conflict_resolution_task(
            task,
            ["src/auth.py", "src/utils.py"],
            client=mock_client,
            server_url="http://127.0.0.1:8052",
            session_id="backend-abc123",
        )

        assert result == "resolver-001"
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "http://127.0.0.1:8052/tasks"
        body = call_args[1]["json"]
        assert body["role"] == "resolver"
        assert "[CONFLICT]" in body["title"]
        assert "Fix auth bug" in body["title"]
        assert "src/auth.py" in body["owned_files"]
        assert "src/utils.py" in body["owned_files"]

    def test_conflict_task_description_includes_files(self) -> None:
        import httpx

        mock_client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "resolver-002"}
        mock_client.post.return_value = mock_resp

        task = _make_task()
        create_conflict_resolution_task(
            task,
            ["src/a.py", "src/b.py"],
            client=mock_client,
            server_url="http://127.0.0.1:8052",
            session_id="backend-xyz",
        )

        body = mock_client.post.call_args[1]["json"]
        assert "src/a.py" in body["description"]
        assert "src/b.py" in body["description"]
        assert "backend-xyz" in body["description"]

    def test_conflict_task_priority_elevated(self) -> None:
        import httpx

        mock_client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "resolver-003"}
        mock_client.post.return_value = mock_resp

        task = _make_task(priority=3)
        create_conflict_resolution_task(
            task,
            ["src/a.py"],
            client=mock_client,
            server_url="http://127.0.0.1:8052",
            session_id="s1",
        )

        body = mock_client.post.call_args[1]["json"]
        # Resolver task priority should be higher (lower number) than original
        assert body["priority"] < 3

    def test_returns_none_on_http_error(self) -> None:
        import httpx

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.side_effect = httpx.ConnectError("connection refused")

        task = _make_task()
        result = create_conflict_resolution_task(
            task,
            ["src/a.py"],
            client=mock_client,
            server_url="http://127.0.0.1:8052",
            session_id="s1",
        )
        assert result is None

    def test_priority_floor_at_one(self) -> None:
        """Priority should not go below 1."""
        import httpx

        mock_client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "r"}
        mock_client.post.return_value = mock_resp

        task = _make_task(priority=1)  # Already at minimum
        create_conflict_resolution_task(
            task,
            ["src/a.py"],
            client=mock_client,
            server_url="http://127.0.0.1:8052",
            session_id="s1",
        )

        body = mock_client.post.call_args[1]["json"]
        assert body["priority"] >= 1
