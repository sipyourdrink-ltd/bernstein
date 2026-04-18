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
from bernstein.core.git.salvage import SalvageResult, salvage_worktree
from bernstein.core.git.worktree_isolation import validate_worktree_isolation
from bernstein.core.platform_compat import process_alive

if TYPE_CHECKING:
    import threading
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

_WORKTREE_BASE = ".sdd/worktrees"
_SETUP_COMMAND_TIMEOUT_S = 300  # 5 minutes max for setup commands

# Graveyard for unmerged commits rescued from crashed agents (audit-097).
_GRAVEYARD_DIR_REL = ".sdd/graveyard"
_GRAVEYARD_REF_PREFIX = "refs/graveyard/"
_GRAVEYARD_GIT_TIMEOUT_S = 30
_GRAVEYARD_DEFAULT_PURGE_DAYS = 14


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


def _check_stale_lock(lock_path: Path, now: float) -> str | None:
    """Check a single lock file and remove it if stale.

    Args:
        lock_path: Path to the lock file.
        now: Current time (seconds since epoch).

    Returns:
        Warning message, or None if no issue.
    """
    if not lock_path.exists():
        return None
    try:
        age_s = now - lock_path.stat().st_mtime
        if age_s > _STALE_LOCK_AGE_S:
            lock_path.unlink()
            msg = f"Removed stale {lock_path.name} (age {age_s:.0f}s). Likely left by a crashed agent."
            logger.warning(msg)
            return msg
        msg = f"{lock_path} exists (age {age_s:.0f}s) — another git operation may be in progress"
        logger.info(msg)
        return msg
    except OSError as exc:
        msg = f"Could not inspect {lock_path}: {exc}"
        logger.warning(msg)
        return msg


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
    w = _check_stale_lock(repo_root / ".git" / "index.lock", now)
    if w:
        warnings.append(w)

    # 2. Check for stale .git/worktrees/*/locked files
    git_worktrees_dir = repo_root / ".git" / "worktrees"
    if git_worktrees_dir.is_dir():
        for wt_dir in git_worktrees_dir.iterdir():
            w = _check_stale_lock(wt_dir / "locked", now)
            if w:
                warnings.append(w)

    # 3. Verify HEAD is valid
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
            encoding="utf-8",
            errors="replace",
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
            encoding="utf-8",
            errors="replace",
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


def _symlink_dirs(repo_root: Path, worktree_path: Path, dirs: list[str]) -> None:
    """Symlink shared directories from repo root into the worktree.

    Args:
        repo_root: Repository root.
        worktree_path: Worktree directory.
        dirs: Directory names to symlink.
    """
    for dir_name in dirs:
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


def _copy_files(repo_root: Path, worktree_path: Path, files: list[str]) -> None:
    """Copy per-worktree files from repo root into the worktree.

    Args:
        repo_root: Repository root.
        worktree_path: Worktree directory.
        files: File names to copy.
    """
    for file_name in files:
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
    _symlink_dirs(repo_root, worktree_path, config.symlink_dirs)
    _copy_files(repo_root, worktree_path, config.copy_files)

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
                encoding="utf-8",
                errors="replace",
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


