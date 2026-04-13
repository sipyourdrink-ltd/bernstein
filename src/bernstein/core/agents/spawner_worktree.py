"""Worktree lifecycle management helpers for spawner.

Free functions that encapsulate worktree operations.  AgentSpawner
delegates to these from its own methods.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading
    from pathlib import Path

    from bernstein.core.agents.warm_pool import PoolSlot, WarmPool
    from bernstein.core.worktree import WorktreeManager, WorktreeSetupConfig

logger = logging.getLogger(__name__)


def worktree_manager_for_repo(
    repo_root: Path,
    use_worktrees: bool,
    worktree_managers: dict[Path, WorktreeManager],
    worktree_setup_config: WorktreeSetupConfig | None,
    shutdown_event: threading.Event | None,
) -> WorktreeManager | None:
    """Return (or create) the WorktreeManager for a given repo root.

    Args:
        repo_root: Repo root path to normalise and look up.
        use_worktrees: Whether worktrees are enabled.
        worktree_managers: Mutable dict of existing managers (may be updated).
        worktree_setup_config: Optional setup config to pass to new managers.
        shutdown_event: Optional threading.Event to attach to new managers.

    Returns:
        WorktreeManager or None when worktrees are disabled.
    """
    if not use_worktrees:
        return None
    from bernstein.core.worktree import WorktreeManager as _WM

    normalized = repo_root.resolve()
    existing = worktree_managers.get(normalized)
    if existing is not None:
        return existing
    manager = _WM(normalized, setup_config=worktree_setup_config)
    manager.set_shutdown_event(shutdown_event)
    worktree_managers[normalized] = manager
    return manager


def cleanup_worktree(
    session_id: str,
    *,
    worktree_roots: dict[str, Path],
    worktree_paths: dict[str, Path],
    worktree_managers: dict[Path, WorktreeManager],
    worktree_mgr: WorktreeManager | None,
    workdir: Path,
) -> None:
    """Remove the worktree and branch for a dead agent session.

    Best-effort: removes the worktree directory, deletes the local branch,
    runs ``git worktree prune``, and pops session from internal dicts.
    Safe to call even if the worktree was never created or already cleaned.

    Args:
        session_id: The session whose worktree should be cleaned up.
        worktree_roots: Mutable map of session_id -> repo root.
        worktree_paths: Mutable map of session_id -> worktree path.
        worktree_managers: Map of repo root -> WorktreeManager.
        worktree_mgr: Default WorktreeManager (may be None).
        workdir: Project working directory.
    """
    worktree_root = worktree_roots.get(session_id, workdir.resolve())
    mgr = worktree_managers.get(worktree_root) or worktree_mgr
    if mgr is not None:
        mgr.cleanup(session_id)
    else:
        # No manager available -- try manual removal of the directory
        worktree_path = worktree_paths.get(session_id)
        if worktree_path is not None and worktree_path.exists():
            import shutil

            try:
                shutil.rmtree(worktree_path)
            except OSError as exc:
                logger.warning("Manual worktree removal failed for %s: %s", session_id, exc)
    worktree_paths.pop(session_id, None)
    worktree_roots.pop(session_id, None)
    logger.info("Cleaned up worktree for dead agent %s", session_id)


def prune_orphan_worktrees(
    active_session_ids: set[str],
    *,
    worktree_managers: dict[Path, WorktreeManager],
    worktree_paths: dict[str, Path],
    worktree_roots: dict[str, Path],
) -> int:
    """Remove orphan worktree directories that don't correspond to active sessions.

    Runs ``git worktree prune`` then scans ``.sdd/worktrees/`` for
    directories whose name is not in *active_session_ids* and removes
    them via :class:`WorktreeManager`.

    Args:
        active_session_ids: Session IDs that are currently alive/working.
        worktree_managers: Map of repo root -> WorktreeManager.
        worktree_paths: Mutable map of session_id -> worktree path.
        worktree_roots: Mutable map of session_id -> repo root.

    Returns:
        Number of orphan worktrees cleaned up.
    """
    cleaned = 0
    for mgr in worktree_managers.values():
        # Prune git's internal worktree bookkeeping first
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=mgr.repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except Exception as exc:
            logger.debug("git worktree prune failed: %s", exc)

        base_dir = mgr.repo_root / ".sdd" / "worktrees"
        if not base_dir.exists():
            continue
        for entry in base_dir.iterdir():
            if entry.is_dir() and entry.name != ".locks" and entry.name not in active_session_ids:
                logger.info("Removing orphan worktree: %s", entry.name)
                mgr.cleanup(entry.name)
                # Also pop from spawner dicts in case they were tracked
                worktree_paths.pop(entry.name, None)
                worktree_roots.pop(entry.name, None)
                cleaned += 1
    return cleaned


def release_warm_pool_slot(
    session_id: str,
    *,
    warm_pool_entries: dict[str, PoolSlot],
    warm_pool: WarmPool | None,
) -> None:
    """Release a claimed warm pool slot for *session_id*, if any.

    Safe to call even when no warm pool entry was claimed -- the
    function is a no-op in that case.  Used to prevent permanent
    worktree leaks when a spawn fails after claiming a slot (BUG-19).
    """
    warm_entry = warm_pool_entries.pop(session_id, None)
    if warm_entry is not None and warm_pool is not None:
        logger.info(
            "Releasing warm pool slot %s after spawn failure for session %s",
            warm_entry.slot_id,
            session_id,
        )
        warm_pool.release_slot(warm_entry.slot_id)
