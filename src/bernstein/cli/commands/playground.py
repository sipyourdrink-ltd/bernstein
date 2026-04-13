"""Bernstein Playground — local sandbox for safe experimentation.

Provides helpers to create isolated sandbox copies of a project directory
using ``git worktree`` (preferred) or a shallow ``git clone`` fallback.
Changes made inside the sandbox can later be diffed, applied back to the
original working tree, or discarded.

Usage::

    from pathlib import Path
    from bernstein.cli.playground import (
        PlaygroundConfig,
        create_sandbox,
        get_sandbox_diff,
        apply_sandbox,
        discard_sandbox,
        list_active_sessions,
    )

    session = create_sandbox(Path.cwd())
    # ... agent works inside session.sandbox_dir ...
    diff = get_sandbox_diff(session)
    apply_sandbox(session)
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

_SESSION_FILE_SUFFIX = ".json"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlaygroundConfig:
    """Tunable knobs for sandbox creation.

    Attributes:
        auto_cleanup: When ``True``, ``discard_sandbox`` removes the
            sandbox directory from disk automatically.
        sandbox_prefix: Directory-name prefix used when creating the
            temporary worktree / clone.
    """

    auto_cleanup: bool = True
    sandbox_prefix: str = ".bernstein-playground-"


@dataclass(frozen=True)
class PlaygroundSession:
    """Immutable record of a single playground sandbox.

    Attributes:
        session_id: Unique identifier for this sandbox.
        original_dir: Absolute path to the original working tree.
        sandbox_dir: Absolute path to the sandbox directory.
        created_at: ISO-8601 timestamp of creation.
        status: Current lifecycle state.
    """

    session_id: str
    original_dir: str
    sandbox_dir: str
    created_at: str
    status: Literal["active", "applied", "discarded"] = "active"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _sessions_dir(workdir: Path) -> Path:
    """Return the directory where session metadata files are stored."""
    return workdir / ".sdd" / "playground"


def save_session(session: PlaygroundSession, sessions_dir: Path) -> None:
    """Persist a session record to *sessions_dir*/<session_id>.json.

    Args:
        session: The session to save.
        sessions_dir: Directory where session JSON files are stored.
    """
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{session.session_id}{_SESSION_FILE_SUFFIX}"
    payload = {
        "session_id": session.session_id,
        "original_dir": session.original_dir,
        "sandbox_dir": session.sandbox_dir,
        "created_at": session.created_at,
        "status": session.status,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    logger.debug("Saved playground session %s → %s", session.session_id, path)


def load_sessions(sessions_dir: Path) -> list[PlaygroundSession]:
    """Load all session records from *sessions_dir*.

    Args:
        sessions_dir: Directory containing ``<session_id>.json`` files.

    Returns:
        List of ``PlaygroundSession`` objects, sorted by *created_at*
        ascending.
    """
    if not sessions_dir.is_dir():
        return []
    sessions: list[PlaygroundSession] = []
    for path in sorted(sessions_dir.glob(f"*{_SESSION_FILE_SUFFIX}")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            sessions.append(
                PlaygroundSession(
                    session_id=data["session_id"],
                    original_dir=data["original_dir"],
                    sandbox_dir=data["sandbox_dir"],
                    created_at=data["created_at"],
                    status=data.get("status", "active"),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Skipping unreadable session file: %s", path)
    return sessions


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a git command, returning the completed process."""
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        timeout=30,
    )


def _is_git_repo(workdir: Path) -> bool:
    """Return ``True`` if *workdir* is inside a git repository."""
    result = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=workdir)
    return result.returncode == 0


def _worktree_add(workdir: Path, sandbox_path: Path, branch_name: str) -> bool:
    """Try ``git worktree add`` and return ``True`` on success."""
    result = _run_git(
        ["worktree", "add", "--detach", str(sandbox_path)],
        cwd=workdir,
    )
    if result.returncode != 0:
        logger.debug("git worktree add failed: %s", result.stderr.strip())
        return False
    return True


