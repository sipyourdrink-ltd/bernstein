"""Agent signal file protocol: WAKEUP, SHUTDOWN, and HEARTBEAT.

The orchestrator writes signal files to `.sdd/runtime/signals/{session_id}/`
to communicate with running agents. Agents periodically check for these files
and respond accordingly. Agents write heartbeats to
`.sdd/runtime/heartbeats/{session_id}.json` so the orchestrator can detect
stuck processes.

Signal cascade:
  - No heartbeat for 60s  → write WAKEUP
  - No heartbeat for 120s → write SHUTDOWN
  - No heartbeat for 180s → kill process, mark task for retry
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING

from bernstein.core.models import AgentHeartbeat

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class AgentSignalManager:
    """Read and write agent signal files for a given working directory.

    Args:
        workdir: Root of the project (parent of `.sdd/`).
    """

    def __init__(self, workdir: Path) -> None:
        self._signals_dir = workdir / ".sdd" / "runtime" / "signals"
        self._heartbeats_dir = workdir / ".sdd" / "runtime" / "heartbeats"

    # ------------------------------------------------------------------
    # WAKEUP
    # ------------------------------------------------------------------

    def write_wakeup(
        self,
        session_id: str,
        task_title: str,
        elapsed_s: float,
        last_activity_ago_s: float,
    ) -> None:
        """Write a WAKEUP signal file to a stuck agent's signal directory.

        Args:
            session_id: The agent's session ID.
            task_title: Title of the task the agent is working on.
            elapsed_s: Total elapsed seconds since the agent was spawned.
            last_activity_ago_s: Seconds since the last detected activity.
        """
        signal_dir = self._signals_dir / session_id
        signal_dir.mkdir(parents=True, exist_ok=True)

        elapsed_min = int(elapsed_s // 60)
        last_ago_min = int(last_activity_ago_s // 60)

        content = (
            f"# WAKEUP — You may be stuck\n"
            f"Your task: {task_title}\n"
            f"Time elapsed: {elapsed_min}m {int(elapsed_s % 60)}s\n"
            f"Last activity: {last_ago_min}m {int(last_activity_ago_s % 60)}s ago\n\n"
            f"If you're stuck:\n"
            f"1. Save your current progress (git add + commit WIP)\n"
            f"2. Report status to task server\n"
            f"3. Continue working or exit if blocked\n"
        )
        (signal_dir / "WAKEUP").write_text(content, encoding="utf-8")
        logger.info("WAKEUP signal written for agent %s", session_id)

    # ------------------------------------------------------------------
    # SHUTDOWN
    # ------------------------------------------------------------------

    def write_shutdown(self, session_id: str, reason: str, task_title: str) -> None:
        """Write a SHUTDOWN signal file telling the agent to save and exit.

        Args:
            session_id: The agent's session ID.
            reason: Human-readable reason for the shutdown.
            task_title: Title of the task the agent is working on.
        """
        signal_dir = self._signals_dir / session_id
        signal_dir.mkdir(parents=True, exist_ok=True)

        content = (
            f"# SHUTDOWN — Save and exit\n"
            f"Reason: {reason}\n"
            f"You have 30 seconds to:\n"
            f'1. Save all current work (git add + commit "[WIP] {task_title}")\n'
            f"2. Report partial progress to task server\n"
            f"3. Exit cleanly\n"
        )
        (signal_dir / "SHUTDOWN").write_text(content, encoding="utf-8")
        logger.info("SHUTDOWN signal written for agent %s (reason: %s)", session_id, reason)

    # ------------------------------------------------------------------
    # HEARTBEAT
    # ------------------------------------------------------------------

    def write_heartbeat(self, session_id: str, heartbeat: AgentHeartbeat) -> None:
        """Write an agent heartbeat file.

        Args:
            session_id: The agent's session ID.
            heartbeat: Heartbeat data to persist.
        """
        self._heartbeats_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": heartbeat.timestamp,
            "files_changed": heartbeat.files_changed,
            "status": heartbeat.status,
            "current_file": heartbeat.current_file,
        }
        hb_file = self._heartbeats_dir / f"{session_id}.json"
        hb_file.write_text(json.dumps(payload), encoding="utf-8")

    def read_heartbeat(self, session_id: str) -> AgentHeartbeat | None:
        """Read the latest heartbeat for a session.

        Args:
            session_id: The agent's session ID.

        Returns:
            Parsed AgentHeartbeat, or None if the file is missing or malformed.
        """
        hb_file = self._heartbeats_dir / f"{session_id}.json"
        if not hb_file.exists():
            return None
        try:
            raw = json.loads(hb_file.read_text(encoding="utf-8"))
            return AgentHeartbeat(
                timestamp=float(raw["timestamp"]),
                files_changed=int(raw.get("files_changed", 0)),
                status=str(raw.get("status", "working")),
                current_file=str(raw.get("current_file", "")),
            )
        except Exception as exc:
            logger.warning("Failed to parse heartbeat for %s: %s", session_id, exc)
            return None

    def is_stale(self, session_id: str, stale_after_s: float) -> bool:
        """Return True if the agent's heartbeat is older than *stale_after_s*.

        Returns False when no heartbeat file exists (agent may not support
        heartbeats yet, so we err on the side of not killing it).

        Args:
            session_id: The agent's session ID.
            stale_after_s: Seconds after which an agent is considered stale.

        Returns:
            True if the last heartbeat is older than *stale_after_s*, else False.
        """
        hb = self.read_heartbeat(session_id)
        if hb is None:
            return False
        return (time.time() - hb.timestamp) > stale_after_s

    # ------------------------------------------------------------------
    # COMMAND (broadcast)
    # ------------------------------------------------------------------

    def write_command_signal(self, session_id: str, message: str) -> bool:
        """Write a COMMAND signal file with an arbitrary message.

        Args:
            session_id: The agent's session ID.
            message: Free-form instruction text for the agent.

        Returns:
            True on success, False on failure.
        """
        try:
            signal_dir = self._signals_dir / session_id
            signal_dir.mkdir(parents=True, exist_ok=True)
            (signal_dir / "COMMAND").write_text(message, encoding="utf-8")
            logger.info("COMMAND signal written for agent %s", session_id)
            return True
        except OSError as exc:
            logger.warning("Failed to write COMMAND signal for %s: %s", session_id, exc)
            return False

    def write_command_signals_all(self, message: str) -> int:
        """Write a COMMAND signal to ALL active session directories.

        Args:
            message: Free-form instruction text for every agent.

        Returns:
            Number of session directories the signal was written to.
        """
        if not self._signals_dir.exists():
            return 0
        count = 0
        for child in self._signals_dir.iterdir():
            if child.is_dir() and self.write_command_signal(child.name, message):
                count += 1
        return count

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def clear_signals(self, session_id: str) -> None:
        """Remove signal files and heartbeat for a given session.

        Idempotent: does not raise if files/directories do not exist.

        Args:
            session_id: The agent's session ID.
        """
        signal_dir = self._signals_dir / session_id
        for name in ("WAKEUP", "SHUTDOWN", "COMMAND"):
            with contextlib.suppress(OSError):
                (signal_dir / name).unlink()
        with contextlib.suppress(OSError):
            signal_dir.rmdir()

        hb_file = self._heartbeats_dir / f"{session_id}.json"
        with contextlib.suppress(OSError):
            hb_file.unlink()
