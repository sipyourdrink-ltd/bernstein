"""Spawner-merge integration tests for audit-091.

Verifies that the spawner merge path routes through
:class:`bernstein.core.merge_queue.MergeQueue` when one is provided so
concurrent agent merges serialise in strict FIFO order and are observable
via the queue snapshot.  When no queue is provided the legacy per-repo
:class:`threading.Lock` path is still exercised (backward compatibility
for single-agent runs and bare unit tests).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bernstein.core.merge_queue import MergeQueue

from bernstein.core.agents.spawner_merge import _do_merge


def _make_session(session_id: str, task_id: str = "T-1") -> Any:
    """Build a minimal duck-typed AgentSession stub for _do_merge."""

    class _Stub:
        pass

    s = _Stub()
    s.id = session_id
    s.task_ids = [task_id]
    return s


class _RecordingMergeFn:
    """Callable that records merge invocations in-order and can pause.

    The pause lets us freeze one merge in-flight while another thread
    enqueues, so we can observe the MergeQueue state transitions.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.in_flight = threading.Event()
        self.release = threading.Event()

    def __call__(self, session_id: str, repo_root: Path) -> Any:
        self.calls.append(session_id)
        self.in_flight.set()
        # Wait for the test to release us (bounded so a hang fails fast).
        self.release.wait(timeout=2.0)

        class _Result:
            success = True
            conflicting_files: list[str] = []
            error = None

        return _Result()


def test_do_merge_with_queue_enqueues_and_processes() -> None:
    """With a MergeQueue provided, _do_merge enqueues the job and the
    queue snapshot reflects it while the merge runs.
    """
    q = MergeQueue()
    session = _make_session("backend-abc", task_id="T-42")
    fn = _RecordingMergeFn()

    # Patch safe_push so _do_merge doesn't touch git.
    with patch("bernstein.core.git_ops.safe_push") as safe_push:
        safe_push.return_value.ok = True
        safe_push.return_value.stderr = ""

        started = threading.Event()

        def run() -> None:
            started.set()
            _do_merge(session, Path("/tmp"), {}, fn, merge_queue=q)

        t = threading.Thread(target=run)
        t.start()
        # Wait until the merge function is inside its critical section.
        assert fn.in_flight.wait(timeout=2.0)

        snap = q.snapshot()
        # While the merge is held, the job is still in the queue (submit()
        # only pops it on exit) and is_merging should be True.
        assert snap["depth"] == 1
        assert snap["jobs"][0]["session_id"] == "backend-abc"
        assert snap["is_merging"] is True

        # Release and let the worker finish.
        fn.release.set()
        t.join(timeout=5.0)
        assert not t.is_alive()

    assert fn.calls == ["backend-abc"]
    # Queue drained and lock released.
    assert q.snapshot()["depth"] == 0
    assert not q.merge_lock.locked()


def test_do_merge_without_queue_uses_legacy_lock_path() -> None:
    """Backwards-compat: _do_merge still works when no queue is provided.

    Uses the per-repo ``merge_locks`` dict so single-agent callers and
    existing tests continue to function unchanged.
    """
    session = _make_session("solo-1")
    merge_locks: dict[Path, threading.Lock] = {}

    def fake_merge(session_id: str, repo_root: Path) -> Any:
        class _Result:
            success = False  # don't trigger the push branch
            conflicting_files: list[str] = []
            error = None

        return _Result()

    result = _do_merge(session, Path("/tmp/repo"), merge_locks, fake_merge, merge_queue=None)
    assert result is not None
    assert not result.success
    # Legacy path installs a lock for the repo root.
    assert Path("/tmp/repo") in merge_locks


def test_concurrent_do_merges_serialize_via_queue_fifo() -> None:
    """Two concurrent _do_merge calls serialise through the queue in FIFO
    order (audit-091 reproduction).  Without the fix both merges would race
    on the ad-hoc per-repo lock with non-deterministic ordering and the
    queue would stay empty.
    """
    q = MergeQueue()
    completion_order: list[str] = []
    completion_lock = threading.Lock()

    # First-started fn holds the lock until we release it; second fn is fast.
    blocking_fn = _RecordingMergeFn()

    def fast_fn(session_id: str, repo_root: Path) -> Any:
        class _Result:
            success = False
            conflicting_files: list[str] = []
            error = None

        with completion_lock:
            completion_order.append(session_id)
        return _Result()

    def wrap_blocking(session_id: str, repo_root: Path) -> Any:
        r = blocking_fn(session_id, repo_root)
        with completion_lock:
            completion_order.append(session_id)
        return r

    session_a = _make_session("agent-A", task_id="T-A")
    session_b = _make_session("agent-B", task_id="T-B")

    with patch("bernstein.core.git_ops.safe_push") as safe_push:
        safe_push.return_value.ok = True
        safe_push.return_value.stderr = ""

        ta = threading.Thread(
            target=_do_merge, args=(session_a, Path("/tmp/r"), {}, wrap_blocking), kwargs={"merge_queue": q}
        )
        tb = threading.Thread(
            target=_do_merge, args=(session_b, Path("/tmp/r"), {}, fast_fn), kwargs={"merge_queue": q}
        )

        ta.start()
        # Ensure A is inside its merge before B enqueues.  This pins down
        # the enqueue order so FIFO is meaningful.
        assert blocking_fn.in_flight.wait(timeout=2.0)
        # Queue should show A pending and currently merging.
        snap = q.snapshot()
        assert snap["depth"] == 1
        assert snap["is_merging"] is True
        tb.start()
        # Give B a moment to enqueue behind A.
        time.sleep(0.05)
        snap = q.snapshot()
        # Now both A and B are queued, A at the head.
        assert snap["depth"] == 2
        assert snap["jobs"][0]["session_id"] == "agent-A"
        assert snap["jobs"][1]["session_id"] == "agent-B"

        # Release A; B should run only after A exits the submit block.
        blocking_fn.release.set()
        ta.join(timeout=5.0)
        tb.join(timeout=5.0)
        assert not ta.is_alive()
        assert not tb.is_alive()

    assert completion_order == ["agent-A", "agent-B"]
    assert q.snapshot()["depth"] == 0
