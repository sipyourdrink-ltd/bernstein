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


def run_hygiene(workdir: Path, *, full: bool = False) -> dict[str, int]:
    """Run git hygiene checks and cleanup.

    Args:
        workdir: Repository root.
        full: If True, run all checks (shutdown mode).
              If False, only quick checks (periodic mode).

    Returns:
        Dict with counts: worktrees_cleaned, branches_deleted, stash_dropped.
    """
    stats: dict[str, int] = {
        "worktrees_cleaned": 0,
        "branches_deleted": 0,
        "stash_dropped": 0,
    }

    # 1. Clean stale worktrees
    stats["worktrees_cleaned"] = _clean_stale_worktrees(workdir)

    # 2. Delete merged agent branches
    stats["branches_deleted"] = _delete_merged_agent_branches(workdir)

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
            "Git hygiene: cleaned %d worktree(s), %d branch(es), %d stash(es)",
            stats["worktrees_cleaned"],
            stats["branches_deleted"],
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


def _delete_merged_agent_branches(workdir: Path) -> int:
    """Delete local agent/* branches that have been merged or are stale."""
    result = run_git(["branch", "--list", "agent/*"], workdir, timeout=10)
    if not result.ok or not result.stdout.strip():
        return 0

    deleted = 0
    for line in result.stdout.strip().splitlines():
        branch = line.strip().lstrip("* ")
        if not branch.startswith("agent/"):
            continue
        # Force delete — these are disposable agent branches
        del_result = run_git(["branch", "-D", branch], workdir, timeout=10)
        if del_result.ok:
            deleted += 1
            logger.debug("Deleted agent branch: %s", branch)

    return deleted


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
