"""Base adapter for CLI coding agents."""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import AbortReason, ApiTierInfo, ModelConfig

logger = logging.getLogger(__name__)

# Default timeout for spawned agent processes (30 minutes).
DEFAULT_TIMEOUT_SECONDS: int = 1800

# Grace period between SIGTERM and SIGKILL (seconds).
_SIGTERM_GRACE_SECONDS: int = 30


class SpawnError(RuntimeError):
    """Raised when an adapter process exits too early to be treated as spawned."""


class RateLimitError(SpawnError):
    """Raised when an adapter detects provider-side rate limiting on startup."""


@dataclass
class SpawnResult:
    """Result of spawning an agent process."""

    pid: int
    log_path: Path
    proc: object | None = None  # subprocess.Popen, kept for poll()-based alive check
    timeout_timer: threading.Timer | None = field(default=None, repr=False)
    abort_reason: AbortReason | None = None
    abort_detail: str = ""
    finish_reason: str = ""


class WaitableProcess(Protocol):
    """Minimal process protocol for fast-exit probing."""

    def wait(self, timeout: float | None = None) -> object:
        """Wait for process completion and return its exit status."""


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
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> SpawnResult:
        """Launch an agent process with the given prompt."""
        ...

    def _start_timeout_watchdog(
        self,
        pid: int,
        timeout_seconds: int,
        session_id: str,
    ) -> threading.Timer:
        """Start a watchdog timer that kills the process on timeout.

        Sends SIGTERM first, waits 30s for graceful shutdown, then SIGKILL.

        Args:
            pid: Process ID to monitor.
            timeout_seconds: Seconds before triggering timeout.
            session_id: Session identifier for structured logging.

        Returns:
            The started Timer — caller should store it for cancellation.
        """

        def _kill_on_timeout() -> None:
            logger.warning(
                "Timeout after %ds: pid=%d session=%s — sending SIGTERM",
                timeout_seconds,
                pid,
                session_id,
            )
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except OSError:
                return  # Already dead

            # Grace period for agent to commit partial work
            deadline = time.monotonic() + _SIGTERM_GRACE_SECONDS
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except OSError:
                    return  # Exited cleanly after SIGTERM
                time.sleep(1)

            logger.warning(
                "Agent did not exit after SIGTERM grace period: pid=%d session=%s — sending SIGKILL",
                pid,
                session_id,
            )
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(pid), signal.SIGKILL)

        timer = threading.Timer(timeout_seconds, _kill_on_timeout)
        timer.daemon = True
        timer.name = f"timeout-watchdog-{session_id}"
        timer.start()
        return timer

    @staticmethod
    def _read_last_lines(log_path: Path, n: int = 10) -> list[str]:
        """Read the last *n* lines from a log file."""
        try:
            return log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        except OSError:
            return []

    @staticmethod
    def _is_rate_limit_error(lines: list[str]) -> bool:
        """Return True when log lines contain a provider rate-limit signal."""
        text = "\n".join(lines).lower()
        needles = (
            "rate limit",
            "usage limit",
            "quota exceeded",
            "too many requests",
            "429",
            "overloaded",
        )
        return any(needle in text for needle in needles)

    def _probe_fast_exit(
        self,
        proc: WaitableProcess,
        log_path: Path,
        *,
        provider_name: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        """Treat early non-zero exits as spawn failures instead of live sessions.

        Args:
            proc: Subprocess-like object with ``wait(timeout=...)``.
            log_path: Runtime log path for tail inspection.
            provider_name: Human-readable provider/adapter label for errors.
            timeout_seconds: Probe window after spawn.

        Raises:
            RateLimitError: Provider immediately exited due to rate limiting.
            SpawnError: Provider immediately exited for another reason.
        """
        try:
            exit_code = proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return
        except Exception as exc:
            logger.debug("Fast-exit probe failed for %s: %s", provider_name, exc)
            return

        if not isinstance(exit_code, int):
            logger.debug("Fast-exit probe for %s returned non-integer exit code %r; skipping", provider_name, exit_code)
            return

        if exit_code == 0:
            return

        tail_lines = self._read_last_lines(log_path, n=10)
        tail_text = tail_lines[-1] if tail_lines else "(no log output)"
        if self._is_rate_limit_error(tail_lines):
            raise RateLimitError(f"{provider_name} rate-limited during startup: {tail_text}")
        raise SpawnError(f"{provider_name} exited early with code {exit_code}: {tail_text}")

    @staticmethod
    def cancel_timeout(result: SpawnResult) -> None:
        """Cancel the timeout watchdog for a completed process."""
        if result.timeout_timer is not None:
            result.timeout_timer.cancel()
            result.timeout_timer = None

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
