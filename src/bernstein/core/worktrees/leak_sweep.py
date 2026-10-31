"""List worktrees whose owning task is confirmed terminal (#5112, slice 1 half).

:func:`~bernstein.core.worktrees.classifier.classify_worktrees` decides
``ORPHAN``/``STALE``/``CORRUPT``/``ACTIVE`` from process liveness and trace-file
age -- a worker whose PID is still alive (or was, recently, per the trace)
classifies as ``ACTIVE`` regardless of whether the task it was running has
actually finished. A PID can outlive its task: reparented, wedged in cleanup,
or simply slow to exit after its last write. This sweep asks a different,
authoritative question -- what does the task log say -- and lists a worktree
whenever the answer is "done," independent of what the liveness heuristic
concluded.

This is a **listing**, not a reap decision: it does not read or change
``is_reapable``, and it never touches the filesystem. Folding the task-record
check into the reap gate itself (or leaving the two independent) is an open
question the issue raises explicitly and is left for a follow-up once an
operator has seen what this reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.worktrees.classifier import ClassifiedWorktree, classify_worktrees
from bernstein.core.worktrees.task_status import read_task_statuses, status_is_terminal

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class LeakedWorktree:
    """A worktree the task log says is done, regardless of process liveness.

    Attributes:
        worktree: The classifier's own row for this directory.
        task_status: The terminal status the task log recorded.
    """

    worktree: ClassifiedWorktree
    task_status: str


def sweep_leaked_worktrees(
    repo_root: Path,
    *,
    now: float | None = None,
) -> list[LeakedWorktree]:
    """Return every worktree whose owning task's recorded status is terminal.

    A worktree with no task id (``ORPHAN``, or a ``CORRUPT`` directory) is
    never listed here -- there is no task record to check, and that gap is
    exactly what the liveness heuristic already covers for those states.

    Args:
        repo_root: Absolute path to the repository root.
        now: Override the wall-clock for tests; forwarded to
            :func:`classify_worktrees`.

    Returns:
        One :class:`LeakedWorktree` per worktree whose task id resolves to a
        terminal status in the on-disk task log, sorted by session id -- a
        guarantee this function makes itself (not merely inherited from
        :func:`classify_worktrees`'s own directory-listing order).
    """
    statuses = read_task_statuses(repo_root / ".sdd")
    leaked: list[LeakedWorktree] = []
    for worktree in classify_worktrees(repo_root, now=now):
        if not worktree.task_id:
            continue
        status = statuses.get(worktree.task_id)
        # status_is_terminal's None case (no log entry for this task) and its
        # False case (a real, recorded, non-terminal status) both mean "do
        # not list" here -- the three-valued distinction matters for a
        # future reap gate, not for this listing, which reuses the same
        # predicate a per-task lookup would rather than re-deriving it. The
        # `status is not None` clause is what a strict type check needs to
        # narrow `status: str | None` to `str` for the dataclass below;
        # `status_is_terminal` returning `True` already guarantees it at
        # runtime, but not in a way a checker can see through the call.
        if status is not None and status_is_terminal(status) is True:
            leaked.append(LeakedWorktree(worktree=worktree, task_status=status))
    leaked.sort(key=lambda entry: entry.worktree.session_id)
    return leaked


__all__ = ["LeakedWorktree", "sweep_leaked_worktrees"]
