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

import json
import logging
import os
import re as _re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.git.git_ops import branch_delete, worktree_add, worktree_list, worktree_remove
from bernstein.core.git.worktree_isolation import validate_worktree_isolation
from bernstein.core.platform_compat import process_alive

if TYPE_CHECKING:
    import threading
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

_WORKTREE_BASE = ".sdd/worktrees"
_SETUP_COMMAND_TIMEOUT_S = 300  # 5 minutes max for setup commands


@dataclass(frozen=True)
class WorktreeSetupConfig:
    """Configuration for environment setup after worktree creation.

    Applied immediately after ``git worktree add`` so the agent process
    finds a fully-provisioned checkout instead of a bare tree.

    Attributes:
        symlink_dirs: Directory names or relative paths to symlink from
            repo_root into the worktree.  Useful for large, read-heavy
            directories like ``node_modules``, ``.venv``, ``dist/``, or
            ``build/`` that are expensive to recreate per worktree.

            **OS caveats:**

            - **macOS / Linux:** Symlinks work natively with no restrictions.
            - **Windows:** Requires Developer Mode enabled or Administrator
              privileges to create symlinks.  When unavailable, ``OSError``
              is caught and logged as a warning — the worktree remains usable
              without the symlinked directories (agents may need to reinstall
              dependencies, increasing setup time).
            - **Cross-filesystem:** On Windows, symlinks across different
              drives (e.g. ``C:`` → ``D:``) need special privileges and may
              fail with ``EXDEV``.  On Unix, cross-device symlinks are
              supported by the kernel, so this is not an issue.
            - **Mixed-OS teams:** If the same repo is shared between macOS
              and Windows agents, expect different behavior.  Windows agents
              without symlink support will duplicate directories, consuming
              additional disk space and setup time.

        copy_files: File names (relative to repo root) to copy into the
            worktree.  Suitable for ``.env`` files that should not be shared
            via symlink (each agent may write its own port/secret overrides).
        sparse_paths: Paths to include when using git sparse-checkout for
            the worktree.  When provided, only the listed paths are checked
            out, reducing disk usage for large monorepos (T481).
        setup_command: Optional shell command to run *inside* the worktree
            after symlinking and copying.  Examples: ``"npm install"``,
            ``"uv sync"``, ``"make setup"``.
    """

    symlink_dirs: tuple[str, ...] = field(default_factory=tuple)
    copy_files: tuple[str, ...] = field(default_factory=tuple)
    sparse_paths: tuple[str, ...] = field(default_factory=tuple)
    setup_command: str | None = None


_STALE_LOCK_AGE_S = 300  # 5 minutes — locks older than this are considered stale


def _check_git_health(repo_root: Path) -> list[str]:
    """Pre-flight health check for the git repository before worktree creation.

    Detects and auto-repairs common corruption left by crashed agents:
    1. Stale ``.git/index.lock`` — deleted if older than 5 minutes.
    2. Stale ``.git/worktrees/*/locked`` files — same treatment.
    3. Invalid HEAD — verified via ``git rev-parse --verify HEAD``.

    Args:
        repo_root: Absolute path to the repository root.

    Returns:
        List of human-readable warnings for issues that were detected
        (and, where possible, auto-repaired).  Empty list means healthy.
    """
    warnings: list[str] = []
    now = time.time()

    # 1. Check for stale .git/index.lock
    index_lock = repo_root / ".git" / "index.lock"
    if index_lock.exists():
        try:
            age_s = now - index_lock.stat().st_mtime
            if age_s > _STALE_LOCK_AGE_S:
                index_lock.unlink()
                msg = f"Removed stale .git/index.lock (age {age_s:.0f}s). Likely left by a crashed agent."
                logger.warning(msg)
                warnings.append(msg)
            else:
                msg = f".git/index.lock exists (age {age_s:.0f}s) — another git operation may be in progress"
                logger.info(msg)
                warnings.append(msg)
        except OSError as exc:
            msg = f"Could not inspect .git/index.lock: {exc}"
            logger.warning(msg)
            warnings.append(msg)

    # 2. Check for stale .git/worktrees/*/locked files
    git_worktrees_dir = repo_root / ".git" / "worktrees"
    if git_worktrees_dir.is_dir():
        for wt_dir in git_worktrees_dir.iterdir():
            locked_file = wt_dir / "locked"
            if not locked_file.exists():
                continue
            try:
                age_s = now - locked_file.stat().st_mtime
                if age_s > _STALE_LOCK_AGE_S:
                    locked_file.unlink()
                    msg = f"Removed stale lock {locked_file} (age {age_s:.0f}s). Likely left by a crashed agent."
                    logger.warning(msg)
                    warnings.append(msg)
            except OSError as exc:
                msg = f"Could not inspect {locked_file}: {exc}"
                logger.warning(msg)
                warnings.append(msg)

    # 3. Verify HEAD is valid
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            msg = f"git HEAD is invalid: {result.stderr.strip()}"
            logger.error(msg)
            warnings.append(msg)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        msg = f"Failed to verify git HEAD: {exc}"
        logger.error(msg)
        warnings.append(msg)

    return warnings


