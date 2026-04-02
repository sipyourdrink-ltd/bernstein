"""Worktree branch/status display for TUI agent panel."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class WorktreeStatus:
    """Worktree branch and dirty status."""

    branch: str
    is_dirty: bool
    ahead: int = 0
    behind: int = 0


def get_worktree_status(worktree_path: Path) -> WorktreeStatus | None:
    """Get worktree branch and dirty status.

    Args:
        worktree_path: Path to worktree directory.

    Returns:
        WorktreeStatus or None if git command fails.
    """
    try:
        # Get current branch
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        if branch_result.returncode != 0:
            return None

        branch = branch_result.stdout.strip()

        # Check for uncommitted changes
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        is_dirty = bool(status_result.stdout.strip())

        # Get ahead/behind count
        ahead = 0
        behind = 0
        if branch != "HEAD":
            count_result = subprocess.run(
                ["git", "rev-list", "--left-right", "--count", f"origin/{branch}...HEAD"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if count_result.returncode == 0 and count_result.stdout.strip():
                parts = count_result.stdout.strip().split()
                if len(parts) == 2:
                    behind, ahead = map(int, parts)

        return WorktreeStatus(
            branch=branch,
            is_dirty=is_dirty,
            ahead=ahead,
            behind=behind,
        )

    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return None


def format_worktree_display(status: WorktreeStatus) -> str:
    """Format worktree status for display.

    Args:
        status: WorktreeStatus instance.

    Returns:
        Formatted string like "feat/task-abc123 [dirty]" or "main [clean]".
    """
    dirty_marker = "[dirty]" if status.is_dirty else "[clean]"

    if status.ahead > 0 or status.behind > 0:
        return f"{status.branch} {dirty_marker} ({status.ahead}↑ {status.behind}↓)"
    else:
        return f"{status.branch} {dirty_marker}"