def _count_unmerged_commits(repo_root: Path, branch: str, base: str = "main") -> int:
    """Return how many commits on *branch* are not reachable from *base* (audit-097).

    Uses ``git rev-list <branch> ^<base> --count``.  If the branch is missing
    or the command fails, returns ``0`` — callers treat that as "nothing to
    preserve" which is safe because graveyard capture is best-effort.

    Args:
        repo_root: Repository root directory.
        branch: Branch name to compare against *base* (e.g. ``agent/<sid>``).
        base: Reference to compare against.  Defaults to ``main``.

    Returns:
        Number of commits on *branch* not in *base*.  ``0`` on any failure.
    """
    try:
        result = subprocess.run(
            ["git", "rev-list", branch, f"^{base}", "--count"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GRAVEYARD_GIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("rev-list for %s failed: %s", branch, exc)
        return 0
    if result.returncode != 0:
        logger.debug("rev-list for %s exited %d: %s", branch, result.returncode, result.stderr.strip())
        return 0
    raw = result.stdout.strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        logger.debug("rev-list for %s produced non-integer output: %r", branch, raw)
        return 0


def _resolve_ref(repo_root: Path, ref: str) -> str | None:
    """Resolve *ref* to a SHA via ``git rev-parse``; return ``None`` on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", ref],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GRAVEYARD_GIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("rev-parse for %s failed: %s", ref, exc)
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _graveyard_timestamp(now: float | None = None) -> str:
    """Return a filesystem- and ref-safe timestamp like ``20260418T103045Z``."""
    import datetime as _dt

    ts = _dt.datetime.fromtimestamp(now if now is not None else time.time(), tz=_dt.UTC)
    return ts.strftime("%Y%m%dT%H%M%SZ")


def preserve_branch_to_graveyard(
    repo_root: Path,
    session_id: str,
    *,
    branch: str | None = None,
    now: float | None = None,
) -> Path | None:
    """Move *branch* to ``refs/graveyard/<sid>-<ts>`` and export a bundle (audit-097).

    Called before a destructive worktree cleanup when the session branch has
    unmerged commits.  The rescue path is:

    1. Resolve the current tip of ``agent/<sid>``.
    2. Create ``refs/graveyard/<sid>-<ts>`` pointing at that tip via
       ``git update-ref``.
    3. Emit a portable ``git bundle`` at
       ``.sdd/graveyard/<sid>-<ts>.bundle`` so the commits survive even if
       the repo's object database is later pruned.

    The original ``agent/<sid>`` branch is *not* deleted here — the caller
    (:meth:`WorktreeManager.cleanup`) already does that via ``git branch
    -D`` once the worktree has been removed.  Attempting to delete it
    earlier would fail because the branch is still checked out in the
    (stale) worktree; the graveyard ref already preserves the commits
    so deletion is safe at the later step.

    On any failure the function logs a warning and returns ``None`` — the
    caller must not treat graveyard preservation as a hard prerequisite.

    Args:
        repo_root: Repository root directory.
        session_id: Agent session identifier.
        branch: Override branch name (defaults to ``agent/<session_id>``).
        now: Optional clock override (seconds since epoch) for deterministic
            tests.

    Returns:
        Path to the bundle file when the bundle was written, otherwise
        ``None`` (ref may still have been created — see logs).
    """
    source_branch = branch if branch is not None else f"agent/{session_id}"
    sha = _resolve_ref(repo_root, source_branch)
    if sha is None:
        logger.debug("Graveyard skip: branch %s does not resolve", source_branch)
        return None

    ts = _graveyard_timestamp(now)
    ref_name = f"{_GRAVEYARD_REF_PREFIX}{session_id}-{ts}"

    # 1. Create the graveyard ref.
    try:
        update = subprocess.run(
            ["git", "update-ref", ref_name, sha],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GRAVEYARD_GIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("Graveyard update-ref failed for %s: %s", session_id, exc)
        return None
    if update.returncode != 0:
        logger.warning(
            "Graveyard update-ref for %s exited %d: %s",
            session_id,
            update.returncode,
            update.stderr.strip(),
        )
        return None

    # 2. Export a bundle so commits survive ``git gc``.
    bundle_dir = repo_root / _GRAVEYARD_DIR_REL
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_path: Path | None = bundle_dir / f"{session_id}-{ts}.bundle"
    try:
        bundle = subprocess.run(
            ["git", "bundle", "create", str(bundle_path), ref_name],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GRAVEYARD_GIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("Graveyard bundle create failed for %s: %s", session_id, exc)
        bundle_path = None
    else:
        if bundle.returncode != 0:
            logger.warning(
                "Graveyard bundle for %s exited %d: %s",
                session_id,
                bundle.returncode,
                bundle.stderr.strip(),
            )
            bundle_path = None

    logger.warning(
        "Preserved branch %s (sha=%s) to %s (bundle=%s)",
        source_branch,
        sha[:12],
        ref_name,
        bundle_path,
    )
    return bundle_path


def purge_graveyard(repo_root: Path, older_than_days: int = _GRAVEYARD_DEFAULT_PURGE_DAYS) -> int:
    """Remove graveyard refs and bundles older than *older_than_days* (audit-097).

    Intended for operator-driven cleanup — never called automatically.  Both
    the ``refs/graveyard/*`` ref and the on-disk ``.sdd/graveyard/*.bundle``
    are purged independently so a partially-corrupted graveyard still shrinks.

    Args:
        repo_root: Repository root directory.
        older_than_days: Entries whose mtime (bundle) or committer date (ref)
            is older than this many days are purged.  Must be non-negative.

    Returns:
        Number of artifacts (refs + bundles) deleted.
    """
    if older_than_days < 0:
        raise ValueError(f"older_than_days must be >= 0, got {older_than_days}")

    cutoff = time.time() - (older_than_days * 86400)
    purged = 0

    # 1. Refs — list via for-each-ref with committer date.
    try:
        listing = subprocess.run(
            [
                "git",
                "for-each-ref",
                "--format=%(refname) %(committerdate:unix)",
                "refs/graveyard/",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GRAVEYARD_GIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("purge_graveyard: for-each-ref failed: %s", exc)
        listing = None

    if listing is not None and listing.returncode == 0:
        for line in listing.stdout.splitlines():
            parts = line.strip().split(" ", 1)
            if len(parts) != 2:
                continue
            ref_name, ts_str = parts
            try:
                committed_at = float(ts_str)
            except ValueError:
                continue
            if committed_at > cutoff:
                continue
            try:
                delete = subprocess.run(
                    ["git", "update-ref", "-d", ref_name],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=_GRAVEYARD_GIT_TIMEOUT_S,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
                logger.warning("purge_graveyard: update-ref -d %s failed: %s", ref_name, exc)
                continue
            if delete.returncode == 0:
                purged += 1
                logger.info("Purged graveyard ref %s", ref_name)
            else:
                logger.warning(
                    "purge_graveyard: update-ref -d %s exited %d: %s",
                    ref_name,
                    delete.returncode,
                    delete.stderr.strip(),
                )

    # 2. Bundles — plain filesystem walk; independent of git state.
    bundle_dir = repo_root / _GRAVEYARD_DIR_REL
    if bundle_dir.is_dir():
        for bundle in bundle_dir.iterdir():
            if not bundle.is_file() or bundle.suffix != ".bundle":
                continue
            try:
                mtime = bundle.stat().st_mtime
            except OSError:
                continue
            if mtime > cutoff:
                continue
            try:
                bundle.unlink()
            except OSError as exc:
                logger.warning("purge_graveyard: could not delete %s: %s", bundle, exc)
                continue
            purged += 1
            logger.info("Purged graveyard bundle %s", bundle)

    return purged


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
        *,
        salvage_on_cleanup: bool = True,
        salvage_push: bool = True,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self._base_dir = self.repo_root / _WORKTREE_BASE
        self._setup_config = setup_config
        self._salvage_on_cleanup = salvage_on_cleanup
        self._salvage_push = salvage_push
        self._shutdown_event: threading.Event | None = None
        self._last_salvage: SalvageResult | None = None

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
                    f"Branch '{branch_name}' already exists. Delete it manually or call cleanup() first. Git: {stderr}"
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

        Before the destructive ``git worktree remove --force`` call, any
        uncommitted work in the worktree is salvaged via
        :func:`~bernstein.core.git.salvage.salvage_worktree` so the diff is
        recoverable post-cleanup (audit-088).  The salvage step is purely
        best-effort: on failure the original cleanup proceeds as before.

        Args:
            session_id: The session whose worktree should be removed.
        """
        worktree_path = self._base_dir / session_id
        branch_name = f"agent/{session_id}"

        # 0. Salvage uncommitted work BEFORE anything destructive happens (audit-088).
        self._last_salvage = None
        if self._salvage_on_cleanup and worktree_path.exists():
            try:
                self._last_salvage = salvage_worktree(
                    self.repo_root,
                    worktree_path,
                    session_id,
                    push=self._salvage_push,
                )
            except Exception as exc:
                logger.warning(
                    "Salvage step crashed for %s (continuing cleanup): %s",
                    session_id,
                    exc,
                )

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
        #    When salvage moved the branch to salvage/<id> the original agent
        #    branch no longer exists; branch_delete will report "not found"
        #    which we already swallow below.
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

    @property
    def last_salvage(self) -> SalvageResult | None:
        """Return the salvage result from the most recent cleanup call.

        ``None`` if no cleanup has been invoked yet or salvage was disabled.
        Useful for tests and for operators who want to log/emit metrics on
        the salvage outcome.
        """
        return self._last_salvage

    def cleanup_all_stale(self) -> int:
        """Remove all worktrees under the base dir from prior runs.

        Called at startup to ensure stale worktrees don't block new spawns.

        Uses both PID-file and lock-file stale detection. Active worktrees
        (live process or valid lock) are never removed.

        Any stale session whose ``agent/<sid>`` branch has commits not yet
        reachable from ``main`` is preserved to
        ``refs/graveyard/<sid>-<ts>`` with an accompanying bundle at
        ``.sdd/graveyard/<sid>-<ts>.bundle`` *before* the destructive
        cleanup runs (audit-097).  This prevents ``kill -9`` / OOM of a
        prior orchestrator from silently wiping unmerged agent work on
        the next startup.

        Returns:
            Number of worktrees cleaned up.
        """
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
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

                # Preserve unmerged commits to the graveyard before we nuke
                # the branch (audit-097).  A crashed agent may have committed
                # work locally that never reached main; ``git worktree
                # remove --force`` followed by ``git branch -D`` would make
                # those commits unreachable and gc-eligible.
                branch_name = f"agent/{session_id}"
                try:
                    unmerged = _count_unmerged_commits(self.repo_root, branch_name, base="main")
                except Exception as exc:  # defensive — never block cleanup
                    logger.debug("Graveyard pre-check failed for %s: %s", session_id, exc)
                    unmerged = 0
                if unmerged > 0:
                    logger.warning(
                        "Stale worktree %s has %d unmerged commit(s); preserving to graveyard",
                        session_id,
                        unmerged,
                    )
                    try:
                        preserve_branch_to_graveyard(self.repo_root, session_id, branch=branch_name)
                    except Exception as exc:  # defensive — log and proceed
                        logger.warning(
                            "Graveyard preservation crashed for %s (continuing cleanup): %s",
                            session_id,
                            exc,
                        )

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
            encoding="utf-8",
            errors="replace",
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
            encoding="utf-8",
            errors="replace",
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
