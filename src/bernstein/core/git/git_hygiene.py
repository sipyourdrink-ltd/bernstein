"""Git hygiene — mechanical cleanup of worktrees, branches, and stale state.

This module handles ONLY the safe, mechanical operations:
- Removing stale worktree directories
- Deleting merged agent branches
- Pruning git worktree registry
- Cleaning stale PID files

It does NOT commit, merge, or push — those decisions require an intelligent
agent that can review diffs and make judgment calls. The orchestrator spawns
a dedicated "hygiene" task for that when needed.

Usage::

    from bernstein.core.git.git_hygiene import run_hygiene

    # On startup: clean stale state from prior crashed runs
    run_hygiene(workdir, full=True)

    # Periodically: quick cleanup of accumulated worktrees
    run_hygiene(workdir)
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
import subprocess
import sys
import time
from typing import TYPE_CHECKING

from bernstein.core.git.git_basic import run_git

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)


def _rmtree_windows_safe(path: Path, max_attempts: int = 3) -> bool:
    """Remove a directory tree with Windows file-lock handling.

    On Windows, files may be locked by processes that haven't fully exited,
    antivirus scanning, or editor file watchers. This function:
    1. Tries shutil.rmtree with permission override
    2. Retries with delays for transient locks
    3. Falls back to PowerShell Remove-Item -Force

    Args:
        path: Directory to remove.
        max_attempts: Number of retry attempts (default 3).

    Returns:
        True if the directory was removed, False otherwise.
    """
    if not path.exists():
        return True

    def _onerror(func: Callable[[str], object], fpath: str, exc_info: object) -> None:
        """Handle permission errors by making file writable and retrying."""
        try:
            os.chmod(fpath, stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            func(fpath)
        except OSError:
            pass  # Give up on this file

    is_windows = sys.platform == "win32"
    attempts = max_attempts if is_windows else 1

    if _rmtree_with_retries(path, _onerror, attempts):
        return True

    if is_windows and path.exists() and _rmtree_powershell_fallback(path):
        return True

    if path.exists():
        logger.warning("Failed to remove %s after %d attempts", path, attempts)
        return False
    return True


def _rmtree_with_retries(
    path: Path,
    onerror: Callable[[Callable[[str], object], str, object], None],
    attempts: int,
) -> bool:
    """Try shutil.rmtree up to *attempts* times, sleeping between retries."""
    for attempt in range(attempts):
        try:
            shutil.rmtree(path, onerror=onerror)
            return True
        except OSError as exc:
            if attempt < attempts - 1:
                time.sleep(1.0)
                logger.debug("Retry %d/%d removing %s: %s", attempt + 1, attempts, path, exc)
    return False


def _rmtree_powershell_fallback(path: Path) -> bool:
    """Last-resort removal via PowerShell on Windows."""
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Remove-Item -LiteralPath '{path}' -Recurse -Force -ErrorAction SilentlyContinue",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return not path.exists()
    except Exception as exc:
        logger.debug("PowerShell Remove-Item failed for %s: %s", path, exc)
        return False


DEFAULT_TARGET_BRANCH = "main"


def run_hygiene(
    workdir: Path,
    *,
    full: bool = False,
    target_branch: str = DEFAULT_TARGET_BRANCH,
    active_session_ids: frozenset[str] | set[str] | None = None,
    force_unmerged: bool = False,
) -> dict[str, int]:
    """Run git hygiene checks and cleanup.

    Args:
        workdir: Repository root.
        full: If True, run all checks (shutdown mode).
              If False, only quick checks (periodic mode).
        target_branch: Branch that agent work must be merged into before a
            local agent/* branch can be safely deleted. Defaults to ``main``.
        active_session_ids: Session identifiers whose branches must be
            preserved regardless of merge status (branches currently being
            committed to by live agents).
        force_unmerged: **Dangerous.** When True, delete agent branches even
            if they are not merged into ``target_branch``. This is a
            privileged opt-in — automated periodic/bootstrap/shutdown
            cleanup must never set it. Only an explicit CLI command with a
            ``--force`` flag should pass ``True``.

    Returns:
        Dict with counts: worktrees_cleaned, branches_deleted, stash_dropped,
        branches_skipped (new: unmerged branches left alone for safety).
    """
    stats: dict[str, int] = {
        "worktrees_cleaned": 0,
        "branches_deleted": 0,
        "branches_skipped": 0,
        "stash_dropped": 0,
    }

    # 1. Clean stale worktrees
    stats["worktrees_cleaned"] = _clean_stale_worktrees(workdir)

    # 2. Delete merged agent branches (preserves unmerged work by default)
    deleted, skipped = _delete_merged_agent_branches(
        workdir,
        target_branch=target_branch,
        active_session_ids=active_session_ids or frozenset(),
        force_unmerged=force_unmerged,
    )
    stats["branches_deleted"] = deleted
    stats["branches_skipped"] = skipped

    # 3. Prune git worktree registry
    run_git(["worktree", "prune"], workdir, timeout=10)

    if full:
        # 4. Drop stale stashes (shutdown only)
        stats["stash_dropped"] = _drop_stale_stashes(workdir)

        # 5. Clean stale runtime state
        _clean_stale_runtime(workdir)

    total = sum(stats.values())
    if total > 0:
        logger.info(
            "Git hygiene: cleaned %d worktree(s), %d branch(es) deleted, "
            "%d branch(es) preserved (unmerged), %d stash(es)",
            stats["worktrees_cleaned"],
            stats["branches_deleted"],
            stats["branches_skipped"],
            stats["stash_dropped"],
        )

    return stats


def _clean_stale_worktrees(workdir: Path) -> int:
    """Remove worktree directories that aren't tracked by git."""
    worktree_dir = workdir / ".sdd" / "worktrees"
    if not worktree_dir.exists():
        return 0

    # Get list of git-tracked worktrees
    result = run_git(["worktree", "list", "--porcelain"], workdir, timeout=10)
    tracked_paths: set[str] = set()
    if result.ok:
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                tracked_paths.add(line.split(" ", 1)[1])

    cleaned = 0
    for entry in worktree_dir.iterdir():
        if not entry.is_dir():
            continue
        if str(entry) not in tracked_paths:  # noqa: SIM102
            # Stale directory — not tracked by git
            if _rmtree_windows_safe(entry):
                cleaned += 1
                logger.debug("Removed stale worktree dir: %s", entry.name)

    return cleaned


def _is_branch_merged(workdir: Path, branch: str, target_branch: str) -> bool:
    """Return True when *branch* is fully contained in *target_branch*.

    Uses ``git merge-base --is-ancestor`` which reports success (exit 0)
    only when every commit on *branch* is reachable from *target_branch* —
    i.e. there is no unmerged work that would be lost by deleting *branch*.

    Args:
        workdir: Repository root.
        branch: Candidate branch name (e.g. ``agent/abc123``).
        target_branch: Integration branch (usually ``main``).

    Returns:
        True iff *branch* is an ancestor of *target_branch*; False when the
        branch still has unique commits or the check could not be run
        (missing target, transient git failure). When in doubt we return
        False — callers must preserve the branch on uncertainty.
    """
    result = run_git(
        ["merge-base", "--is-ancestor", branch, target_branch],
        workdir,
        timeout=10,
    )
    return result.ok


def _session_id_from_branch(branch: str) -> str:
    """Extract the session id suffix from an ``agent/<session>`` branch.

    Args:
        branch: Branch name.

    Returns:
        Portion after the ``agent/`` prefix, or the original branch when it
        does not match the expected layout.
    """
    prefix = "agent/"
    return branch[len(prefix) :] if branch.startswith(prefix) else branch


def _delete_merged_agent_branches(
    workdir: Path,
    *,
    target_branch: str = DEFAULT_TARGET_BRANCH,
    active_session_ids: frozenset[str] | set[str] = frozenset(),
    force_unmerged: bool = False,
) -> tuple[int, int]:
    """Delete local ``agent/*`` branches that are safe to remove.

    A branch is only deleted when ALL of the following hold:

    1. Its session id is NOT in *active_session_ids* (no live agent is
       committing to it).
    2. Every commit on it is already reachable from *target_branch* (i.e.
       ``git merge-base --is-ancestor`` succeeds) — unless *force_unmerged*
       is explicitly True.

    Args:
        workdir: Repository root.
        target_branch: Branch to check ancestry against. Defaults to
            :data:`DEFAULT_TARGET_BRANCH`.
        active_session_ids: Session identifiers whose branches must not be
            touched because live agents are still using them.
        force_unmerged: **Dangerous.** Skip the merge-ancestry check and
            delete unmerged branches too. Reserved for privileged CLI paths
            that explicitly opt in; automated cleanup MUST leave this False.

    Returns:
        ``(deleted_count, skipped_count)`` — skipped counts branches that
        were preserved because they were unmerged or in use.
    """
    result = run_git(["branch", "--list", "agent/*"], workdir, timeout=10)
    if not result.ok or not result.stdout.strip():
        return 0, 0

    deleted = 0
    skipped = 0
    for line in result.stdout.strip().splitlines():
        branch = line.strip().lstrip("* ")
        if not branch.startswith("agent/"):
            continue

        # Guard 1: live agents own these branches — never touch them.
        session_id = _session_id_from_branch(branch)
        if session_id in active_session_ids:
            logger.info(
                "Preserving agent branch %s — session %s is active",
                branch,
                session_id,
            )
            skipped += 1
            continue

        # Guard 2: only delete fully-merged branches unless force was given.
        merged = _is_branch_merged(workdir, branch, target_branch)
        if not merged and not force_unmerged:
            logger.warning(
                "Preserving unmerged agent branch %s — not an ancestor of %s; "
                "pass force_unmerged=True via a privileged CLI path to override",
                branch,
                target_branch,
            )
            skipped += 1
            continue

        del_args = ["branch", "-D"] if (force_unmerged and not merged) else ["branch", "-d"]
        del_result = run_git([*del_args, branch], workdir, timeout=10)
        if del_result.ok:
            deleted += 1
            if merged:
                logger.info("Deleted merged agent branch: %s", branch)
            else:
                logger.warning(
                    "Force-deleted UNMERGED agent branch %s (force_unmerged=True)",
                    branch,
                )
        else:
            logger.warning(
                "Failed to delete agent branch %s: %s",
                branch,
                del_result.stderr.strip() or del_result.stdout.strip(),
            )
            skipped += 1

    return deleted, skipped


def _drop_stale_stashes(workdir: Path) -> int:
    """Drop all git stashes (agent work should be committed, not stashed)."""
    result = run_git(["stash", "list"], workdir, timeout=10)
    if not result.ok or not result.stdout.strip():
        return 0

    count = len(result.stdout.strip().splitlines())
    if count > 0:
        run_git(["stash", "clear"], workdir, timeout=10)
        logger.debug("Dropped %d stash(es)", count)
    return count


def _clean_stale_runtime(workdir: Path) -> None:
    """Remove stale PID files and agent state from prior crashed runs."""
    runtime = workdir / ".sdd" / "runtime"
    if not runtime.exists():
        return

    # Remove stale PID files
    pids_dir = runtime / "pids"
    if pids_dir.exists():
        for pid_file in pids_dir.glob("*.pid"):
            with contextlib.suppress(OSError):
                pid_file.unlink()

    # Remove stale agents.json (will be recreated)
    agents_json = runtime / "agents.json"
    if agents_json.exists():
        with contextlib.suppress(OSError):
            agents_json.unlink()
