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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.git_ops import branch_delete, worktree_add, worktree_list, worktree_remove

if TYPE_CHECKING:
    import threading

logger = logging.getLogger(__name__)

_WORKTREE_BASE = ".sdd/worktrees"
_SETUP_COMMAND_TIMEOUT_S = 300  # 5 minutes max for setup commands


@dataclass(frozen=True)
class WorktreeSetupConfig:
    """Configuration for environment setup after worktree creation.

    Applied immediately after ``git worktree add`` so the agent process
    finds a fully-provisioned checkout instead of a bare tree.

    Attributes:
        symlink_dirs: Directory names to symlink from repo_root into the
            worktree.  Useful for large build artefacts like ``node_modules``
            or ``.venv`` that are expensive to recreate per worktree.
        copy_files: File names (relative to repo root) to copy into the
            worktree.  Suitable for ``.env`` files that should not be shared
            via symlink (each agent may write its own port/secret overrides).
        setup_command: Optional shell command to run *inside* the worktree
            after symlinking and copying.  Examples: ``"npm install"``,
            ``"uv sync"``, ``"make setup"``.
    """

    symlink_dirs: tuple[str, ...] = field(default_factory=tuple)
    copy_files: tuple[str, ...] = field(default_factory=tuple)
    setup_command: str | None = None


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
    3. Optionally runs a setup command (e.g. ``npm install``) inside the
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
            # Ensure parent directory exists (for nested paths like .env.d/local)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            logger.info("Copied %s into worktree", file_name)
        except OSError as exc:
            logger.warning("Failed to copy %r into worktree: %s", file_name, exc)

    # --- Run optional setup command -------------------------------------------
    if config.setup_command:
        logger.info("Running worktree setup command: %s", config.setup_command)
        try:
            result = subprocess.run(
                config.setup_command,
                shell=True,  # SECURITY: shell=True required because worktree setup
                # commands are admin-configured shell strings that may use
                # pipes or redirects; not user input
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

        result = worktree_add(self.repo_root, worktree_path, branch_name)

        if not result.ok:
            stderr = result.stderr.strip()
            if "already exists" in stderr:
                raise WorktreeError(
                    f"Branch '{branch_name}' already exists. Delete it manually or call cleanup() first. Git: {stderr}"
                )
            raise WorktreeError(f"git worktree add failed for session '{session_id}': {stderr}")

        logger.info("Created worktree %s (branch %s)", worktree_path, branch_name)

        if self._setup_config is not None:
            setup_worktree_env(self.repo_root, worktree_path, self._setup_config)

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
            if not result.ok:
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
            if not result.ok:
                logger.warning(
                    "git branch -D failed for %s: %s",
                    branch_name,
                    result.stderr.strip(),
                )
        except Exception as exc:
            logger.warning("Failed to delete branch %s: %s", branch_name, exc)

        logger.info("Cleaned up worktree for session %s", session_id)

    def cleanup_all_stale(self) -> int:
        """Remove all worktrees under the base dir from prior runs.

        Called at startup to ensure stale worktrees don't block new spawns.

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
            if entry.is_dir():
                session_id = entry.name
                if self._session_has_live_pid(session_id):
                    logger.debug("Keeping live worktree %s during stale cleanup", session_id)
                    continue
                logger.info("Cleaning stale worktree: %s", session_id)
                self.cleanup(session_id)
                cleaned += 1
        return cleaned

    def _session_has_live_pid(self, session_id: str) -> bool:
        """Return True when the session has a live recorded worker process."""
        pid_file = self.repo_root / ".sdd" / "runtime" / "pids" / f"{session_id}.json"
        if not pid_file.exists():
            return False
        try:
            data = json.loads(pid_file.read_text(encoding="utf-8"))
            worker_pid = int(data.get("worker_pid", 0) or 0)
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if worker_pid <= 0:
            return False
        try:
            os.kill(worker_pid, 0)
            return True
        except OSError:
            return False

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
        "created_at": __import__("time").time(),
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
    except (json.JSONDecodeError, OSError, ValueError):
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
        return False  # process is alive
    except OSError:
        return True  # process is dead
# ---------------------------------------------------------------------------
# Slug validation for worktree names (T572)
# ---------------------------------------------------------------------------

import re as _re
from typing import Optional, Tuple

def validate_worktree_slug(name: str) -> Tuple[bool, Optional[str]]:
    """
    Validate worktree name as a valid slug (T572).
    
    Args:
        name: Worktree name to validate
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not name:
        return False, "Worktree name cannot be empty"
    
    # Check length
    if len(name) < 3:
        return False, "Worktree name must be at least 3 characters"
    
    if len(name) > 50:
        return False, "Worktree name must be 50 characters or less"
    
    # Check for valid characters (alphanumeric and hyphens only)
    if not _re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', name):
        return False, "Worktree name can only contain lowercase letters, numbers, and hyphens, must start and end with alphanumeric"
    
    # Check for reserved names
    reserved_names = {"main", "master", "head", "worktree", "worktrees", "git", "refs"}
    if name in reserved_names:
        return False, f"Worktree name '{name}' is reserved"
    
    # Check for common invalid patterns
    if name.startswith(".") or name.endswith("."):
        return False, "Worktree name cannot start or end with a dot"
    
    if "--" in name:
        return False, "Worktree name cannot contain consecutive hyphens"
    
    if name.startswith("-") or name.endswith("-"):
        return False, "Worktree name cannot start or end with a hyphen"
    
    return True, None

def sanitize_worktree_name(name: str) -> str:
    """
    Sanitize a worktree name to be slug-compliant.
    
    Args:
        name: Input worktree name
        
    Returns:
        Sanitized worktree name
    """
    # Convert to lowercase
    sanitized = name.lower()
    
    # Replace spaces and underscores with hyphens
    sanitized = _re.sub(r'[\s_]+', '-', sanitized)
    
    # Remove any non-alphanumeric characters except hyphens
    sanitized = _re.sub(r'[^a-z0-9-]', '', sanitized)
    
    # Remove leading/trailing hyphens
    sanitized = sanitized.strip('-')
    
    # Remove consecutive hyphens
    sanitized = _re.sub(r'-+', '-', sanitized)
    
    # Ensure it's not empty
    if not sanitized:
        sanitized = "worktree"
    
    # Ensure it starts and ends with alphanumeric
    sanitized = _re.sub(r'^-+', '', sanitized)
    sanitized = _re.sub(r'-+$', '', sanitized)
    
    # Ensure it's not empty after sanitization
    if not sanitized:
        sanitized = "worktree"
    
    return sanitized

def generate_worktree_slug(base_name: str, existing_names: set) -> str:
    """
    Generate a unique worktree slug from a base name.
    
    Args:
        base_name: Base name for the worktree
        existing_names: Set of existing worktree names
        
    Returns:
        Unique worktree slug
    """
    # First, sanitize the base name
    base_slug = sanitize_worktree_name(base_name)
    
    # If the sanitized name is empty, use a default
    if not base_slug:
        base_slug = "worktree"
    
    # If the base slug is already unique, use it
    if base_slug not in existing_names:
        return base_slug
    
    # Otherwise, find a unique name by appending a number
    counter = 1
    while True:
        candidate = f"{base_slug}-{counter}"
        if candidate not in existing_names:
            return candidate
        counter += 1
# ---------------------------------------------------------------------------
# Sparse checkout for agent worktrees (T573)
# ---------------------------------------------------------------------------

import subprocess as _subprocess
from pathlib import Path
from typing import List, Optional

def enable_sparse_checkout(
    worktree_path: Path,
    patterns: List[str],
    *,
    core_sparse_checkout: bool = True
) -> bool:
    """
    Enable sparse checkout for a worktree (T573).
    
    Args:
        worktree_path: Path to the worktree
        patterns: List of sparse checkout patterns
        core_sparse_checkout: Whether to use core.sparseCheckout
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Enable sparse checkout
        if core_sparse_checkout:
            _subprocess.run(
                ["git", "config", "core.sparseCheckout", "true"],
                cwd=worktree_path,
                check=True,
                capture_output=True
            )
        
        # Create sparse-checkout file
        sparse_file = worktree_path / ".git" / "info" / "sparse-checkout"
        sparse_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(sparse_file, "w") as f:
            for pattern in patterns:
                f.write(f"{pattern}\n")
        
        # Read sparse-checkout file
        _subprocess.run(
            ["git", "sparse-checkout", "reapply"],
            cwd=worktree_path,
            check=True,
            capture_output=True
        )
        
        logger.info(f"Sparse checkout enabled for {worktree_path} with {len(patterns)} patterns")
        return True
        
    except _subprocess.CalledProcessError as e:
        logger.error(f"Failed to enable sparse checkout: {e}")
        return False
    except Exception as e:
        logger.error(f"Error enabling sparse checkout: {e}")
        return False

def disable_sparse_checkout(worktree_path: Path) -> bool:
    """
    Disable sparse checkout for a worktree.
    
    Args:
        worktree_path: Path to the worktree
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Disable sparse checkout
        _subprocess.run(
            ["git", "config", "--unset", "core.sparseCheckout"],
            cwd=worktree_path,
            check=False,
            capture_output=True
        )
        
        # Remove sparse-checkout file
        sparse_file = worktree_path / ".git" / "info" / "sparse-checkout"
        if sparse_file.exists():
            sparse_file.unlink()
        
        logger.info(f"Sparse checkout disabled for {worktree_path}")
        return True
        
    except Exception as e:
        logger.error(f"Error disabling sparse checkout: {e}")
        return False

def get_sparse_checkout_patterns(worktree_path: Path) -> Optional[List[str]]:
    """
    Get current sparse checkout patterns for a worktree.
    
    Args:
        worktree_path: Path to the worktree
        
    Returns:
        List of patterns or None if not enabled
    """
    sparse_file = worktree_path / ".git" / "info" / "sparse-checkout"
    
    if not sparse_file.exists():
        return None
    
    try:
        with open(sparse_file, "r") as f:
            patterns = [line.strip() for line in f if line.strip()]
        return patterns
    except Exception as e:
        logger.error(f"Error reading sparse checkout patterns: {e}")
        return None

def update_sparse_checkout_patterns(
    worktree_path: Path,
    patterns: List[str]
) -> bool:
    """
    Update sparse checkout patterns for a worktree.
    
    Args:
        worktree_path: Path to the worktree
        patterns: New list of sparse checkout patterns
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Get current patterns to check if we need to update
        current_patterns = get_sparse_checkout_patterns(worktree_path)
        
        # If patterns haven't changed, no need to update
        if current_patterns == patterns:
            return True
        
        # Update sparse-checkout file
        sparse_file = worktree_path / ".git" / "info" / "sparse-checkout"
        sparse_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(sparse_file, "w") as f:
            for pattern in patterns:
                f.write(f"{pattern}\n")
        
        # Reapply sparse checkout
        _subprocess.run(
            ["git", "sparse-checkout", "reapply"],
            cwd=worktree_path,
            check=True,
            capture_output=True
        )
        
        logger.info(f"Sparse checkout patterns updated for {worktree_path}")
        return True
        
    except _subprocess.CalledProcessError as e:
        logger.error(f"Failed to update sparse checkout patterns: {e}")
        return False
    except Exception as e:
        logger.error(f"Error updating sparse checkout patterns: {e}")
        return False
