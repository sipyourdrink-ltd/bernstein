"""Tests for MergeQueue, conflict detection, and create_conflict_resolution_task."""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from bernstein.core.git_basic import GitResult
from bernstein.core.merge_queue import (
    ConflictCheckResult,
    MergeJob,
    MergeQueue,
    _parse_merge_tree_conflicts,
    detect_merge_conflicts,
)
from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType
from bernstein.core.task_lifecycle import create_conflict_resolution_task

# ---------------------------------------------------------------------------
# Sample git merge-tree outputs
# ---------------------------------------------------------------------------

_SHA = "a" * 40  # dummy 40-char SHA

_ONE_CONFLICT = f"""\
changed in both
  base   100644 {_SHA} src/auth.py
  our    100644 {_SHA} src/auth.py
  their  100644 {_SHA} src/auth.py
@@ -1,3 +1,7 @@
 def foo():
+<<<<<<< .our
+    return 1
+=======
+    return 2
+>>>>>>> .their
     pass
"""

_TWO_CONFLICTS = f"""\
changed in both
  base   100644 {_SHA} src/auth.py
  our    100644 {_SHA} src/auth.py
  their  100644 {_SHA} src/auth.py
@@ -1,3 +1,7 @@
+<<<<<<< .our
+    return 1
+=======
+    return 2
+>>>>>>> .their
changed in both
  base   100644 {_SHA} src/utils.py
  our    100644 {_SHA} src/utils.py
  their  100644 {_SHA} src/utils.py
@@ -1,3 +1,7 @@
+<<<<<<< .our
+    x = 1
+=======
+    x = 2
+>>>>>>> .their
"""

_CLEAN_MERGE = f"""\
changed in both
  base   100644 {_SHA} src/models.py
  our    100644 {_SHA} src/models.py
  their  100644 {_SHA} src/models.py
@@ -1,3 +1,4 @@
 class Foo:
+    pass
"""

_MIXED = f"""\
changed in both
  base   100644 {_SHA} src/models.py
  our    100644 {_SHA} src/models.py
  their  100644 {_SHA} src/models.py
@@ -1,3 +1,4 @@
 class Foo:
+    pass
changed in both
  base   100644 {_SHA} src/auth.py
  our    100644 {_SHA} src/auth.py
  their  100644 {_SHA} src/auth.py
@@ -1,3 +1,7 @@
+<<<<<<< .our
+    return 1
+=======
+    return 2
+>>>>>>> .their
"""

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


class TestMergeQueueSnapshot:
    def test_empty_snapshot(self) -> None:
        q = MergeQueue()
        snap = q.snapshot()
        assert snap["depth"] == 0
        assert snap["jobs"] == []
        assert snap["is_merging"] is False

    def test_snapshot_includes_job_fields(self) -> None:
        q = MergeQueue()
        q.enqueue("session-1", task_id="T-1", task_title="Fix auth")
        snap = q.snapshot()
        assert snap["depth"] == 1
        job = snap["jobs"][0]
        assert job["session_id"] == "session-1"
        assert job["task_id"] == "T-1"
        assert job["task_title"] == "Fix auth"
        assert job["branch_name"] == "agent/session-1"

    def test_snapshot_is_merging_while_lock_held(self) -> None:
        q = MergeQueue()
        with q.merge_lock:
            snap = q.snapshot()
            assert snap["is_merging"] is True


# ---------------------------------------------------------------------------
# _parse_merge_tree_conflicts
# ---------------------------------------------------------------------------


class TestParseMergeTreeConflicts:
    def test_empty_output(self) -> None:
        assert _parse_merge_tree_conflicts("") == []

    def test_clean_merge_no_conflicts(self) -> None:
        assert _parse_merge_tree_conflicts(_CLEAN_MERGE) == []

    def test_single_conflict(self) -> None:
        result = _parse_merge_tree_conflicts(_ONE_CONFLICT)
        assert result == ["src/auth.py"]

    def test_two_conflicts(self) -> None:
        result = _parse_merge_tree_conflicts(_TWO_CONFLICTS)
        assert "src/auth.py" in result
        assert "src/utils.py" in result
        assert len(result) == 2

    def test_mixed_returns_only_conflicted_file(self) -> None:
        result = _parse_merge_tree_conflicts(_MIXED)
        assert result == ["src/auth.py"]
        assert "src/models.py" not in result

    def test_no_duplicate_paths(self) -> None:
        # base/our/their lines all reference the same path — must dedupe
        result = _parse_merge_tree_conflicts(_ONE_CONFLICT)
        assert result.count("src/auth.py") == 1


