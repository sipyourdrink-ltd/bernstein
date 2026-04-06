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
import signal
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.platform_compat import IS_WINDOWS, kill_process, process_alive

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Grace period between SIGTERM and SIGKILL for orphaned agents (seconds).
DEFAULT_SIGTERM_GRACE_S: int = 10

#: Maximum age for a PID file to be considered (seconds).  Files older than
#: this are stale metadata from very old runs and are simply deleted.
MAX_PID_FILE_AGE_S: float = 7 * 24 * 3600  # 7 days


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
        errors: List of error messages for any failed cleanup attempts.
    """

    scanned: int = 0
    orphans_found: int = 0
    orphans_killed: int = 0
    stale_removed: int = 0
    errors: list[str] = field(default_factory=list[str])


def _read_pid_file(pid_file: Path) -> dict[str, int | str]:
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
            return data  # type: ignore[return-value]
    except (OSError, json.JSONDecodeError, ValueError):
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

        result.scanned += 1
        session_id = pid_file.stem

        data = _read_pid_file(pid_file)
        if not data:
            # Corrupt PID file — remove it
            try:
                pid_file.unlink(missing_ok=True)
                result.stale_removed += 1
            except OSError:
                pass
            continue

        # Check file age — very old PID files are just stale metadata
        try:
            file_mtime = pid_file.stat().st_mtime
        except OSError:
            file_mtime = 0.0
        if file_mtime > 0 and (now - file_mtime) > MAX_PID_FILE_AGE_S:
            logger.debug("Removing ancient PID file: %s (age %.0f days)", pid_file.name, (now - file_mtime) / 86400)
            try:
                pid_file.unlink(missing_ok=True)
                result.stale_removed += 1
            except OSError:
                pass
            continue

        # Extract PIDs — check both worker_pid and pid
        pids_to_check: list[int] = []
        worker_pid = int(data.get("worker_pid", 0) or 0)
        main_pid = int(data.get("pid", 0) or 0)
        if worker_pid > 0:
            pids_to_check.append(worker_pid)
        if main_pid > 0 and main_pid != worker_pid:
            pids_to_check.append(main_pid)

        if not pids_to_check:
            try:
                pid_file.unlink(missing_ok=True)
                result.stale_removed += 1
            except OSError:
                pass
            continue

        # Check if any recorded PID is still alive
        alive_pids = [p for p in pids_to_check if process_alive(p)]

        if not alive_pids:
            # Process already dead — clean up the PID file
            logger.debug("Stale PID file (process dead): %s", session_id)
            try:
                pid_file.unlink(missing_ok=True)
                result.stale_removed += 1
            except OSError:
                pass
            continue

        # Orphaned process found
        result.orphans_found += 1
        role = str(data.get("role", "unknown"))

        for alive_pid in alive_pids:
            logger.warning(
                "Orphaned agent process: session=%s pid=%d role=%s",
                session_id,
                alive_pid,
                role,
            )

            if dry_run:
                continue

            killed = _terminate_process(alive_pid, grace_seconds=grace_seconds)
            if killed:
                result.orphans_killed += 1
                logger.info(
                    "Terminated orphaned agent: session=%s pid=%d",
                    session_id,
                    alive_pid,
                )
            else:
                error_msg = f"Failed to terminate orphan: session={session_id} pid={alive_pid}"
                result.errors.append(error_msg)
                logger.error(error_msg)

        # Clean up PID file after termination
        if not dry_run:
            with contextlib.suppress(OSError):
                pid_file.unlink(missing_ok=True)

    return result