def _shallow_clone(workdir: Path, sandbox_path: Path) -> bool:
    """Fallback: ``git clone --depth 1`` from *workdir* into *sandbox_path*."""
    result = _run_git(
        ["clone", "--depth", "1", str(workdir), str(sandbox_path)],
    )
    if result.returncode != 0:
        logger.debug("git clone failed: %s", result.stderr.strip())
        return False
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_sandbox(
    workdir: Path,
    config: PlaygroundConfig | None = None,
) -> PlaygroundSession:
    """Create an isolated sandbox copy of *workdir*.

    Tries ``git worktree add`` first; falls back to a shallow clone if
    worktrees are unavailable.

    Args:
        workdir: The original project directory to sandbox.
        config: Optional configuration overrides.

    Returns:
        A new :class:`PlaygroundSession` describing the sandbox.

    Raises:
        RuntimeError: If neither worktree nor clone succeeded.
        ValueError: If *workdir* is not a git repository.
    """
    cfg = config or PlaygroundConfig()
    workdir = workdir.resolve()

    if not _is_git_repo(workdir):
        msg = f"Not a git repository: {workdir}"
        raise ValueError(msg)

    session_id = uuid.uuid4().hex[:12]
    sandbox_name = f"{cfg.sandbox_prefix}{session_id}"
    sandbox_path = workdir.parent / sandbox_name

    branch_name = f"playground-{session_id}"
    created = _worktree_add(workdir, sandbox_path, branch_name)
    if not created:
        created = _shallow_clone(workdir, sandbox_path)
    if not created:
        msg = f"Failed to create sandbox at {sandbox_path}"
        raise RuntimeError(msg)

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    session = PlaygroundSession(
        session_id=session_id,
        original_dir=str(workdir),
        sandbox_dir=str(sandbox_path),
        created_at=now,
        status="active",
    )

    sessions_dir = _sessions_dir(workdir)
    save_session(session, sessions_dir)
    logger.info("Created playground sandbox %s at %s", session_id, sandbox_path)
    return session


def get_sandbox_diff(session: PlaygroundSession) -> str:
    """Return a unified diff of changes in the sandbox vs its HEAD.

    Args:
        session: An active playground session.

    Returns:
        The ``git diff`` output as a string (empty if no changes).
    """
    sandbox = Path(session.sandbox_dir)
    if not sandbox.is_dir():
        return ""
    result = _run_git(["diff", "HEAD"], cwd=sandbox)
    if result.returncode != 0:
        logger.warning("git diff failed in sandbox %s: %s", session.session_id, result.stderr.strip())
        return ""
    return result.stdout


def apply_sandbox(session: PlaygroundSession) -> bool:
    """Merge sandbox changes back into the original working tree.

    Generates a patch from the sandbox and applies it to the original
    directory using ``git apply``.

    Args:
        session: An active playground session.

    Returns:
        ``True`` if the patch was applied successfully (or there was
        nothing to apply), ``False`` otherwise.
    """
    diff = get_sandbox_diff(session)
    if not diff:
        logger.info("No changes to apply from sandbox %s", session.session_id)
        return True

    original = Path(session.original_dir)
    result = subprocess.run(
        ["git", "apply", "--3way", "-"],
        input=diff,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=original,
        timeout=30,
    )
    if result.returncode != 0:
        logger.error("git apply failed: %s", result.stderr.strip())
        return False

    # Update session status on disk.
    updated = PlaygroundSession(
        session_id=session.session_id,
        original_dir=session.original_dir,
        sandbox_dir=session.sandbox_dir,
        created_at=session.created_at,
        status="applied",
    )
    save_session(updated, _sessions_dir(original))
    logger.info("Applied sandbox %s changes to %s", session.session_id, original)
    return True


def discard_sandbox(session: PlaygroundSession) -> bool:
    """Clean up a sandbox directory and mark the session as discarded.

    If the sandbox was created via ``git worktree``, this also removes
    the worktree reference.

    Args:
        session: The playground session to discard.

    Returns:
        ``True`` if cleanup succeeded, ``False`` otherwise.
    """
    sandbox = Path(session.sandbox_dir)
    original = Path(session.original_dir)

    # Try removing via git worktree first.
    _run_git(["worktree", "remove", "--force", str(sandbox)], cwd=original)

    # Fallback: plain directory removal.
    if sandbox.is_dir():
        try:
            shutil.rmtree(sandbox)
        except OSError:
            logger.warning("Failed to remove sandbox directory: %s", sandbox)
            return False

    # Update session status on disk.
    updated = PlaygroundSession(
        session_id=session.session_id,
        original_dir=session.original_dir,
        sandbox_dir=session.sandbox_dir,
        created_at=session.created_at,
        status="discarded",
    )
    save_session(updated, _sessions_dir(original))
    logger.info("Discarded playground sandbox %s", session.session_id)
    return True


def list_active_sessions(workdir: Path) -> list[PlaygroundSession]:
    """Return all playground sessions with ``status == 'active'``.

    Args:
        workdir: The project directory whose ``.sdd/playground`` folder
            is inspected.

    Returns:
        List of active sessions, sorted by creation time.
    """
    sessions = load_sessions(_sessions_dir(workdir))
    return [s for s in sessions if s.status == "active"]