# ---------------------------------------------------------------------------
# detect_merge_conflicts
# ---------------------------------------------------------------------------


class TestDetectMergeConflicts:
    def test_no_conflicts(self, tmp_path: Path) -> None:
        base_r = GitResult(returncode=0, stdout="abc123\n", stderr="")
        tree_r = GitResult(returncode=0, stdout=_CLEAN_MERGE, stderr="")

        with patch("bernstein.core.git.merge_queue.run_git", side_effect=[base_r, tree_r]):
            result = detect_merge_conflicts("agent/backend-abc", "main", tmp_path)

        assert not result.has_conflicts
        assert result.conflicting_files == []
        assert result.branch == "agent/backend-abc"
        assert result.base == "main"

    def test_conflict_detected(self, tmp_path: Path) -> None:
        base_r = GitResult(returncode=0, stdout="abc123\n", stderr="")
        tree_r = GitResult(returncode=0, stdout=_ONE_CONFLICT, stderr="")

        with patch("bernstein.core.git.merge_queue.run_git", side_effect=[base_r, tree_r]):
            result = detect_merge_conflicts("agent/backend-abc", "main", tmp_path)

        assert result.has_conflicts
        assert result.conflicting_files == ["src/auth.py"]

    def test_two_files_conflict(self, tmp_path: Path) -> None:
        base_r = GitResult(returncode=0, stdout="deadbeef\n", stderr="")
        tree_r = GitResult(returncode=0, stdout=_TWO_CONFLICTS, stderr="")

        with patch("bernstein.core.git.merge_queue.run_git", side_effect=[base_r, tree_r]):
            result = detect_merge_conflicts("agent/backend-abc", "main", tmp_path)

        assert result.has_conflicts
        assert len(result.conflicting_files) == 2

    def test_merge_base_failure_returns_no_conflict(self, tmp_path: Path) -> None:
        """Unrelated histories / missing branch → skip conflict check."""
        fail_r = GitResult(returncode=1, stdout="", stderr="fatal: no common ancestor\n")

        with patch("bernstein.core.git.merge_queue.run_git", return_value=fail_r):
            result = detect_merge_conflicts("agent/orphan", "main", tmp_path)

        assert not result.has_conflicts
        assert result.conflicting_files == []

    def test_git_commands_use_correct_args(self, tmp_path: Path) -> None:
        base_r = GitResult(returncode=0, stdout="deadbeef\n", stderr="")
        tree_r = GitResult(returncode=0, stdout="", stderr="")

        with patch("bernstein.core.git.merge_queue.run_git", side_effect=[base_r, tree_r]) as mock:
            detect_merge_conflicts("agent/backend-xyz", "main", tmp_path)

        calls = mock.call_args_list
        assert calls[0] == call(["merge-base", "main", "agent/backend-xyz"], tmp_path)
        assert calls[1] == call(["merge-tree", "deadbeef", "main", "agent/backend-xyz"], tmp_path)

    def test_result_fields_populated(self, tmp_path: Path) -> None:
        base_r = GitResult(returncode=0, stdout="abc123\n", stderr="")
        tree_r = GitResult(returncode=0, stdout=_ONE_CONFLICT, stderr="")

        with patch("bernstein.core.git.merge_queue.run_git", side_effect=[base_r, tree_r]):
            result = detect_merge_conflicts("agent/qa-xyz", "main", tmp_path)

        assert isinstance(result, ConflictCheckResult)
        assert result.branch == "agent/qa-xyz"
        assert result.base == "main"


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


# ---------------------------------------------------------------------------
# MergeQueue.submit (audit-091: FIFO serialization of concurrent merges)
# ---------------------------------------------------------------------------


