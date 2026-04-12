"""MCP server health monitoring with auto-restart (MCP-001).

Adds periodic health probes (default every 30s) to :class:`MCPManager`.
Auto-restarts crashed servers with exponential backoff (1s, 2s, 4s, max 30s).
Gives up after 5 consecutive restart failures.

Usage::

    from bernstein.core.mcp_health_monitor import McpHealthMonitor

    monitor = McpHealthMonitor(manager)
    monitor.start()    # begins background health-check loop
    # ...
    monitor.stop()     # stops the loop

The monitor is designed to run in a background thread so it does not
interfere with the main async event loop.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.mcp_manager import MCPManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PROBE_INTERVAL: float = 30.0
DEFAULT_MAX_RESTARTS: int = 5
DEFAULT_INITIAL_BACKOFF: float = 1.0
DEFAULT_MAX_BACKOFF: float = 30.0
BACKOFF_MULTIPLIER: float = 2.0


# ---------------------------------------------------------------------------
# Per-server restart state
# ---------------------------------------------------------------------------


@dataclass
class _RestartState:
    """Tracks restart attempts for a single server.

    Attributes:
        consecutive_failures: Number of consecutive failed restarts.
        next_backoff: Delay before the next restart attempt.
        last_attempt_ts: Monotonic timestamp of the last restart attempt.
        given_up: True if max restarts exceeded.
    """

    consecutive_failures: int = 0
    next_backoff: float = DEFAULT_INITIAL_BACKOFF
    last_attempt_ts: float = 0.0
    given_up: bool = False


@dataclass(frozen=True)
class HealthProbeResult:
    """Result of a single health probe cycle.

    Attributes:
        ts: Monotonic timestamp when the probe ran.
        server_name: Name of the server probed.
        alive: Whether the server was alive.
        restarted: Whether a restart was attempted.
        restart_success: Whether the restart succeeded (None if not attempted).
        given_up: Whether the monitor has given up on this server.
    """

    ts: float
    server_name: str
    alive: bool
    restarted: bool = False
    restart_success: bool | None = None
    given_up: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "ts": self.ts,
            "server_name": self.server_name,
            "alive": self.alive,
            "restarted": self.restarted,
            "restart_success": self.restart_success,
            "given_up": self.given_up,
        }


# ---------------------------------------------------------------------------
# McpHealthMonitor
# ---------------------------------------------------------------------------


class McpHealthMonitor:
    """Periodic health monitor for MCP servers with auto-restart.

    Probes all servers managed by an :class:`MCPManager` at a configurable
    interval.  When a server is found dead, attempts to restart it with
    exponential backoff.  Gives up after *max_restarts* consecutive failures.

    Args:
        manager: The MCPManager whose servers to monitor.
        probe_interval: Seconds between health probe cycles.
        max_restarts: Maximum consecutive restart failures before giving up.
        initial_backoff: Initial backoff delay in seconds.
        max_backoff: Maximum backoff delay in seconds.
    """

    def __init__(
        self,
        manager: MCPManager,
        *,
        probe_interval: float = DEFAULT_PROBE_INTERVAL,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
    ) -> None:
        self._manager = manager
        self._probe_interval = probe_interval
        self._max_restarts = max_restarts
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff

        self._restart_states: dict[str, _RestartState] = {}
        self._history: list[HealthProbeResult] = []
        self._max_history = 500

        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Callback hook for testing / observation
        self._on_probe: Any = None

    @property
    def running(self) -> bool:
        """Whether the monitor loop is currently running."""
        return self._running

    @property
    def history(self) -> list[HealthProbeResult]:
        """Read-only copy of recent probe results."""
        return list(self._history)

    def get_restart_state(self, server_name: str) -> _RestartState | None:
        """Return the restart state for a server, or None if never tracked."""
        return self._restart_states.get(server_name)

    def start(self) -> None:
        """Start the background health-check loop.

        No-op if already running.
        """
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="mcp-health-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("MCP health monitor started (interval=%.1fs)", self._probe_interval)

    def stop(self) -> None:
        """Stop the background health-check loop.

        Blocks until the thread exits (up to one probe interval).
        """
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._probe_interval + 2.0)
            self._thread = None
        logger.info("MCP health monitor stopped")

    def probe_once(self) -> list[HealthProbeResult]:
        """Run one probe cycle across all servers (synchronous).

        Useful for testing or manual probing without the background loop.

        Returns:
            List of probe results, one per server.
        """
        results: list[HealthProbeResult] = []
        now = time.monotonic()

        for name in self._manager.server_names:
            result = self._probe_server(name, now)
            results.append(result)
            self._record(result)

        return results

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Background loop: probe all servers, sleep, repeat."""
        while self._running and not self._stop_event.is_set():
            try:
                self.probe_once()
            except Exception:
                logger.exception("Unexpected error in health monitor loop")
            self._stop_event.wait(timeout=self._probe_interval)

    # ------------------------------------------------------------------
    # Per-server probing
    # ------------------------------------------------------------------

    def _probe_server(self, name: str, now: float) -> HealthProbeResult:
        """Probe a single server and attempt restart if dead.

        Args:
            name: Server name.
            now: Current monotonic timestamp.

        Returns:
            Probe result.
        """
        alive = self._manager.is_alive(name)

        if alive:
            # Reset restart state on success
            state = self._restart_states.get(name)
            if state is not None and state.consecutive_failures > 0:
                logger.info(
                    "MCP server '%s' recovered after %d restart attempt(s)",
                    name,
                    state.consecutive_failures,
                )
                state.consecutive_failures = 0
                state.next_backoff = self._initial_backoff
                state.given_up = False
            return HealthProbeResult(ts=now, server_name=name, alive=True)

        # Server is dead — check restart state
        state = self._restart_states.setdefault(name, _RestartState())

        if state.given_up:
            return HealthProbeResult(
                ts=now,
                server_name=name,
                alive=False,
                given_up=True,
            )

        if state.consecutive_failures >= self._max_restarts:
            logger.error(
                "MCP server '%s' reached max restarts (%d), giving up",
                name,
                self._max_restarts,
            )
            state.given_up = True
            return HealthProbeResult(
                ts=now,
                server_name=name,
                alive=False,
                given_up=True,
            )

        # Check backoff
        elapsed = now - state.last_attempt_ts
        if elapsed < state.next_backoff:
            return HealthProbeResult(
                ts=now,
                server_name=name,
                alive=False,
                restarted=False,
            )

        # Attempt restart
        state.last_attempt_ts = now
        success = self._attempt_restart(name)

        if success:
            logger.info(
                "MCP server '%s' restarted successfully (attempt %d)",
                name,
                state.consecutive_failures + 1,
            )
            state.consecutive_failures = 0
            state.next_backoff = self._initial_backoff
            return HealthProbeResult(
                ts=now,
                server_name=name,
                alive=True,
                restarted=True,
                restart_success=True,
            )
        else:
            state.consecutive_failures += 1
            state.next_backoff = min(
                state.next_backoff * BACKOFF_MULTIPLIER,
                self._max_backoff,
            )
            logger.warning(
                "MCP server '%s' restart failed (attempt %d/%d, next backoff=%.1fs)",
                name,
                state.consecutive_failures,
                self._max_restarts,
                state.next_backoff,
            )
            return HealthProbeResult(
                ts=now,
                server_name=name,
                alive=False,
                restarted=True,
                restart_success=False,
            )

    def _attempt_restart(self, name: str) -> bool:
        """Attempt to restart a dead server.

        Stops the server (if needed) and starts it again.

        Args:
            name: Server name.

        Returns:
            True if the server is alive after restart.
        """
        config = self._manager.get_server_info(name)
        if config is None:
            return False

        try:
            # Stop any lingering state
            self._manager._stop_server(name)  # pyright: ignore[reportPrivateUsage]
            # Remove from internal tracking so start_all can re-init
            self._manager._servers.pop(name, None)  # pyright: ignore[reportPrivateUsage]
            # Re-start
            self._manager._start_server(config)  # pyright: ignore[reportPrivateUsage]
            return self._manager.is_alive(name)
        except Exception as exc:
            logger.warning("Restart attempt for '%s' raised: %s", name, exc)
            return False

    # ------------------------------------------------------------------
    # History tracking
    # ------------------------------------------------------------------

    def _record(self, result: HealthProbeResult) -> None:
        """Append a probe result to history, trimming if needed."""
        self._history.append(result)
        if len(self._history) > self._max_history:
            del self._history[: len(self._history) - self._max_history]
        if self._on_probe is not None:
            self._on_probe(result)

    def get_status(self) -> dict[str, Any]:
        """Return a summary of monitor status for all servers.

        Returns:
            Dict keyed by server name with health and restart info.
        """
        status: dict[str, Any] = {}
        for name in self._manager.server_names:
            alive = self._manager.is_alive(name)
            state = self._restart_states.get(name)
            entry: dict[str, Any] = {"alive": alive}
            if state is not None:
                entry["consecutive_failures"] = state.consecutive_failures
                entry["given_up"] = state.given_up
                entry["next_backoff"] = state.next_backoff
            status[name] = entry
        return status
