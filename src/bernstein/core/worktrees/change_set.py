"""Resolve the exact set of paths one task changed.

"Undo this task" needs a definition of *this task's change* that does not
depend on guessing. Two obvious sources cannot supply one:

* **Commit subjects.** ``bernstein undo`` matches ``task:<task_id>``
  against recent subjects, but nothing in the tree writes that string -
  agent commits read ``[WIP] <title>`` and ``feat: <summary>`` - so the
  scan answers "nothing to undo" for every real task.
* **The lineage spine.** A spine entry carries ``artifact_path``,
  ``content_hash``, ``actor`` and ``step_id`` and no task id, and writes
  performed by a CLI adapter's own subprocess never reach that boundary
  at all.

The per-task worktree does know. Every task runs on its own
``agent/<session_id>`` branch, and the session-to-task binding is already
recorded in ``.sdd/runtime/pids/<session_id>.json`` and surfaced by
:func:`~bernstein.core.worktrees.classifier.classify_worktrees`. Diffing
that branch against the integration branch **three-dot** - from where the
task forked, not from where the integration branch now stands - yields
exactly the paths the task itself changed.

The three dots are load-bearing. Two-dot ``main..agent/<sid>`` reports
every path that landed on ``main`` after the task forked as a *deletion*
the task never made; a reversal built on that set would restore files the
task never removed and still report a clean revert.

This module is read-only. It resolves and returns the change set; it does
not revert anything.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.git.git_basic import GitResult, run_git
from bernstein.core.worktrees.classifier import (
    DEFAULT_INTEGRATION_BRANCH,
    classify_worktrees,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "TaskChangePath",
    "TaskChangeSet",
    "TaskChangeSetUnresolved",
    "resolve_task_change_set",
]

#: Seconds allowed for each read-only git call. Mirrors the budget
#: ``spawner_merge._incoming_files`` gives the equivalent diff.
_GIT_TIMEOUT_S = 30

#: ``git diff --raw`` status letter to the word this module reports.
_CHANGE_TYPES = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "T": "typechange",
}


class TaskChangeSetUnresolved(RuntimeError):
    """The set of paths a task changed could not be determined.

    Raised rather than collapsed into an empty change set. An empty set is
    a real answer - a task that touched no files - so returning one for a
    task whose worktree cannot be found, or whose branch git could not
    diff, would make "we could not look" indistinguishable from "there is
    nothing there". A reversal driven by the second reading would report
    success having reverted nothing.
    """


@dataclass(frozen=True, slots=True)
class TaskChangePath:
    """One path the task changed, with the blobs on either side of it.

    Attributes:
        path: Repo-relative path, exactly as git names it.
        change_type: ``added`` / ``modified`` / ``deleted`` /
            ``typechange``, or the lowercased git status letter for a
            status this module does not name.
        pre_hash: Blob hash the path held before the task, or ``None``
            when the task added it.
        post_hash: Blob hash the path holds on the task branch, or
            ``None`` when the task deleted it.
    """

    path: str
    change_type: str
    pre_hash: str | None
    post_hash: str | None


@dataclass(frozen=True, slots=True)
class TaskChangeSet:
    """Everything one task changed, resolved from its worktree branch.

    Attributes:
        task_id: Task the set belongs to.
        session_id: Session that ran it - the worktree slug and the
            ``agent/<session_id>`` branch suffix.
        branch: Branch the task's commits live on.
        integration_branch: Branch the task branch was diffed against.
        merge_base: Commit the task forked from; the pre-change side of
            every path below.
        paths: The changed paths in git's path order.
    """

    task_id: str
    session_id: str
    branch: str
    integration_branch: str
    merge_base: str
    paths: tuple[TaskChangePath, ...]


def resolve_task_change_set(
    repo_root: Path,
    task_id: str,
    *,
    integration_branch: str = DEFAULT_INTEGRATION_BRANCH,
) -> TaskChangeSet:
    """Return the set of paths ``task_id`` changed, with blob hashes.

    Args:
        repo_root: Absolute repository root.
        task_id: Task whose change set to resolve.
        integration_branch: Branch the task branch is diffed against.
            Defaults to :data:`DEFAULT_INTEGRATION_BRANCH`.

    Returns:
        A :class:`TaskChangeSet`; ``paths`` is empty only when the task's
        branch genuinely changed nothing.

    Raises:
        TaskChangeSetUnresolved: No worktree records this task, more than
            one does, or git could not diff the task's branch.
    """
    session_id = _session_for_task(repo_root, task_id)
    branch = f"agent/{session_id}"
    merge_base = _merge_base(repo_root, integration_branch, branch, task_id=task_id)
    paths = _diff_raw(repo_root, merge_base, branch, task_id=task_id)
    return TaskChangeSet(
        task_id=task_id,
        session_id=session_id,
        branch=branch,
        integration_branch=integration_branch,
        merge_base=merge_base,
        paths=paths,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _session_for_task(repo_root: Path, task_id: str) -> str:
    """Return the session id whose worktree carries ``task_id``.

    Raises:
        TaskChangeSetUnresolved: Zero or several worktrees claim the task.
            Two worktrees for one task is a binding we cannot arbitrate,
            and picking either would attribute one session's files to the
            other's reversal.
    """
    matches = [row for row in classify_worktrees(repo_root) if row.task_id == task_id]
    if not matches:
        msg = f"no worktree under {repo_root}/.sdd records task {task_id!r}; its change set cannot be resolved"
        raise TaskChangeSetUnresolved(msg)
    if len(matches) > 1:
        sessions = ", ".join(sorted(row.session_id for row in matches))
        msg = f"task {task_id!r} is claimed by several worktrees ({sessions}); refusing to guess"
        raise TaskChangeSetUnresolved(msg)
    return matches[0].session_id


def _merge_base(repo_root: Path, integration_branch: str, branch: str, *, task_id: str) -> str:
    """Return the commit ``branch`` forked from ``integration_branch``."""
    result = _git(repo_root, ["merge-base", integration_branch, branch], task_id=task_id)
    base = result.stdout.strip()
    if not base:
        msg = (
            f"task {task_id!r}: {branch} and {integration_branch} share no merge base, "
            "so what the task changed cannot be separated from what it inherited"
        )
        raise TaskChangeSetUnresolved(msg)
    return base


def _diff_raw(
    repo_root: Path,
    merge_base: str,
    branch: str,
    *,
    task_id: str,
) -> tuple[TaskChangePath, ...]:
    """Return the changed paths between ``merge_base`` and ``branch``.

    ``--no-renames`` because rename detection reports only a rename's
    destination, and a reversal has to restore the source path too.
    ``--abbrev=64`` asks for more hexdigits than any hash git uses, which
    git clamps to the full object name - so the hashes are never
    abbreviated prefixes that could collide.
    """
    result = _git(
        repo_root,
        ["diff", "--raw", "--no-renames", "-z", "--abbrev=64", f"{merge_base}..{branch}"],
        task_id=task_id,
    )
    fields = result.stdout.split("\0")
    paths: list[TaskChangePath] = []
    index = 0
    while index < len(fields):
        meta = fields[index]
        if not meta:
            index += 1
            continue
        if index + 1 >= len(fields):
            msg = f"task {task_id!r}: git diff --raw record {meta!r} has no path"
            raise TaskChangeSetUnresolved(msg)
        paths.append(_parse_record(meta, fields[index + 1], task_id=task_id))
        index += 2
    return tuple(paths)


def _parse_record(meta: str, path: str, *, task_id: str) -> TaskChangePath:
    """Parse one ``:<mode> <mode> <sha> <sha> <status>`` raw-diff record."""
    parts = meta.lstrip(":").split(" ")
    if len(parts) != 5 or not meta.startswith(":"):
        msg = f"task {task_id!r}: unparsable git diff --raw record {meta!r}"
        raise TaskChangeSetUnresolved(msg)
    _pre_mode, _post_mode, pre_sha, post_sha, status = parts
    return TaskChangePath(
        path=path,
        change_type=_CHANGE_TYPES.get(status, status.lower()),
        pre_hash=_blob_or_none(pre_sha),
        post_hash=_blob_or_none(post_sha),
    )


def _blob_or_none(sha: str) -> str | None:
    """Return ``sha``, or ``None`` for git's all-zero "no blob" sentinel."""
    if not sha or sha.strip("0") == "":
        return None
    return sha


def _git(repo_root: Path, args: list[str], *, task_id: str) -> GitResult:
    """Run a read-only git command, raising on failure or timeout.

    Every git call here answers a question the caller then treats as
    fact, so a non-zero exit must not degrade to an empty answer.
    """
    try:
        result = run_git(args, repo_root, timeout=_GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        msg = f"task {task_id!r}: git {' '.join(args)} did not complete ({exc})"
        raise TaskChangeSetUnresolved(msg) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        msg = f"task {task_id!r}: git {' '.join(args)} failed ({detail})"
        raise TaskChangeSetUnresolved(msg)
    return result