class TestMergeQueueSubmit:
    """submit() is the production entry point that ties enqueue/dequeue
    together under merge_lock so concurrent agent merges serialise in
    strict FIFO order.
    """

    def test_submit_enqueues_job(self) -> None:
        """submit() must enqueue the job before yielding."""
        q = MergeQueue()
        with q.submit("session-a", task_id="T-1", task_title="hello") as job:
            # While inside the block, the job is at the head.
            assert job.session_id == "session-a"
            assert q.peek() is not None
            assert q.peek().session_id == "session-a"  # type: ignore[union-attr]
            assert q.merge_lock.locked()
        # After the block, queue drained and lock released.
        assert q.peek() is None
        assert not q.merge_lock.locked()

    def test_submit_dequeues_of_empty_queue_returns_none(self) -> None:
        """After submit() completes the queue is empty and dequeue is None."""
        q = MergeQueue()
        with q.submit("s1", task_id="T-1"):
            pass
        assert q.dequeue() is None

    def test_submit_concurrent_two_merges_serialize_fifo(self) -> None:
        """Two concurrent submit()s observe strict FIFO order under merge_lock.

        Reproduces audit-091 scenario: without the fix the merge lock was
        acquired ad-hoc so ordering was non-deterministic and the queue
        stayed empty.  With submit() the first thread to enqueue is the
        first to execute, and the second thread blocks until the first
        exits the context manager.
        """
        q = MergeQueue()
        order: list[str] = []
        inside_events: dict[str, threading.Event] = {
            "a_inside": threading.Event(),
            "a_can_exit": threading.Event(),
            "b_started": threading.Event(),
        }

        def worker_a() -> None:
            with q.submit("session-a", task_id="T-1"):
                inside_events["a_inside"].set()
                order.append("a-start")
                # Wait until thread B has definitely enqueued and is
                # blocked behind A before A releases the lock.
                assert inside_events["b_started"].wait(timeout=2.0)
                # Give B a moment to try (and fail) to overtake.
                inside_events["a_can_exit"].wait(timeout=0.1)
                order.append("a-end")

        def worker_b() -> None:
            inside_events["b_started"].set()
            with q.submit("session-b", task_id="T-2"):
                order.append("b-start")
                order.append("b-end")

        ta = threading.Thread(target=worker_a)
        tb = threading.Thread(target=worker_b)
        ta.start()
        # Ensure A has reached the head of the queue and acquired merge_lock
        # before B enqueues — this is what pins down "FIFO by enqueue order".
        assert inside_events["a_inside"].wait(timeout=2.0)
        tb.start()
        inside_events["a_can_exit"].set()
        ta.join(timeout=5.0)
        tb.join(timeout=5.0)
        assert not ta.is_alive()
        assert not tb.is_alive()
        # A started and finished before B started — serialized.
        assert order == ["a-start", "a-end", "b-start", "b-end"]

    def test_submit_fifo_preserved_with_many_waiters(self) -> None:
        """When N threads enqueue in order s0..sN-1, they execute in that order."""
        q = MergeQueue()
        start_barrier = threading.Event()
        executed: list[str] = []
        executed_lock = threading.Lock()
        n = 5

        # Gate thread-submit ordering: only enqueue after the previous
        # thread has enqueued, so FIFO enqueue order is deterministic.
        enqueue_gates = [threading.Event() for _ in range(n)]
        enqueue_gates[0].set()  # first thread can enqueue immediately

        def worker(i: int) -> None:
            enqueue_gates[i].wait(timeout=2.0)
            # Enqueue in strict order; allow next thread to enqueue after us.
            start_barrier.wait(timeout=2.0)
            with q.submit(f"s{i}", task_id=f"T-{i}"):
                with executed_lock:
                    executed.append(f"s{i}")
                # Let next thread enqueue as soon as we've reached the head
                # (enqueue happens at submit() entry, before the lock grab).
                if i + 1 < n:
                    enqueue_gates[i + 1].set()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        # Release the barrier so the first waiter enqueues.
        start_barrier.set()
        for t in threads:
            t.join(timeout=5.0)
            assert not t.is_alive()

        assert executed == [f"s{i}" for i in range(n)]
        # Queue drained.
        assert len(q) == 0
        assert not q.merge_lock.locked()

    def test_submit_releases_lock_on_exception(self) -> None:
        """If the caller raises inside submit(), the lock is released and the
        job is removed from the queue.
        """
        q = MergeQueue()
        with contextlib.suppress(RuntimeError):
            with q.submit("s1", task_id="T-1"):
                raise RuntimeError("boom")
        assert len(q) == 0
        assert not q.merge_lock.locked()
        # Second submit() still works.
        with q.submit("s2", task_id="T-2") as job:
            assert job.session_id == "s2"
