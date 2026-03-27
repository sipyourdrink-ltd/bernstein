"""FIFO merge queue for serialized branch merging with conflict routing.

Ensures only one git merge runs at a time and provides a queue structure
for processing agent branches in completion order.  Conflict routing
(creating resolver tasks) is handled by the orchestrator after dequeuing.
"""

from __future__ import annotations

import collections
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MergeJob:
    """A pending merge job waiting in the queue.

    Attributes:
        session_id: The agent session whose branch should be merged.
        task_id: The task the agent was working on.
        task_title: Human-readable task title (used in conflict task body).
        branch_name: Full branch name (agent/{session_id}).
    """

    session_id: str
    task_id: str
    task_title: str = ""
    branch_name: str = field(init=False)

    def __post_init__(self) -> None:
        self.branch_name = f"agent/{self.session_id}"


class MergeQueue:
    """Thread-safe FIFO queue for serializing branch merges.

    Guarantees that only one git merge runs at a time, preventing
    concurrent merges that could cause conflicts between agent branches.
    Jobs are processed in FIFO order — first-completed agent merges first.

    The queue also exposes a ``merge_lock`` that callers can acquire
    directly when processing a job dequeued outside this class.

    Usage::

        queue = MergeQueue()
        queue.enqueue("backend-abc123", task_id="t1", task_title="Fix auth")

        with queue.merge_lock:
            job = queue.dequeue()
            if job:
                result = spawner._merge_worktree_branch(job.session_id)
                # handle result ...
    """

    def __init__(self) -> None:
        self._queue: collections.deque[MergeJob] = collections.deque()
        self._queue_lock = threading.Lock()
        # Held during each git merge operation so concurrent callers block.
        self.merge_lock = threading.Lock()

    def enqueue(self, session_id: str, task_id: str, task_title: str = "") -> None:
        """Add a merge job to the end of the queue.

        Args:
            session_id: The agent session whose branch to merge.
            task_id: The task the agent was working on.
            task_title: Human-readable task title (for conflict task body).
        """
        job = MergeJob(session_id=session_id, task_id=task_id, task_title=task_title)
        with self._queue_lock:
            self._queue.append(job)
        logger.debug(
            "MergeQueue: enqueued session %s (task %s), depth=%d",
            session_id,
            task_id,
            len(self),
        )

    def dequeue(self) -> MergeJob | None:
        """Remove and return the oldest job, or None if the queue is empty.

        Returns:
            The oldest MergeJob or None.
        """
        with self._queue_lock:
            return self._queue.popleft() if self._queue else None

    def peek(self) -> MergeJob | None:
        """Return the oldest job without removing it, or None if empty.

        Returns:
            The oldest MergeJob or None.
        """
        with self._queue_lock:
            return self._queue[0] if self._queue else None

    def snapshot(self) -> dict[str, Any]:
        """Return current queue state as a serialisable dict.

        Includes all pending jobs, the current queue depth, and whether a
        merge operation is currently in progress (``merge_lock`` is held).

        Returns:
            Dict with keys ``jobs`` (list[dict]), ``depth`` (int), and
            ``is_merging`` (bool).
        """
        with self._queue_lock:
            jobs: list[dict[str, str]] = [
                {
                    "session_id": job.session_id,
                    "task_id": job.task_id,
                    "task_title": job.task_title,
                    "branch_name": job.branch_name,
                }
                for job in self._queue
            ]
        return {
            "jobs": jobs,
            "depth": len(jobs),
            "is_merging": self.merge_lock.locked(),
        }

    def __len__(self) -> int:
        with self._queue_lock:
            return len(self._queue)

    def __bool__(self) -> bool:
        return len(self) > 0
