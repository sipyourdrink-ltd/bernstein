"""Worktree lifecycle management helpers for spawner.

Free functions that encapsulate worktree operations.  AgentSpawner
delegates to these from its own methods.

Also home to the artifact-mode workspace helpers (issue #2996): a session
whose whole task batch completes on signed lineage receipts (see
``bernstein.core.tasks.artifact_completion.needs_git_worktree``) gets an
isolated plain directory under ``.sdd/workspaces/<session_id>`` instead of a
git worktree - no checkout, no agent branch, nothing to merge back.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading
    from pathlib import Path

    from bernstein.core.agents.warm_pool import PoolSlot, WarmPool
    from bernstein.core.worktree import WorktreeManager, WorktreeSetupConfig

logger = logging.getLogger(__name__)

#: Repo-root-relative base directory for artifact-mode session workspaces.
#: Deliberately a sibling of ``.sdd/worktrees`` rather than inside it, so the
#: worktree orphan pruner and ``git worktree``-shaped cleanup never treat a
#: plain directory as a checkout.
ARTIFACT_WORKSPACES_RELPATH = ".sdd/workspaces"


def create_artifact_workspace(repo_root: Path, session_id: str) -> Path:
    """Create the isolated plain working directory for an artifact-mode session.

    The directory is what an artifact-mode task gets *instead of* a git
    worktree (issue #2996): a scratch cwd to run in, with no checkout and no
    agent branch. Raises ``FileExistsError`` when the path already exists so a
    session id collision fails loudly, mirroring ``WorktreeManager.create``.
    """
    workspace = repo_root / ARTIFACT_WORKSPACES_RELPATH / session_id
    workspace.parent.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=False, exist_ok=False)
    logger.info("Created artifact-mode workspace %s (no git worktree)", workspace)
    return workspace


def cleanup_artifact_workspace(
    session_id: str,
    *,
    artifact_workdirs: dict[str, Path],
) -> None:
    """Remove the artifact-mode workspace for ``session_id``, if any.

    Best-effort and safe to call for sessions that never had one - the
    function is a no-op in that case. There is nothing to merge or salvage:
    the session's durable output is its signed lineage receipt, not the
    scratch directory.
    """
    workspace = artifact_workdirs.pop(session_id, None)
    if workspace is None:
        return
    try:
        shutil.rmtree(workspace)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Artifact workspace removal failed for %s: %s", session_id, exc)
        return
    logger.info("Cleaned up artifact-mode workspace for %s", session_id)


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
    artifact_workdirs: dict[str, Path] | None = None,
) -> int:
    """Remove orphan worktree directories that don't correspond to active sessions.

    Runs ``git worktree prune`` then scans ``.sdd/worktrees/`` for
    directories whose name is not in *active_session_ids* and removes
    them via :class:`WorktreeManager`. Orphan artifact-mode workspaces
    under ``.sdd/workspaces/`` (issue #2996) are swept on the same terms -
    they are plain directories, so removal is a plain ``rmtree``.

    Args:
        active_session_ids: Session IDs that are currently alive/working.
        worktree_managers: Map of repo root -> WorktreeManager.
        worktree_paths: Mutable map of session_id -> worktree path.
        worktree_roots: Mutable map of session_id -> repo root.
        artifact_workdirs: Mutable map of session_id -> artifact workspace.

    Returns:
        Number of orphan worktrees and workspaces cleaned up.
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
        if base_dir.exists():
            for entry in base_dir.iterdir():
                if entry.is_dir() and entry.name != ".locks" and entry.name not in active_session_ids:
                    logger.info("Removing orphan worktree: %s", entry.name)
                    mgr.cleanup(entry.name)
                    # Also pop from spawner dicts in case they were tracked
                    worktree_paths.pop(entry.name, None)
                    worktree_roots.pop(entry.name, None)
                    cleaned += 1

        workspaces_dir = mgr.repo_root / ARTIFACT_WORKSPACES_RELPATH
        if workspaces_dir.exists():
            for entry in workspaces_dir.iterdir():
                if entry.is_dir() and entry.name not in active_session_ids:
                    logger.info("Removing orphan artifact workspace: %s", entry.name)
                    try:
                        shutil.rmtree(entry)
                    except OSError as exc:
                        logger.warning("Orphan artifact workspace removal failed for %s: %s", entry.name, exc)
                        continue
                    if artifact_workdirs is not None:
                        artifact_workdirs.pop(entry.name, None)
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
