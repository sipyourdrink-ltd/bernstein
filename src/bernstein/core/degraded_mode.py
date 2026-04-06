"""Degraded mode management when the task server is unreachable.

When the task server becomes unreachable, the orchestrator enters
degraded mode:

1. **Spawning pauses** — no new agents are launched.
2. **State preserved** — pending decisions are written to the WAL so
   they survive a crash and can be replayed.
3. **Retry with backoff** — server connectivity is probed with
   exponential backoff instead of crashing.
4. **Existing agents continue** — agents already running are not killed;
   heartbeat and reaping still operate.

Once the server responds successfully to a health probe, degraded mode
is exited and normal operation resumes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.http_retry import compute_backoff

if TYPE_CHECKING:
    import httpx

    from bernstein.core.wal import WALWriter

logger = logging.getLogger(__name__)


@dataclass
class DegradedModeConfig:
    """Configuration for degraded mode behavior.

    Attributes:
        enter_after_failures: Enter degraded mode after this many
            consecutive server failures.
        exit_after_successes: Exit degraded mode after this many
            consecutive successful probes.
        probe_base_delay_s: Base delay between server health probes.
        probe_max_delay_s: Maximum delay between probes.
        max_degraded_ticks: Maximum ticks in degraded mode before
            stopping the orchestrator entirely (0 = unlimited).
    """

    enter_after_failures: int = 3
    exit_after_successes: int = 2
    probe_base_delay_s: float = 5.0
    probe_max_delay_s: float = 60.0
    max_degraded_ticks: int = 0


@dataclass
class DegradedModeState:
    """Runtime state for degraded mode tracking.

    Attributes:
        active: Whether the orchestrator is currently in degraded mode.
        consecutive_failures: Number of consecutive server failures.
        consecutive_successes: Number of consecutive successful probes
            since last failure.
        entered_at: Monotonic timestamp when degraded mode was entered.
        ticks_in_degraded: Number of ticks spent in degraded mode.
        last_probe_time: Monotonic timestamp of last server probe.
        probe_attempt: Current probe attempt number (for backoff).
        last_wal_flush_time: Monotonic timestamp of last WAL flush in
            degraded mode.
    """

    active: bool = False
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    entered_at: float = 0.0
    ticks_in_degraded: int = 0
    last_probe_time: float = 0.0
    probe_attempt: int = 0
    last_wal_flush_time: float = 0.0


class DegradedModeManager:
    """Manage orchestrator degraded mode transitions.

    Thread-safe for read access to ``state``; mutations happen only from
    the orchestrator tick thread.

    Args:
        config: Degraded mode configuration.
        wal_writer: WAL writer for persisting state during degraded mode.
    """

    def __init__(
        self,
        config: DegradedModeConfig | None = None,
        wal_writer: WALWriter | None = None,
    ) -> None:
        self._config = config or DegradedModeConfig()
        self._wal_writer = wal_writer
        self._state = DegradedModeState()

    @property
    def is_degraded(self) -> bool:
        """Whether the orchestrator is currently in degraded mode."""
        return self._state.active

    @property
    def state(self) -> DegradedModeState:
        """Current degraded mode state (read-only snapshot)."""
        return self._state

    def record_server_failure(self) -> bool:
        """Record a server communication failure.

        Call this when the task server is unreachable or returns an error.

        Returns:
            True if degraded mode was just entered (transition happened).
        """
        self._state.consecutive_failures += 1
        self._state.consecutive_successes = 0

        if self._state.active:
            self._state.ticks_in_degraded += 1
            return False

        if self._state.consecutive_failures >= self._config.enter_after_failures:
            self._enter_degraded_mode()
            return True

        return False

    def record_server_success(self) -> bool:
        """Record a successful server communication.

        Call this when a server request succeeds.

        Returns:
            True if degraded mode was just exited (transition happened).
        """
        self._state.consecutive_successes += 1
        self._state.consecutive_failures = 0

        if not self._state.active:
            return False

        if self._state.consecutive_successes >= self._config.exit_after_successes:
            self._exit_degraded_mode()
            return True

        return False

    def should_probe_server(self) -> bool:
        """Check if it is time to send a health probe to the server.

        Returns True when enough time has elapsed since the last probe
        based on exponential backoff with jitter.

        Returns:
            True if a probe should be sent now.
        """
        if not self._state.active:
            return True  # Not in degraded mode; always try

        now = time.monotonic()
        delay = compute_backoff(
            self._state.probe_attempt,
            self._config.probe_base_delay_s,
            self._config.probe_max_delay_s,
            jitter=False,  # deterministic for probes
        )
        return (now - self._state.last_probe_time) >= delay

    def record_probe_attempt(self) -> None:
        """Record that a server probe was just sent."""
        self._state.last_probe_time = time.monotonic()
        self._state.probe_attempt += 1

    def should_stop_orchestrator(self) -> bool:
        """Check if the orchestrator should stop entirely.

        Returns True if ``max_degraded_ticks`` is set and has been exceeded.

        Returns:
            True if the orchestrator should shut down.
        """
        if self._config.max_degraded_ticks <= 0:
            return False
        return self._state.ticks_in_degraded >= self._config.max_degraded_ticks

    def should_allow_spawn(self) -> bool:
        """Check if agent spawning should be allowed.

        Spawning is blocked in degraded mode to prevent spawning agents
        that cannot report back to the task server.

        Returns:
            True if spawning should proceed.
        """
        return not self._state.active

    def preserve_state_to_wal(
        self,
        pending_tasks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Write pending orchestrator state to the WAL for crash safety.

        Called periodically during degraded mode to ensure no decisions
        are lost if the orchestrator crashes while the server is down.

        Args:
            pending_tasks: List of task dicts that were pending when
                degraded mode was entered.
        """
        if self._wal_writer is None:
            return

        now = time.monotonic()
        # Rate-limit WAL flushes to once per 10 seconds
        if now - self._state.last_wal_flush_time < 10.0:
            return

        try:
            self._wal_writer.write_entry(
                decision_type="degraded_mode_state",
                inputs={
                    "consecutive_failures": self._state.consecutive_failures,
                    "ticks_in_degraded": self._state.ticks_in_degraded,
                    "pending_task_count": len(pending_tasks) if pending_tasks else 0,
                },
                output={"active": True},
                actor="degraded_mode_manager",
                committed=False,
            )
            self._state.last_wal_flush_time = now
        except OSError:
            logger.debug("WAL write failed during degraded mode")

    def _enter_degraded_mode(self) -> None:
        """Transition into degraded mode."""
        self._state.active = True
        self._state.entered_at = time.monotonic()
        self._state.ticks_in_degraded = 0
        self._state.probe_attempt = 0
        logger.warning(
            "Entering DEGRADED MODE: task server unreachable (%d consecutive failures). Spawning paused.",
            self._state.consecutive_failures,
        )

        # Record transition in WAL
        if self._wal_writer is not None:
            try:
                self._wal_writer.write_entry(
                    decision_type="degraded_mode_enter",
                    inputs={"consecutive_failures": self._state.consecutive_failures},
                    output={"active": True},
                    actor="degraded_mode_manager",
                )
            except OSError:
                logger.debug("WAL write failed for degraded_mode_enter")

    def _exit_degraded_mode(self) -> None:
        """Transition out of degraded mode."""
        duration = time.monotonic() - self._state.entered_at
        logger.info(
            "Exiting DEGRADED MODE: server reachable again (after %.1fs, %d ticks in degraded)",
            duration,
            self._state.ticks_in_degraded,
        )
        self._state.active = False
        self._state.consecutive_failures = 0
        self._state.ticks_in_degraded = 0
        self._state.probe_attempt = 0

        # Record transition in WAL
        if self._wal_writer is not None:
            try:
                self._wal_writer.write_entry(
                    decision_type="degraded_mode_exit",
                    inputs={"duration_s": duration},
                    output={"active": False},
                    actor="degraded_mode_manager",
                )
            except OSError:
                logger.debug("WAL write failed for degraded_mode_exit")


def probe_server_health(client: httpx.Client, base_url: str) -> bool:
    """Send a lightweight health probe to the task server.

    Args:
        client: httpx client.
        base_url: Server base URL.

    Returns:
        True if the server responded with a 2xx status.
    """
    try:
        resp = client.get(f"{base_url}/status", timeout=5.0)
        return resp.status_code < 400
    except Exception:
        return False
