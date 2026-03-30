"""Base adapter for CLI coding agents."""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import sys
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ApiTierInfo, ModelConfig

_log = logging.getLogger(__name__)


@dataclass
class SpawnResult:
    """Result of spawning an agent process."""

    pid: int
    log_path: Path
    proc: object | None = None  # subprocess.Popen, kept for poll()-based alive check
    timer: threading.Timer | None = None  # watchdog; cancel on normal process exit


def build_worker_cmd(
    cmd: list[str],
    *,
    role: str,
    session_id: str,
    pid_dir: Path,
    model: str = "",
) -> list[str]:
    """Wrap a CLI command with bernstein-worker for process visibility.

    The worker sets the process title to "bernstein: <role> [<session>]"
    and writes a PID metadata file for ``bernstein ps``.

    Args:
        cmd: The original CLI command to wrap.
        role: Agent role (qa, backend, etc.).
        session_id: Unique session identifier.
        pid_dir: Directory for PID metadata JSON files.
        model: Model name for metadata display.

    Returns:
        Wrapped command list.
    """
    return [
        sys.executable,
        "-m",
        "bernstein.core.worker",
        "--role",
        role,
        "--session",
        session_id,
        "--pid-dir",
        str(pid_dir),
        "--model",
        model,
        "--",
        *cmd,
    ]


class CLIAdapter(ABC):
    """Interface for launching and monitoring CLI coding agents.

    Implement this for each supported CLI (Claude Code, Codex, Gemini, etc.).
    """

    @abstractmethod
    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 1800,
    ) -> SpawnResult:
        """Launch an agent process with the given prompt."""
        ...

    def _start_watchdog(
        self,
        proc: subprocess.Popen[bytes],
        *,
        timeout_seconds: int,
        workdir: Path,
        session_id: str,
    ) -> threading.Timer:
        """Start a watchdog timer that kills proc after timeout_seconds.

        On timeout: commits partial work, sends SIGTERM, waits 30 s, then SIGKILL.
        The returned timer should be cancelled when the process exits normally.

        Args:
            proc: The subprocess to watch.
            timeout_seconds: Seconds before the watchdog fires.
            workdir: Agent working directory (used for git commit of partial work).
            session_id: Session identifier, included in log messages.

        Returns:
            The started threading.Timer (daemon, so it won't block interpreter exit).
        """

        def _on_timeout() -> None:
            if proc.poll() is not None:
                return  # already exited normally

            _log.warning(
                "Agent timed out — killing",
                extra={
                    "session_id": session_id,
                    "timeout_seconds": timeout_seconds,
                    "reason": "timeout",
                },
            )

            # Preserve partial work before sending signals
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=workdir,
                    capture_output=True,
                    timeout=10,
                )
                subprocess.run(
                    ["git", "commit", "-m", f"[WIP] timeout: {session_id}"],
                    cwd=workdir,
                    capture_output=True,
                    timeout=10,
                )

            # SIGTERM first
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)

            # SIGKILL after 30 s if still alive
            def _force_kill() -> None:
                if proc.poll() is None:
                    with contextlib.suppress(OSError):
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

            force_timer = threading.Timer(30.0, _force_kill)
            force_timer.daemon = True
            force_timer.start()

        timer = threading.Timer(float(timeout_seconds), _on_timeout)
        timer.daemon = True
        timer.start()
        return timer

    def is_alive(self, pid: int) -> bool:
        """Check if the agent process is still running."""
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def kill(self, pid: int) -> None:
        """Terminate the agent process."""
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(pid), signal.SIGTERM)

    @abstractmethod
    def name(self) -> str:
        """Human-readable name of this CLI adapter."""
        ...

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect the current API tier and remaining quota.

        Returns:
            ApiTierInfo if tier detection is supported and successful, None otherwise.
            Subclasses should override this to return provider-specific tier info.
        """
        return None
