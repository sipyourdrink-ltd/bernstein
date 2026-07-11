"""Git-commit snapshot primitive for the worktree sandbox (issue #2295).

Snapshot and resume are ``NotImplementedError`` across every cloud
sandbox backend, which blocks any session-rewind or fork-from-step
workflow. Bernstein already isolates each task in a git worktree, and a
git commit is a cheap, content-addressed snapshot. This module turns a
worktree's working tree into a commit under a dedicated
``refs/bernstein/snapshots/<run_id>/<step_index>`` ref and restores it
into a fresh detached worktree.

Why a commit and not a stash or a tarball:

* The commit sha is content-addressed, so two byte-identical working
  trees snapshot to the same sha - the snapshot id is a fingerprint, not
  an opaque handle. That makes AC5 (a tampered ref no longer matches the
  journal-recorded sha) a plain sha comparison.
* Resume is ``git worktree add --detach <path> <sha>``, which git
  guarantees restores the tree byte-identically (AC1) into an isolated
  directory with its own index and HEAD (AC4).

The snapshot captures both tracked and untracked files: work is staged
into a scratch index (via ``GIT_INDEX_FILE`` so the live worktree index
is never touched) and written to a tree, then committed with the current
HEAD as parent so history stays walkable.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from bernstein.core.git.git_basic import run_git

logger = logging.getLogger(__name__)

#: Ref namespace for per-step worktree snapshots. Kept under
#: ``refs/bernstein/`` so it never collides with branches, tags, or the
#: ``refs/graveyard/`` rescue namespace.
SNAPSHOT_REF_PREFIX = "refs/bernstein/snapshots"

_GIT_TIMEOUT_S = 60


class SnapshotError(RuntimeError):
    """Raised when a worktree snapshot or resume operation fails."""


def snapshot_ref_name(run_id: str, step_index: int) -> str:
    """Return the ref a snapshot for ``(run_id, step_index)`` is stored at.

    Args:
        run_id: The run whose worktree is being snapshotted.
        step_index: The journal step index the snapshot pins.

    Returns:
        A fully-qualified ref name under :data:`SNAPSHOT_REF_PREFIX`.
    """
    return f"{SNAPSHOT_REF_PREFIX}/{run_id}/{step_index}"


def commit_worktree_snapshot(
    repo_root: Path,
    worktree_path: Path,
    *,
    run_id: str,
    step_index: int,
) -> str:
    """Commit *worktree_path*'s working tree and pin it to a snapshot ref.

    The full working tree (tracked, modified, and untracked files) is
    staged into a scratch index, written to a tree object, and committed
    with the worktree's current ``HEAD`` as parent. The resulting commit
    sha is stored at :func:`snapshot_ref_name` and returned as the
    snapshot id.

    Args:
        repo_root: The owning repository root (holds the object store and
            the snapshot refs).
        worktree_path: The worktree whose contents to snapshot.
        run_id: The run identifier the snapshot belongs to.
        step_index: The journal step index the snapshot pins.

    Returns:
        The 40-char commit sha stored at the snapshot ref.

    Raises:
        SnapshotError: If any underlying git command fails.
    """
    # A linked worktree's ``.git`` is a gitdir *file*, not a directory, so
    # the scratch index cannot live under it. Use a real temp file that is
    # never the live worktree index.
    index_fd, index_name = tempfile.mkstemp(prefix="bernstein-snapshot-index-")
    os.close(index_fd)
    scratch_index = Path(index_name)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(scratch_index)
    # Deterministic author/committer identity keeps the commit sha a pure
    # function of the tree + parent, so a byte-identical worktree snapshots
    # to the same sha regardless of the operator's git config.
    env.update(
        {
            "GIT_AUTHOR_NAME": "bernstein-snapshot",
            "GIT_AUTHOR_EMAIL": "snapshot@bernstein.local",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
            "GIT_COMMITTER_NAME": "bernstein-snapshot",
            "GIT_COMMITTER_EMAIL": "snapshot@bernstein.local",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
        }
    )
    try:
        # 1. Seed the scratch index from HEAD, then stage everything on top
        #    so tracked, modified, and untracked files are all captured.
        _run(["read-tree", "HEAD"], worktree_path, env)
        _run(["add", "-A"], worktree_path, env)
        tree = _run(["write-tree"], worktree_path, env).strip()
        parent = _rev_parse(worktree_path, "HEAD")
        message = f"bernstein snapshot run={run_id} step={step_index}"
        commit_args = ["commit-tree", tree, "-m", message]
        if parent is not None:
            commit_args.extend(["-p", parent])
        sha = _run(commit_args, worktree_path, env).strip()
        # 2. Pin the commit to a dedicated ref in the owning repo so the
        #    snapshot survives worktree teardown and git gc.
        ref = snapshot_ref_name(run_id, step_index)
        _run(["update-ref", ref, sha], repo_root, os.environ.copy())
    finally:
        try:
            scratch_index.unlink(missing_ok=True)
        except OSError:
            logger.debug("Failed to remove scratch snapshot index %s", scratch_index)
    return sha


def resume_worktree_snapshot(
    repo_root: Path,
    snapshot_id: str,
    dest_path: Path,
) -> None:
    """Check *snapshot_id* out into a fresh detached worktree at *dest_path*.

    Uses ``git worktree add --detach`` so the restored tree lands in an
    isolated directory with its own index and HEAD; two resumes of the
    same snapshot never share mutable state (AC4).

    Args:
        repo_root: The owning repository root.
        snapshot_id: The commit sha returned by
            :func:`commit_worktree_snapshot`.
        dest_path: Directory the snapshot should be restored into. Must
            not already exist (git refuses to overwrite it).

    Raises:
        SnapshotError: If the worktree checkout fails.
    """
    _run(
        ["worktree", "add", "--detach", str(dest_path), snapshot_id],
        repo_root,
        os.environ.copy(),
    )


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> str:
    """Run a git command, returning stdout or raising :class:`SnapshotError`."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=_GIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        raise SnapshotError(f"git {args[0]} failed: {exc}") from exc
    if result.returncode != 0:
        raise SnapshotError(f"git {args[0]} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def _rev_parse(cwd: Path, ref: str) -> str | None:
    """Resolve *ref* to a sha in *cwd*, or ``None`` when it does not resolve."""
    result = run_git(["rev-parse", "--verify", "--quiet", ref], cwd, timeout=_GIT_TIMEOUT_S)
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


__all__ = [
    "SNAPSHOT_REF_PREFIX",
    "SnapshotError",
    "commit_worktree_snapshot",
    "resume_worktree_snapshot",
    "snapshot_ref_name",
]
