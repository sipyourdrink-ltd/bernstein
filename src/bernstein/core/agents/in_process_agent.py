"""In-process agent backend — run agents inside a thread of the same Python process.

Provides a subprocess-compatible interface for agents that run inside the
orchestrator process (no separate OS process per agent).  Use with caution:
a crash (e.g. segfault, uncaught ``SystemExit``) can take down the task server.

The ``InProcessAgent`` wraps a ``CLIAdapter`` and intercepts its ``spawn``
call, running it inside a daemon thread.  The adapter spawns a subprocess
internally — the "in-process" refers to how the *orchestrator* tracks the
agent lifecycle.

Typical usage::

    backend = InProcessAgent(adapter, workdir, pid_dir=...)
    pid, log_path = backend.run(prompt=prompt, ...)
    alive = backend.is_alive(session_id)
    exit_code = backend.wait(session_id)
    backend.stop(session_id)
    backend.cleanup(session_id)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.adapters.base import CLIAdapter, SpawnResult
    from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal result record
# ---------------------------------------------------------------------------


@dataclass
class _ThreadResult:
    """Internal: tracks an in-process agent session."""

    session_id: str
    adapter_name: str
    pid: int  # synthetic PID (thread ident)
    log_path: Path
    thread: threading.Thread
    started_at: float
    finished_at: float = 0.0
    exit_code: int | None = None
    error_detail: str = ""
    stop_requested: bool = False


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class InProcessAgent:
    """Run an agent inside a daemon thread of the current process.

    Crash isolation: the thread traps ``SystemExit`` so the main process
    survives accidental exits from agent code.

    Args:
        adapter: CLIAdapter to run (e.g. ClaudeCodeAdapter).
        workdir: Project root directory.
        pid_dir: Directory for PID metadata (optional, for ``bernstein ps``).
    """

    def __init__(
        self,
        adapter: CLIAdapter,
        workdir: Path,
        pid_dir: Path | None = None,
    ) -> None:
        self._adapter = adapter
        self._workdir = workdir
        self._pid_dir = pid_dir
        self._sessions: dict[str, _ThreadResult] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def name(self) -> str:
        """Human-readable backend name."""
        return f"in-process:{self._adapter.name()}"

    def run(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 1800,
    ) -> tuple[int, Path]:
        """Start an in-process agent run and return (pid, log_path).

        Spawns the adapter's subprocess inside a daemon thread.  The thread
        blocks on the adapter call until the subprocess finishes.

        Args:
            prompt: Full prompt text for the agent.
            workdir: Working directory for the agent.
            model_config: Model and effort configuration.
            session_id: Unique session identifier.
            mcp_config: Optional MCP server configuration.
            timeout_seconds: Timeout in seconds (default 30 minutes).

        Returns:
            Tuple of (synthetic PID, log path).
        """
        log_dir = workdir / ".sdd" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{session_id}.log"

        # Reserve a session record with a synthetic PID so the caller
        # can report it immediately.  We will update it once the thread starts.
        fake_pid = _next_pid()

        thread = threading.Thread(
            target=self._run_worker,
            args=(
                prompt,
                workdir,
                model_config,
                session_id,
                mcp_config,
                timeout_seconds,
                log_path,
            ),
            name=f"in-process-agent-{session_id}",
            daemon=True,
        )

        session = _ThreadResult(
            session_id=session_id,
            adapter_name=self._adapter.name(),
            pid=fake_pid,
            log_path=log_path,
            thread=thread,
            started_at=time.time(),
        )

        with self._lock:
            self._sessions[session_id] = session

        thread.start()

        # Wait briefly for thread to get real thread ident
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and thread.ident is None:
            time.sleep(0.01)

        real_tid = thread.ident if thread.ident else fake_pid
        with self._lock:
            session.pid = real_tid

        if self._pid_dir is not None:
            self._pid_dir.mkdir(parents=True, exist_ok=True)
            pid_file = self._pid_dir / f"{session_id}.json"
            pid_file.write_text(
                json.dumps(
                    {
                        "worker_pid": real_tid,
                        "role": session_id.rsplit("-", 1)[0],
                        "session": session_id,
                        "command": self._adapter.name(),
                        "started_at": time.time(),
                        "backend": "in_process",
                    }
                ),
                encoding="utf-8",
            )

        return real_tid, log_path

    def is_alive(self, session_id: str) -> bool:
        """Check if the agent thread is still running.

        Args:
            session_id: Session to check.

        Returns:
            True if the thread is still alive and has not yet completed.
        """
        with self._lock:
            session = self._sessions.get(session_id)

        if session is None:
            return False

        return session.thread.is_alive()

    def wait(self, session_id: str, timeout: float | None = None) -> int | None:
        """Wait for a running agent to finish.

        Args:
            session_id: Session to wait for.
            timeout: Maximum seconds to wait.  None = block indefinitely.

        Returns:
            Exit code (0 = success, non-zero = failure), or None if the
            session is unknown or still running after timeout.
        """
        with self._lock:
            session = self._sessions.get(session_id)

        if session is None:
            logger.debug("wait: unknown session %s", session_id)
            return None

        session.thread.join(timeout=timeout)

        if session.thread.is_alive():
            return None

        # Thread finished — return exit_code if available
        with self._lock:
            finished = self._sessions.get(session_id)

        if finished is not None and finished.exit_code is not None:
            return finished.exit_code

        # Thread finished but exit_code not set — assume success
        with self._lock:
            if session_id in self._sessions and self._sessions[session_id].exit_code is None:
                self._sessions[session_id].exit_code = 0
                self._sessions[session_id].finished_at = time.time()
        return 0

    def stop(self, session_id: str) -> None:
        """Kill a running in-process agent.

        Since Python threads cannot be forcibly killed, we write a
        SHUTDOWN signal file and flag the session.  The adapter's subprocess
        should check for this and exit gracefully.

        Args:
            session_id: Session to stop.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.stop_requested = True

        # Write SHUTDOWN signal file (convention used by all agents)
        signal_dir = self._workdir / ".sdd" / "runtime" / "signals"
        signal_file = signal_dir / session_id / "SHUTDOWN"
        signal_file.parent.mkdir(parents=True, exist_ok=True)
        signal_file.write_text("stop", encoding="utf-8")

        logger.info("Sent shutdown signal to in-process agent %s", session_id)

    def cleanup(self, session_id: str) -> None:
        """Remove a session from the internal registry after it has ended.

        Args:
            session_id: Session to remove.
        """
        with self._lock:
            self._sessions.pop(session_id, None)

    @property
    def active_sessions(self) -> dict[str, _ThreadResult]:
        """Return a copy of active session records."""
        with self._lock:
            return dict(self._sessions)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_worker(
        self,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None,
        timeout_seconds: int,
        log_path: Path,
    ) -> None:
        """Target function for the agent thread.

        Traps SystemExit to prevent agent code from killing the process.
        Writes result metadata when done.

        Args:
            prompt: Full prompt passed to the adapter.
            workdir: Working directory.
            model_config: Model config.
            session_id: Session identifier.
            mcp_config: MCP config for the agent.
            timeout_seconds: Timeout for the spawned subprocess.
            log_path: Where to write agent output.
        """
        exit_code: int | None = None
        error_detail = ""

        try:
            result: SpawnResult = self._adapter.spawn(
                prompt=prompt,
                workdir=workdir,
                model_config=model_config,
                session_id=session_id,
                mcp_config=mcp_config,
                timeout_seconds=timeout_seconds,
            )

            # so that ``is_alive()`` and ``wait()`` reflect the true status.
            proc = result.proc
            if proc is not None:
                _proc_waitable: Any = proc
                if callable(getattr(_proc_waitable, "wait", None)):
                    exit_code = _proc_waitable.wait()
                elif result.pid not in (0, None):
                    exit_code = _wait_on_pid(result.pid)
                else:
                    exit_code = 0
            elif result.pid not in (0, None):
                # Wait on the raw PID if the adapter did not expose a proc.
                exit_code = _wait_on_pid(result.pid)
            else:
                exit_code = 0  # adapter did not expose a handle

        except SystemExit as exc:
            # Trap SystemExit — the adapter might call sys.exit().
            if isinstance(exc.code, int):
                exit_code = exc.code
            elif exc.code is not None:
                exit_code = 1
            else:
                exit_code = 0
            error_detail = f"SystemExit({exc.code})"
            logger.warning(
                "In-process agent %s triggered SystemExit(code=%s)",
                session_id,
                exc.code,
            )

        except Exception as exc:
            exit_code = 1
            error_detail = str(exc)
            logger.error("In-process agent %s failed: %s", session_id, exc)

        finally:
            with self._lock:
                if session_id in self._sessions:
                    current = self._sessions[session_id]
                    current.exit_code = exit_code
                    current.finished_at = time.time()
                    current.error_detail = error_detail


# ---------------------------------------------------------------------------
# Synthetic PID generator
# ---------------------------------------------------------------------------

_next_pid_counter = 10000
_next_pid_lock = threading.Lock()


def _next_pid() -> int:
    """Return a monotonically increasing synthetic PID.

    Starts at 10000 to avoid confusion with real PIDs on macOS.

    Returns:
        Unique integer usable as a pid surrogate.
    """
    global _next_pid_counter
    with _next_pid_lock:
        pid = _next_pid_counter
        _next_pid_counter += 1
        return pid


def _wait_on_pid(pid: int, timeout: float = 600.0) -> int:
    """Wait on a raw PID without blocking the calling thread indefinitely.

    Uses ``os.waitpid`` with periodic polling via WNOHANG.

    Args:
        pid: Process ID to wait for.
        timeout: Maximum seconds to wait (default 10 min).

    Returns:
        Exit code, or 0 if the process could not be found.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                if os.WIFEXITED(status):
                    return os.WEXITSTATUS(status)
                return 128 + (os.WTERMSIG(status) if os.WIFSIGNALED(status) else 0)
        except ChildProcessError:
            return 0  # process already reaped
        time.sleep(0.5)
    return 0  # timed out
