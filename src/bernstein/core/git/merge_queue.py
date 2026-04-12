"""FIFO merge queue for serialized branch merging with conflict routing.

Ensures only one git merge runs at a time and provides a queue structure
for processing agent branches in completion order.  Conflict routing
(creating resolver tasks) is handled by the orchestrator after dequeuing.

Pre-merge conflict detection uses ``git merge-tree`` so the working tree and
index are never touched during the check.
"""

from __future__ import annotations

import collections
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.git_basic import run_git

if TYPE_CHECKING:
    from pathlib import Path

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


@dataclass
class ConflictCheckResult:
    """Outcome of a pre-merge conflict check via ``git merge-tree``.

    Attributes:
        has_conflicts: True when merge-tree detected at least one conflict.
        conflicting_files: File paths that would conflict (empty when clean).
        branch: The feature branch that was checked.
        base: The target branch being merged into.
    """

    has_conflicts: bool
    conflicting_files: list[str]
    branch: str
    base: str


# ---------------------------------------------------------------------------
# merge-tree output parsing
# ---------------------------------------------------------------------------

_SECTION_HEADER_RE = re.compile(
    r"^(changed in both|added in both|both added|both removed|removed in both)",
    re.IGNORECASE,
)
_DESCRIPTOR_RE = re.compile(r"^[ \t]+(?:base|our|their)[ \t]+\d+[ \t]+[0-9a-f]+[ \t]+(.+)$")
_CONFLICT_MARKER = "<<<<<<< "


def _parse_merge_tree_conflicts(output: str) -> list[str]:
    """Extract conflicting file paths from ``git merge-tree`` output.

    The old-style ``git merge-tree <base> <ours> <theirs>`` command writes
    sections for each changed file.  A section that contains a conflict
    marker (``<<<<<<< .our``) has a real conflict.  File paths appear on the
    ``base``/``our``/``their`` descriptor lines that open each section.

    Args:
        output: Raw stdout from ``git merge-tree <base> <ours> <theirs>``.

    Returns:
        Deduplicated list of file paths where conflicts were found.
    """
    if _CONFLICT_MARKER not in output:
        return []

    files: list[str] = []
    seen: set[str] = set()
    current_paths: set[str] = set()
    current_has_conflict = False

    for line in output.splitlines():
        if _SECTION_HEADER_RE.match(line):
            # Finalize the previous section before starting a new one.
            if current_has_conflict:
                for p in sorted(current_paths):
                    if p not in seen:
                        seen.add(p)
                        files.append(p)
            current_paths = set()
            current_has_conflict = False
        else:
            m = _DESCRIPTOR_RE.match(line)
            if m:
                current_paths.add(m.group(1).strip())
            elif _CONFLICT_MARKER in line:
                current_has_conflict = True

    # Finalize the last section.
    if current_has_conflict:
        for p in sorted(current_paths):
            if p not in seen:
                seen.add(p)
                files.append(p)

    return files


# ---------------------------------------------------------------------------
# Public conflict-detection API
# ---------------------------------------------------------------------------


def detect_merge_conflicts(branch: str, base: str, cwd: Path) -> ConflictCheckResult:
    """Pre-flight conflict check using ``git merge-tree`` (no working-tree changes).

    Simulates a 3-way merge without touching the working tree or index.
    Returns which files would conflict if the merge were attempted now.

    Steps:

    1. ``git merge-base <base> <branch>`` — find the common ancestor.
    2. ``git merge-tree <ancestor> <base> <branch>`` — simulate the merge.
    3. Parse the output for conflict markers.

    When ``merge-base`` fails (e.g. unrelated histories) the function returns
    a clean result rather than blocking the queue.

    Args:
        branch: Feature branch to check (e.g. ``"agent/backend-abc123"``).
        base: Target branch being merged into (e.g. ``"main"``).
        cwd: Repository root.

    Returns:
        :class:`ConflictCheckResult` with conflict status and file list.
    """
    base_r = run_git(["merge-base", base, branch], cwd)
    if not base_r.ok:
        logger.warning(
            "detect_merge_conflicts: merge-base failed for %s..%s: %s",
            base,
            branch,
            base_r.stderr.strip(),
        )
        return ConflictCheckResult(has_conflicts=False, conflicting_files=[], branch=branch, base=base)

    merge_base = base_r.stdout.strip()

    tree_r = run_git(["merge-tree", merge_base, base, branch], cwd)
    conflicting_files = _parse_merge_tree_conflicts(tree_r.stdout)

    if conflicting_files:
        logger.info(
            "detect_merge_conflicts: %d conflict(s) in %s → %s: %s",
            len(conflicting_files),
            branch,
            base,
            ", ".join(conflicting_files),
        )
    else:
        logger.debug(
            "detect_merge_conflicts: no conflicts for %s → %s",
            branch,
            base,
        )

    return ConflictCheckResult(
        has_conflicts=bool(conflicting_files),
        conflicting_files=conflicting_files,
        branch=branch,
        base=base,
    )


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
