"""WorktreeManager — git worktree lifecycle for agent session isolation.

Each spawned agent gets its own git worktree at .sdd/worktrees/{session_id}
on a branch named agent/{session_id}. This eliminates file-level conflicts
between concurrent agents working in the same repository.

Usage::

    mgr = WorktreeManager(repo_root=Path("."))
    worktree_path = mgr.create("session-abc123")
    # ... spawn agent in worktree_path ...
    mgr.cleanup("session-abc123")
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_WORKTREE_BASE = ".sdd/worktrees"


class WorktreeError(Exception):
    """Raised when a worktree operation fails irrecoverably."""


class WorktreeManager:
    """Manage per-session git worktrees for agent isolation.

    Each call to :meth:`create` produces an isolated checkout on a short-lived
    branch.  :meth:`cleanup` removes the worktree and branch.  The manager is
    intentionally thin — no state beyond the repo root; ground truth lives in
    ``git worktree list``.

    Args:
        repo_root: Absolute path to the repository root.
    """

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self._base_dir = self.repo_root / _WORKTREE_BASE

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, session_id: str) -> Path:
        """Create a git worktree for *session_id* and return its path.

        The worktree is created at ``.sdd/worktrees/{session_id}`` on branch
        ``agent/{session_id}``.  If either already exists, the method raises
        :class:`WorktreeError` so the caller can decide whether to reuse or
        fail the spawn.

        Args:
            session_id: Unique identifier for the agent session.

        Returns:
            Path to the newly-created worktree directory.

        Raises:
            WorktreeError: If the worktree or branch already exists, or if
                the ``git worktree add`` command fails for any other reason.
        """
        worktree_path = self._base_dir / session_id
        branch_name = f"agent/{session_id}"

        if worktree_path.exists():
            raise WorktreeError(
                f"Worktree path already exists: {worktree_path}. "
                "Call cleanup() first or use a unique session_id."
            )

        self._base_dir.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", branch_name],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            # Detect branch-already-exists case for a clearer message
            if "already exists" in stderr:
                raise WorktreeError(
                    f"Branch '{branch_name}' already exists. "
                    f"Delete it manually or call cleanup() first. Git: {stderr}"
                )
            raise WorktreeError(
                f"git worktree add failed for session '{session_id}': {stderr}"
            )

        logger.info("Created worktree %s (branch %s)", worktree_path, branch_name)
        return worktree_path

    def cleanup(self, session_id: str) -> None:
        """Remove the worktree and branch for *session_id*.

        Best-effort: logs warnings for individual failures but does not raise.
        Safe to call even if the worktree was never created or already cleaned.

        Args:
            session_id: The session whose worktree should be removed.
        """
        worktree_path = self._base_dir / session_id
        branch_name = f"agent/{session_id}"

        # 1. Remove the worktree (--force handles dirty state)
        try:
            result = subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning(
                    "git worktree remove failed for %s: %s",
                    session_id,
                    result.stderr.strip(),
                )
        except Exception as exc:
            logger.warning("Failed to remove worktree for %s: %s", session_id, exc)

        # 2. Delete the branch
        try:
            result = subprocess.run(
                ["git", "branch", "-D", branch_name],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    "git branch -D failed for %s: %s",
                    branch_name,
                    result.stderr.strip(),
                )
        except Exception as exc:
            logger.warning("Failed to delete branch %s: %s", branch_name, exc)

        logger.info("Cleaned up worktree for session %s", session_id)

    def list_active(self) -> list[str]:
        """Return session IDs that currently have active worktrees.

        Queries ``git worktree list`` and filters for paths under
        ``.sdd/worktrees/``.  Only the directory name (== session_id) is
        returned.

        Returns:
            List of active session IDs (may be empty).
        """
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as exc:
            logger.warning("git worktree list failed: %s", exc)
            return []

        if result.returncode != 0:
            logger.warning(
                "git worktree list returned non-zero: %s", result.stderr.strip()
            )
            return []

        session_ids: list[str] = []
        base_str = str(self._base_dir)

        for line in result.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            wt_path = line[len("worktree "):].strip()
            if wt_path.startswith(base_str):
                # Extract session_id = last path component
                session_id = Path(wt_path).name
                session_ids.append(session_id)

        return session_ids