def _apply_sparse_checkout(worktree_path: Path, sparse_paths: Sequence[str]) -> bool:
    """Enable git sparse-checkout on a worktree.

    Args:
        worktree_path: Path to the worktree directory.
        sparse_paths: Paths to include in the sparse checkout.

    Returns:
        True if sparse checkout was applied successfully.
    """
    if not sparse_paths:
        return False
    try:
        result = subprocess.run(
            ["git", "sparse-checkout", "init", "--cone"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning("git sparse-checkout init failed: %s", result.stderr.strip())
            return False

        result = subprocess.run(
            ["git", "sparse-checkout", "set", *sparse_paths],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning("git sparse-checkout set failed: %s", result.stderr.strip())
            return False

        logger.info("Applied sparse checkout to worktree %s: %s", worktree_path, sparse_paths)
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("Sparse checkout failed for %s: %s", worktree_path, exc)
        return False


def setup_worktree_env(
    repo_root: Path,
    worktree_path: Path,
    config: WorktreeSetupConfig,
) -> None:
    """Set up the environment inside a newly-created worktree.

    1. Symlinks large shared directories so the agent doesn't need to
       reinstall dependencies.
    2. Copies per-worktree files (e.g. ``.env``) so each agent has its
       own editable copy.
    3. Applies sparse checkout if configured.
    4. Optionally runs a setup command (e.g. ``npm install``) inside the
       worktree when symlinks are insufficient.

    Failures are logged as warnings but never propagate — a partially-set-up
    worktree is better than a hard spawn failure.

    Args:
        repo_root: Absolute path to the repository root.
        worktree_path: Path to the newly-created worktree directory.
        config: Environment setup configuration.
    """
    # --- Symlink shared directories -------------------------------------------
    for dir_name in config.symlink_dirs:
        source = repo_root / dir_name
        target = worktree_path / dir_name
        if not source.exists():
            logger.debug("Skipping symlink for %r: source does not exist", dir_name)
            continue
        if target.exists() or target.is_symlink():
            logger.debug("Skipping symlink for %r: target already exists", dir_name)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source)
            logger.info("Symlinked worktree/%s -> %s", dir_name, source)
        except OSError as exc:
            logger.warning("Failed to symlink %r into worktree: %s", dir_name, exc)

    # --- Copy environment files -----------------------------------------------
    for file_name in config.copy_files:
        source = repo_root / file_name
        target = worktree_path / file_name
        if not source.is_file():
            logger.debug("Skipping copy of %r: source missing or not a file", file_name)
            continue
        if target.exists():
            logger.debug("Skipping copy of %r: target already exists", file_name)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            logger.info("Copied %s into worktree", file_name)
        except OSError as exc:
            logger.warning("Failed to copy %r into worktree: %s", file_name, exc)

    # --- Apply sparse checkout ------------------------------------------------
    if config.sparse_paths:
        if _apply_sparse_checkout(worktree_path, config.sparse_paths):
            logger.info("Sparse checkout applied to worktree: %s", config.sparse_paths)
        else:
            logger.warning("Sparse checkout failed for worktree %s", worktree_path)

    # --- Run optional setup command -------------------------------------------
    if config.setup_command:
        logger.info("Running worktree setup command: %s", config.setup_command)
        try:
            import shlex

            result = subprocess.run(
                shlex.split(config.setup_command),
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=_SETUP_COMMAND_TIMEOUT_S,
            )
            if result.returncode != 0:
                logger.warning(
                    "Worktree setup command exited %d: %s",
                    result.returncode,
                    result.stderr[:500],
                )
            else:
                logger.info("Worktree setup command succeeded")
        except subprocess.TimeoutExpired:
            logger.warning("Worktree setup command timed out after %ds", _SETUP_COMMAND_TIMEOUT_S)
        except OSError as exc:
            logger.warning("Failed to run worktree setup command: %s", exc)


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
        setup_config: Optional environment setup applied after each worktree is
            created (symlinks, file copies, setup command).
    """

    def __init__(
        self,
        repo_root: Path,
        setup_config: WorktreeSetupConfig | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self._base_dir = self.repo_root / _WORKTREE_BASE
        self._setup_config = setup_config
        self._shutdown_event: threading.Event | None = None

    def set_shutdown_event(self, shutdown_event: threading.Event | None) -> None:
        """Attach a shutdown event used to reject new worktree creation."""
        self._shutdown_event = shutdown_event

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
        if self._shutdown_event is not None and self._shutdown_event.is_set():
            raise WorktreeError("Orchestrator shutting down — refusing new worktree")

        worktree_path = self._base_dir / session_id
        branch_name = f"agent/{session_id}"

        if worktree_path.exists():
            raise WorktreeError(f"Worktree path '{worktree_path}' already exists")

        self._base_dir.mkdir(parents=True, exist_ok=True)

        # Pre-flight: detect and auto-repair stale locks / invalid HEAD
        health_warnings = _check_git_health(self.repo_root)
        for warning in health_warnings:
            logger.warning("Git health pre-check: %s", warning)

        result = worktree_add(self.repo_root, worktree_path, branch_name)

        if not result.ok:
            stderr = result.stderr.strip()
            if "already exists" in stderr:
                raise WorktreeError(
                    f"Branch '{branch_name}' already exists. "
                    "Delete it manually or call cleanup() first. Git: {stderr}"
                )
            raise WorktreeError(f"git worktree add failed for session '{session_id}': {stderr}")

        logger.info("Created worktree %s (branch %s)", worktree_path, branch_name)

        # Write lock file for stale detection (T487)
        worker_pid = os.getpid()
        write_worktree_lock(self.repo_root, session_id, pid=worker_pid)

        allowed_symlinks: tuple[str, ...] = ()
        if self._setup_config is not None:
            setup_worktree_env(self.repo_root, worktree_path, self._setup_config)
            allowed_symlinks = self._setup_config.symlink_dirs

        isolation_result = validate_worktree_isolation(
            worktree_path,
            self.repo_root,
            allowed_symlink_dirs=allowed_symlinks,
        )
        if not isolation_result.passed:
            self.cleanup(session_id)
            raise WorktreeError(
                f"Worktree isolation violated for session '{session_id}': " + "; ".join(isolation_result.violations)
            )

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
            result = worktree_remove(self.repo_root, worktree_path)
            if not result.ok and "not a working tree" not in result.stderr:
                logger.warning(
                    "git worktree remove failed for %s: %s",
                    session_id,
                    result.stderr.strip(),
                )
        except Exception as exc:
            logger.warning("Failed to remove worktree for %s: %s", session_id, exc)

        # 2. Delete the branch
        try:
            result = branch_delete(self.repo_root, branch_name)
            if not result.ok and "not found" not in result.stderr:
                logger.warning(
                    "git branch -D failed for %s: %s",
                    branch_name,
                    result.stderr.strip(),
                )
        except Exception as exc:
            logger.warning("Failed to delete branch %s: %s", branch_name, exc)

        # 3. Remove lock file
        remove_worktree_lock(self.repo_root, session_id)

        logger.info("Cleaned up worktree for session %s", session_id)

    def cleanup_all_stale(self) -> int:
        """Remove all worktrees under the base dir from prior runs.

        Called at startup to ensure stale worktrees don't block new spawns.

        Uses both PID-file and lock-file stale detection. Active worktrees
        (live process or valid lock) are never removed.

        Returns:
            Number of worktrees cleaned up.
        """
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as exc:
            logger.debug("git worktree prune failed: %s", exc)

        if not self._base_dir.exists():
            return 0
        cleaned = 0
        for entry in self._base_dir.iterdir():
            if entry.is_dir() and entry.name != ".locks":
                session_id = entry.name
                if self._is_session_live(session_id):
                    logger.debug("Keeping live worktree %s during stale cleanup", session_id)
                    continue
                logger.info("Cleaning stale worktree: %s", session_id)
                self.cleanup(session_id)
                cleaned += 1
        return cleaned

    def _is_session_live(self, session_id: str) -> bool:
        """Return True if the session still has a live process or lock."""
        pid_alive = self._session_has_live_pid(session_id)
        lock_not_stale = not is_worktree_lock_stale(self.repo_root, session_id)
        return pid_alive or lock_not_stale

    def _session_has_live_pid(self, session_id: str) -> bool:
        """Return True when the session has a live recorded worker process."""
        pid_file = self.repo_root / ".sdd" / "runtime" / "pids" / f"{session_id}.json"
        if not pid_file.exists():
            return False
        try:
            data = json.loads(pid_file.read_text(encoding="utf-8"))
            worker_pid = int(data.get("worker_pid", 0) or 0)
        except (OSError, ValueError):
            return False
        if worker_pid <= 0:
            return False
        return process_alive(worker_pid)

    def list_active(self) -> list[str]:
        """Return session IDs that currently have active worktrees.

        Queries ``git worktree list`` and filters for paths under
        ``.sdd/worktrees/``.  Only the directory name (== session_id) is
        returned.

        Returns:
            List of active session IDs (may be empty).
        """
        try:
            output = worktree_list(self.repo_root)
        except Exception as exc:
            logger.warning("git worktree list failed: %s", exc)
            return []

        session_ids: list[str] = []
        base_str = str(self._base_dir)

        for line in output.splitlines():
            if not line.startswith("worktree "):
                continue
            wt_path = line[len("worktree ") :].strip()
            if wt_path.startswith(base_str):
                session_id = Path(wt_path).name
                session_ids.append(session_id)

        return session_ids


# ---------------------------------------------------------------------------
# Slug validation for worktree names (T572)
# ---------------------------------------------------------------------------

_SLUG_MAX_LEN = 64
_SLUG_PATTERN = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}[a-zA-Z0-9]$|^[a-zA-Z0-9]$")
_SLUG_RESERVED = frozenset({".", "..", "HEAD", "FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD"})


def validate_worktree_slug(slug: str) -> str:
    """Validate and return *slug* for use as a worktree session identifier (T572).

    Rules:
    - 1-64 characters.
    - Starts and ends with alphanumeric.
    - Interior characters: alphanumeric, ``-``, ``_``, ``.``.
    - No path traversal (``..``, ``/``, ``\\``).
    - Not a reserved git name.

    Args:
        slug: Candidate session identifier.

    Returns:
        The validated slug (unchanged).

    Raises:
        WorktreeError: If the slug is invalid.
    """
    if not slug:
        raise WorktreeError("Worktree slug must not be empty")
    if len(slug) > _SLUG_MAX_LEN:
        raise WorktreeError(f"Worktree slug too long ({len(slug)} chars, max {_SLUG_MAX_LEN}): {slug!r}")
    if "/" in slug or "\\" in slug:
        raise WorktreeError(f"Worktree slug must not contain path separators: {slug!r}")
    if ".." in slug:
        raise WorktreeError(f"Worktree slug must not contain '..': {slug!r}")
    if slug in _SLUG_RESERVED:
        raise WorktreeError(f"Worktree slug is a reserved git name: {slug!r}")
    if not _SLUG_PATTERN.match(slug):
        raise WorktreeError(f"Worktree slug contains invalid characters (allowed: a-z A-Z 0-9 - _ .): {slug!r}")
    return slug


# ---------------------------------------------------------------------------
# Worktree lock file protocol (T580)
# ---------------------------------------------------------------------------

_WORKTREE_LOCK_DIR = ".sdd/worktrees/.locks"


def write_worktree_lock(repo_root: Path, session_id: str, pid: int) -> Path:
    """Write a PID-based lock file for an active worktree (T580).

    Args:
        repo_root: Repository root directory.
        session_id: Agent session identifier.
        pid: Worker process PID.

    Returns:
        Path to the written lock file.
    """
    lock_dir = repo_root / _WORKTREE_LOCK_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{session_id}.lock"
    payload = {
        "session_id": session_id,
        "pid": pid,
        "created_at": time.time(),
    }
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    return lock_path


def remove_worktree_lock(repo_root: Path, session_id: str) -> None:
    """Remove the lock file for a worktree session (T580).

    Args:
        repo_root: Repository root directory.
        session_id: Agent session identifier.
    """
    lock_path = repo_root / _WORKTREE_LOCK_DIR / f"{session_id}.lock"
    try:
        lock_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to remove worktree lock for %s: %s", session_id, exc)


def is_worktree_lock_stale(repo_root: Path, session_id: str) -> bool:
    """Return True if the worktree lock is stale (process no longer alive) (T580).

    Args:
        repo_root: Repository root directory.
        session_id: Agent session identifier.

    Returns:
        True if the lock file is absent or the recorded PID is dead.
    """
    lock_path = repo_root / _WORKTREE_LOCK_DIR / f"{session_id}.lock"
    if not lock_path.exists():
        return True
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = int(data.get("pid", 0))
    except (OSError, ValueError):
        return True
    if pid <= 0:
        return True
    return not process_alive(pid)


# ---------------------------------------------------------------------------
# Sparse checkout for agent worktrees (T573)
# ---------------------------------------------------------------------------


def apply_sparse_checkout(
    worktree_path: Path,
    sparse_paths: list[str],
    *,
    timeout: int = 30,
) -> bool:
    """Apply sparse checkout to a worktree (T573).

    Enables ``git sparse-checkout`` in cone mode and sets the given paths.
    Falls back gracefully if the git version does not support sparse checkout.

    Args:
        worktree_path: Path to the worktree directory.
        sparse_paths: List of paths/patterns to include in the sparse checkout.
        timeout: Command timeout in seconds.

    Returns:
        True if sparse checkout was applied, False if unsupported or skipped.
    """
    if not sparse_paths:
        return False

    try:
        # Enable sparse checkout
        result = subprocess.run(
            ["git", "sparse-checkout", "init", "--cone"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning(
                "git sparse-checkout init failed for %s: %s",
                worktree_path,
                result.stderr.strip(),
            )
            return False

        # Set the paths
        result = subprocess.run(
            ["git", "sparse-checkout", "set", *sparse_paths],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning(
                "git sparse-checkout set failed for %s: %s",
                worktree_path,
                result.stderr.strip(),
            )
            return False

        logger.info("Applied sparse checkout to %s: %s", worktree_path, sparse_paths)
        return True

    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("Sparse checkout failed for %s: %s", worktree_path, exc)
        return False
