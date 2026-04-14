"""Zombie process cleanup on orchestrator startup (AGENT-006).

On startup, scans ``.sdd/runtime/pids/`` for PID files from prior runs,
checks if the recorded processes are still alive, and sends SIGTERM to
orphaned agents.  This prevents resource leaks from crashed orchestrator
sessions.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import signal
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.defaults import AGENT
from bernstein.core.platform_compat import IS_WINDOWS, kill_process, process_alive

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Pattern for sanitizing user-controlled strings before logging.
_SAFE_LOG_RE = re.compile(r"[^a-zA-Z0-9._\-]")


def _sanitize_for_log(value: str, max_len: int = 128) -> str:
    """Sanitize a user-controlled string for safe logging."""
    return _SAFE_LOG_RE.sub("_", value[:max_len])


#: Grace period between SIGTERM and SIGKILL for orphaned agents (seconds).
DEFAULT_SIGTERM_GRACE_S: int = 10

#: Maximum age for a PID file to be considered (seconds).  Files older than
#: this are stale metadata from very old runs and are simply deleted.
MAX_PID_FILE_AGE_S: float = AGENT.zombie_pid_max_age_s


@dataclass(frozen=True)
class OrphanedAgent:
    """An orphaned agent process found during startup cleanup.

    Attributes:
        session_id: The session identifier from the PID file name.
        pid: The process ID recorded in the PID file.
        worker_pid: The worker process ID, if recorded separately.
        role: Agent role, if recorded.
        killed: Whether the process was successfully killed.
        reason: Explanation of the cleanup action taken.
    """

    session_id: str
    pid: int
    worker_pid: int = 0
    role: str = ""
    killed: bool = False
    reason: str = ""


@dataclass
class CleanupResult:
    """Result of the zombie cleanup scan.

    Attributes:
        scanned: Number of PID files examined.
        orphans_found: Number of orphaned (still alive) processes found.
        orphans_killed: Number of orphaned processes successfully terminated.
        stale_removed: Number of stale PID files removed (process already dead).
        task_count: Total number of tasks associated with scanned agent sessions.
        errors: List of error messages for any failed cleanup attempts.
    """

    scanned: int = 0
    orphans_found: int = 0
    orphans_killed: int = 0
    stale_removed: int = 0
    task_count: int = 0
    errors: list[str] = field(default_factory=list[str])

    def summary(self) -> str:
        """Return a human-readable one-line summary of the cleanup results."""
        parts = [
            f"scanned={self.scanned}",
            f"tasks={self.task_count}",
            f"orphans={self.orphans_found}",
            f"killed={self.orphans_killed}",
            f"stale={self.stale_removed}",
        ]
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return " ".join(parts)


def _read_pid_file(pid_file: Path) -> dict[str, Any]:
    """Read and parse a PID metadata file.

    PID files are JSON with at minimum a ``pid`` or ``worker_pid`` key.

    Args:
        pid_file: Path to the PID JSON file.

    Returns:
        Dict with parsed fields (pid, worker_pid, role, etc.).
        Returns empty dict on parse failure.
    """
    try:
        data = json.loads(pid_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _terminate_process(pid: int, *, grace_seconds: int = DEFAULT_SIGTERM_GRACE_S) -> bool:
    """Send SIGTERM to a process and wait, then SIGKILL if needed.

    Args:
        pid: Process ID to terminate.
        grace_seconds: Seconds to wait after SIGTERM before SIGKILL.

    Returns:
        True if the process was killed or already dead, False on failure.
    """
    if pid <= 0:
        return False

    if not process_alive(pid):
        return True

    # Send SIGTERM
    if not kill_process(pid, signal.SIGTERM):
        return not process_alive(pid)

    # Wait for graceful shutdown
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.5)

    # Process still alive — force kill
    if process_alive(pid):
        logger.warning("Process %d did not exit after SIGTERM, sending SIGKILL", pid)
        if IS_WINDOWS:
            kill_process(pid, signal.SIGTERM)  # Windows uses taskkill /F
        else:
            kill_process(pid, signal.SIGKILL)
        time.sleep(1)

    return not process_alive(pid)


def scan_and_cleanup_zombies(
    workdir: Path,
    *,
    grace_seconds: int = DEFAULT_SIGTERM_GRACE_S,
    dry_run: bool = False,
) -> CleanupResult:
    """Scan PID files and terminate orphaned agent processes.

    Reads all JSON files in ``.sdd/runtime/pids/``, checks if the recorded
    processes are still alive, and terminates orphaned ones.  Stale PID files
    (where the process is already dead) are removed.

    Args:
        workdir: Project working directory.
        grace_seconds: Seconds to wait between SIGTERM and SIGKILL.
        dry_run: If True, report orphans but do not kill them.

    Returns:
        CleanupResult with statistics and any errors.
    """
    pid_dir = workdir / ".sdd" / "runtime" / "pids"
    result = CleanupResult()

    if not pid_dir.is_dir():
        return result

    now = time.time()

    for pid_file in pid_dir.iterdir():
        if not pid_file.is_file() or not pid_file.name.endswith(".json"):
            continue
        _process_pid_file(pid_file, result, now, grace_seconds, dry_run)

    return result


def _remove_stale_pid_file(pid_file: Path, result: CleanupResult) -> None:
    """Remove a stale PID file and increment the counter."""
    try:
        pid_file.unlink(missing_ok=True)
        result.stale_removed += 1
    except OSError:
        pass


def _extract_pids(data: dict[str, Any]) -> list[int]:
    """Extract PIDs from a PID file data dict."""
    pids: list[int] = []
    worker_pid = int(data.get("worker_pid", 0) or 0)
    main_pid = int(data.get("pid", 0) or 0)
    if worker_pid > 0:
        pids.append(worker_pid)
    if main_pid > 0 and main_pid != worker_pid:
        pids.append(main_pid)
    return pids


def _process_pid_file(
    pid_file: Path,
    result: CleanupResult,
    now: float,
    grace_seconds: int,
    dry_run: bool,
) -> None:
    """Process a single PID file for zombie cleanup."""
    result.scanned += 1
    session_id = pid_file.stem

    data = _read_pid_file(pid_file)
    if not data:
        _remove_stale_pid_file(pid_file, result)
        return

    task_ids_raw = data.get("task_ids")
    result.task_count += len(task_ids_raw) if isinstance(task_ids_raw, list) else 1

    try:
        file_mtime = pid_file.stat().st_mtime
    except OSError:
        file_mtime = 0.0
    if file_mtime > 0 and (now - file_mtime) > MAX_PID_FILE_AGE_S:
        logger.debug("Removing ancient PID file: %s (age %.0f days)", pid_file.name, (now - file_mtime) / 86400)
        _remove_stale_pid_file(pid_file, result)
        return

    pids_to_check = _extract_pids(data)
    if not pids_to_check:
        _remove_stale_pid_file(pid_file, result)
        return

    alive_pids = [p for p in pids_to_check if process_alive(p)]
    if not alive_pids:
        logger.debug("Stale PID file (process dead): %s", session_id)
        _remove_stale_pid_file(pid_file, result)
        return

    result.orphans_found += 1
    role = str(data.get("role", "unknown"))
    for alive_pid in alive_pids:
        logger.warning(
            "Orphaned agent process: session=%s pid=%d role=%s",
            _sanitize_for_log(session_id),
            alive_pid,
            _sanitize_for_log(role),
        )
        if dry_run:
            continue
        killed = _terminate_process(alive_pid, grace_seconds=grace_seconds)
        if killed:
            result.orphans_killed += 1
            logger.info("Terminated orphaned agent: session=%s pid=%d", session_id, alive_pid)
        else:
            error_msg = f"Failed to terminate orphan: session={session_id} pid={alive_pid}"
            result.errors.append(error_msg)
            logger.error(error_msg)

    if not dry_run:
        with contextlib.suppress(OSError):
            pid_file.unlink(missing_ok=True)
