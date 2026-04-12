"""Server supervisor — auto-restart task server on crash.

Wraps the uvicorn server process with a restart loop. If the server
crashes, it's restarted automatically (up to MAX_RESTARTS within
RESTART_WINDOW_S). A health check thread monitors /health every 10s.

This is the "systemd Restart=always" pattern, built into Bernstein
so it works everywhere without external process managers.

Usage:
    Instead of calling _start_server() directly, use supervised_server():

        pid = supervised_server(workdir, port)
        # Server will auto-restart on crash, up to 5 times in 10 minutes
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from bernstein.core.platform_compat import kill_process
from bernstein.core.process_utils import is_process_alive
from bernstein.core.runtime_state import SupervisorStateSnapshot, rotate_log_file, write_supervisor_state

logger = logging.getLogger(__name__)

# Restart policy
MAX_RESTARTS = 5  # Max restarts within the window
RESTART_WINDOW_S = 600  # 10 minutes -- reset counter after this
RESTART_DELAY_S = 2  # Wait before restart (backoff: 2, 4, 8, 16, 30)
HEALTH_CHECK_INTERVAL_S = 10  # Check server health every 10s
HEALTH_CHECK_TIMEOUT_S = 3  # HTTP timeout for health check
MAX_CONSECUTIVE_FAILURES = 6  # 6 x 10s = 60s of failures -- restart


def supervised_server(
    workdir: Path,
    port: int,
    bind_host: str = "127.0.0.1",
    cluster_enabled: bool = False,
    auth_token: str | None = None,
    evolve_mode: bool = False,
) -> int:
    """Launch the task server with automatic restart on crash.

    Returns the PID of the supervisor thread's current server process.
    The supervisor runs as a daemon thread — it dies when the main
    process exits.
    """
    state = _SupervisorState(
        workdir=workdir,
        port=port,
        bind_host=bind_host,
        cluster_enabled=cluster_enabled,
        auth_token=auth_token,
        evolve_mode=evolve_mode,
    )

    # Start initial server
    pid = _launch_server(state)
    state.current_pid = pid
    write_supervisor_state(workdir, state.snapshot())

    # Start supervisor thread (monitors + restarts)
    supervisor = threading.Thread(
        target=_supervisor_loop,
        args=(state,),
        daemon=True,
        name="server-supervisor",
    )
    supervisor.start()

    # Start health check thread
    health = threading.Thread(
        target=_health_check_loop,
        args=(state,),
        daemon=True,
        name="server-health",
    )
    health.start()

    return pid


class _SupervisorState:
    """Shared state between supervisor and health check threads."""

    def __init__(
        self,
        workdir: Path,
        port: int,
        bind_host: str,
        cluster_enabled: bool,
        auth_token: str | None,
        evolve_mode: bool,
    ) -> None:
        self.workdir = workdir
        self.port = port
        self.bind_host = bind_host
        self.cluster_enabled = cluster_enabled
        self.auth_token = auth_token
        self.evolve_mode = evolve_mode

        self.current_pid: int = 0
        self.restart_count: int = 0
        self.restart_timestamps: list[float] = []
        self.stopped: bool = False
        self.lock = threading.Lock()
        self.consecutive_health_failures: int = 0
        self.started_at: float = time.time()

    def snapshot(self) -> SupervisorStateSnapshot:
        """Build the persisted supervisor state snapshot."""
        last_restart_at = self.restart_timestamps[-1] if self.restart_timestamps else None
        return SupervisorStateSnapshot(
            started_at=self.started_at,
            restart_count=self.restart_count,
            current_pid=self.current_pid,
            last_restart_at=last_restart_at,
        )


def _launch_server(state: _SupervisorState) -> int:
    """Launch a single server process instance."""
    workdir = state.workdir
    port = state.port
    bind_host = state.bind_host

    pid_path = workdir / ".sdd" / "runtime" / "server.pid"

    env = dict(os.environ)
    if state.cluster_enabled:
        env["BERNSTEIN_CLUSTER_ENABLED"] = "1"
    env["BERNSTEIN_BIND_HOST"] = bind_host
    if state.auth_token:
        env["BERNSTEIN_AUTH_TOKEN"] = state.auth_token
    for var in ("BERNSTEIN_STORAGE_BACKEND", "BERNSTEIN_DATABASE_URL", "BERNSTEIN_REDIS_URL"):
        if var in os.environ:
            env[var] = os.environ[var]

    server_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "bernstein.core.server:app",
        "--host",
        bind_host,
        "--port",
        str(port),
    ]
    # ``--reload`` is gated on ``evolve_mode``. In a self-modifying system
    # (bernstein agents constantly edit src/bernstein/*.py) ``--reload``
    # is catastrophic: every file write triggers a uvicorn restart, which
    # drops in-flight HTTP connections, races on port 8052 ("Address already
    # in use"), and replays the WAL with duplicate task claims. Incident
    # 2026-04-11 03:19 — server hung 127s, orchestrator gave up. Production
    # runs MUST not auto-reload; only the explicit dev/evolve flow may.
    if state.evolve_mode:
        src_dir = str(workdir / "src" / "bernstein")
        if Path(src_dir).is_dir():
            server_cmd.extend(["--reload", "--reload-dir", src_dir])
        else:
            server_cmd.append("--reload")

    log_path = workdir / ".sdd" / "runtime" / "server.log"
    rotate_log_file(log_path)
    log_fh = log_path.open("a")  # Append on restart, don't overwrite
    proc = subprocess.Popen(
        server_cmd,
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(workdir),
    )
    log_fh.close()
    pid_path.write_text(str(proc.pid))
    state.current_pid = proc.pid
    write_supervisor_state(workdir, state.snapshot())
    return proc.pid


def _is_alive(pid: int) -> bool:
    """Check if process is alive."""
    return is_process_alive(pid)


def _supervisor_loop(state: _SupervisorState) -> None:
    """Monitor server process and restart on crash."""
    while not state.stopped:
        time.sleep(1)

        with state.lock:
            pid = state.current_pid

        if pid <= 0 or state.stopped:
            continue

        if _is_alive(pid):
            continue

        # Server died — attempt restart
        logger.warning("Server process %d died. Attempting restart...", pid)

        # Check restart budget
        now = time.monotonic()
        with state.lock:
            # Prune old timestamps outside window
            state.restart_timestamps = [t for t in state.restart_timestamps if now - t < RESTART_WINDOW_S]

            if len(state.restart_timestamps) >= MAX_RESTARTS:
                logger.error(
                    "Server crashed %d times in %ds — giving up. Check .sdd/runtime/server.log for root cause.",
                    MAX_RESTARTS,
                    RESTART_WINDOW_S,
                )
                state.stopped = True
                return

            state.restart_count += 1
            state.restart_timestamps.append(now)
            attempt = state.restart_count

        # Exponential backoff: 2, 4, 8, 16, 30 (capped)
        delay = min(RESTART_DELAY_S * (2 ** (attempt - 1)), 30)
        logger.info("Restart attempt %d/%d in %ds...", attempt, MAX_RESTARTS, delay)
        time.sleep(delay)

        if state.stopped:
            return

        try:
            new_pid = _launch_server(state)
            with state.lock:
                state.current_pid = new_pid
                state.consecutive_health_failures = 0
                write_supervisor_state(state.workdir, state.snapshot())
            logger.info("Server restarted successfully (PID %d)", new_pid)
        except Exception:
            logger.exception("Failed to restart server")


def _health_check_loop(state: _SupervisorState) -> None:
    """Periodically check server health via HTTP."""
    import httpx

    url = f"http://127.0.0.1:{state.port}/health"

    # Wait for initial startup
    time.sleep(5)

    while not state.stopped:
        time.sleep(HEALTH_CHECK_INTERVAL_S)

        if state.stopped:
            return

        try:
            resp = httpx.get(url, timeout=HEALTH_CHECK_TIMEOUT_S)
            if resp.status_code == 200:
                with state.lock:
                    state.consecutive_health_failures = 0
                continue
        except Exception:
            pass

        # Health check failed
        with state.lock:
            state.consecutive_health_failures += 1
            failures = state.consecutive_health_failures

        if failures >= MAX_CONSECUTIVE_FAILURES:
            pid = state.current_pid
            if _is_alive(pid):
                # Server alive but unresponsive — kill it so supervisor restarts
                logger.warning(
                    "Server unresponsive for %ds (PID %d) — killing for restart",
                    failures * HEALTH_CHECK_INTERVAL_S,
                    pid,
                )
                kill_process(pid, signal.SIGTERM)
                time.sleep(3)
                if _is_alive(pid):
                    kill_process(pid, 9)
                with state.lock:
                    state.consecutive_health_failures = 0
