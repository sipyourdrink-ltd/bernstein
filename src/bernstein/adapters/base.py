"""Base adapter for CLI coding agents."""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from bernstein.core.platform_compat import kill_process_group, process_alive
from bernstein.core.resource_limits import ResourceLimits, make_preexec_fn

if TYPE_CHECKING:
    from collections.abc import Callable
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
    workdir: Path,
    log_path: Path,
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
        workdir: Project root directory.
        log_path: Path to the agent log file.
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
        "--workdir",
        str(workdir),
        "--log-path",
        str(log_path),
        "--model",
        model,
        "--",
        *cmd,
    ]


class CLIAdapter(ABC):
    """Interface for launching and monitoring CLI coding agents.

    Implement this for each supported CLI (Claude Code, Codex, Gemini, etc.).
    """

    def __init__(self) -> None:
        self._resource_limits: ResourceLimits | None = None

    def set_resource_limits(self, limits: ResourceLimits | None) -> None:
        """Configure OS-level resource limits applied to spawned child processes.

        Must be called before :meth:`spawn`.  On POSIX, limits are enforced via
        ``resource.setrlimit`` in the child process ``preexec_fn``.  On other
        platforms the limits are recorded but not enforced.

        Args:
            limits: Resource limits to apply, or ``None`` to clear limits.
        """
        self._resource_limits = limits

    def _get_preexec_fn(self) -> "Callable[[], None] | None":
        """Return a preexec_fn for subprocess.Popen based on configured limits.

        Returns:
            A zero-argument callable to pass as ``preexec_fn``, or ``None``
            when no limits are configured or the platform does not support it.
        """
        if self._resource_limits is None:
            return None
        return make_preexec_fn(self._resource_limits)

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
            if not kill_process_group(pid, signal.SIGTERM):
                return  # Already dead

            # Grace period for agent to commit partial work
            deadline = time.monotonic() + _SIGTERM_GRACE_SECONDS
            while time.monotonic() < deadline:
                if not process_alive(pid):
                    return  # Exited cleanly after SIGTERM
                time.sleep(1)

            logger.warning(
                "Agent did not exit after SIGTERM grace period: pid=%d session=%s — sending SIGKILL",
                pid,
                session_id,
            )
            kill_process_group(pid, signal.SIGKILL)

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
            "you've hit your limit",
            "hit your limit",
            "limit exceeded",
            "resets",  # "resets Apr 5 at 10pm" pattern from Claude Code
        )
        return any(needle in text for needle in needles)

    def _probe_fast_exit(
        self,
        proc: WaitableProcess,
        log_path: Path,
        *,
        provider_name: str,
        timeout_seconds: float = 8.0,
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
        return process_alive(pid)

    def kill(self, pid: int) -> None:
        """Terminate the agent process and its entire process group.

        Processes are spawned with ``start_new_session=True``, so the PID
        equals the PGID.  Using the PID directly avoids ``os.getpgid()``
        failing when the wrapper process has already exited — this prevents
        orphan child processes from accumulating.
        """
        kill_process_group(pid, signal.SIGTERM)

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

    def supports_auth_refresh(self) -> bool:
        """Return True if this adapter supports automated auth refresh (OAuth)."""
        return False

    def refresh_auth(self, _workdir: Path) -> bool:
        """Attempt to refresh authentication credentials.

        Returns:
            True if refresh was successful, False otherwise.
        """
        return False

    def is_rate_limited(self) -> bool:
        """Check if the provider is currently rate-limited.

        Subclasses should override this to probe the CLI for rate-limit
        signals before spawning.  Default returns False (no check).

        Returns:
            True if the provider is known to be rate-limited right now.
        """
        return False

    def cancel_tool_batch(self, _session_id: str, _batch_id: str) -> None:
        """Abort all pending tool calls in a batch.

        Optional: implemented by adapters that support concurrent tool execution.

        Args:
            _session_id: Agent session ID.
            _batch_id: The batch identifier to cancel.
        """
        return
