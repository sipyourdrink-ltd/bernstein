"""Deterministic shutdown ordering for the orchestrator.

Ensures a safe, reproducible shutdown sequence:
1. Signal all agents to stop (SHUTDOWN files)
2. Drain running agents (wait up to timeout)
3. Flush WAL (write-ahead log)
4. Save session state
5. Close HTTP connections
6. Stop task server
7. Final cleanup (thread pool, locks)

Each phase is tracked with a callback for observability.

Usage::

    seq = ShutdownSequence(timeout_s=30.0)
    seq.add_phase("drain_agents", drain_fn)
    seq.add_phase("flush_wal", wal_fn)
    seq.add_phase("close_connections", close_fn)
    result = seq.execute()
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShutdownPhaseResult:
    """Result of a single shutdown phase.

    Attributes:
        name: Phase name.
        success: Whether the phase completed without error.
        duration_ms: Wall-clock duration in milliseconds.
        error: Error message if the phase failed.
        skipped: Whether the phase was skipped (timeout exceeded).
    """

    name: str
    success: bool
    duration_ms: float
    error: str = ""
    skipped: bool = False


@dataclass(frozen=True)
class ShutdownResult:
    """Aggregate result of the full shutdown sequence.

    Attributes:
        phases: Results for each phase in execution order.
        total_duration_ms: Total shutdown wall-clock time.
        all_succeeded: True if every phase completed without error.
        timed_out: True if the global timeout was reached.
    """

    phases: list[ShutdownPhaseResult]
    total_duration_ms: float
    all_succeeded: bool
    timed_out: bool

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-compatible dict.

        Returns:
            Dictionary with phases, timing, and status.
        """
        return {
            "total_duration_ms": round(self.total_duration_ms, 2),
            "all_succeeded": self.all_succeeded,
            "timed_out": self.timed_out,
            "phases": [
                {
                    "name": p.name,
                    "success": p.success,
                    "duration_ms": round(p.duration_ms, 2),
                    "error": p.error,
                    "skipped": p.skipped,
                }
                for p in self.phases
            ],
        }


@dataclass
class ShutdownSequence:
    """Deterministic shutdown sequence executor.

    Phases are executed in registration order. A global timeout ensures
    the shutdown completes within a bounded time even if a phase hangs.

    Args:
        timeout_s: Maximum total time for the entire shutdown sequence.
    """

    timeout_s: float = 30.0
    _phases: list[tuple[str, Callable[[], None]]] = field(default_factory=list[tuple[str, Callable[[], None]]])

    def add_phase(self, name: str, fn: Callable[[], None]) -> None:
        """Register a shutdown phase.

        Phases execute in the order they are added.

        Args:
            name: Human-readable phase name.
            fn: Callable that performs the shutdown work. Should be
                idempotent (safe to call multiple times).
        """
        self._phases.append((name, fn))

    def execute(self) -> ShutdownResult:
        """Execute all shutdown phases in order.

        Each phase is timed individually. If the global timeout is
        reached, remaining phases are skipped.

        Returns:
            Aggregate shutdown result.
        """
        results: list[ShutdownPhaseResult] = []
        start = time.monotonic()
        timed_out = False

        for name, fn in self._phases:
            elapsed = (time.monotonic() - start) * 1000  # ms
            remaining_ms = self.timeout_s * 1000 - elapsed

            if remaining_ms <= 0:
                logger.warning("Shutdown timeout reached, skipping phase '%s'", name)
                results.append(
                    ShutdownPhaseResult(
                        name=name,
                        success=False,
                        duration_ms=0.0,
                        skipped=True,
                    )
                )
                timed_out = True
                continue

            phase_start = time.monotonic()
            try:
                logger.info("Shutdown phase '%s' starting", name)
                fn()
                duration_ms = (time.monotonic() - phase_start) * 1000
                results.append(
                    ShutdownPhaseResult(
                        name=name,
                        success=True,
                        duration_ms=duration_ms,
                    )
                )
                logger.info("Shutdown phase '%s' completed in %.1fms", name, duration_ms)
            except Exception as exc:
                duration_ms = (time.monotonic() - phase_start) * 1000
                results.append(
                    ShutdownPhaseResult(
                        name=name,
                        success=False,
                        duration_ms=duration_ms,
                        error=str(exc),
                    )
                )
                logger.warning(
                    "Shutdown phase '%s' failed after %.1fms: %s",
                    name,
                    duration_ms,
                    exc,
                )

        total_ms = (time.monotonic() - start) * 1000
        all_ok = all(p.success for p in results)

        return ShutdownResult(
            phases=results,
            total_duration_ms=total_ms,
            all_succeeded=all_ok,
            timed_out=timed_out,
        )

    @property
    def phase_names(self) -> list[str]:
        """Return the ordered list of registered phase names.

        Returns:
            Phase names in registration order.
        """
        return [name for name, _ in self._phases]


def build_default_shutdown_sequence(
    *,
    signal_agents_fn: Callable[[], None] | None = None,
    drain_agents_fn: Callable[[], None] | None = None,
    flush_wal_fn: Callable[[], None] | None = None,
    save_state_fn: Callable[[], None] | None = None,
    close_connections_fn: Callable[[], None] | None = None,
    stop_server_fn: Callable[[], None] | None = None,
    final_cleanup_fn: Callable[[], None] | None = None,
    timeout_s: float = 30.0,
) -> ShutdownSequence:
    """Build the default 7-phase shutdown sequence.

    Any phase callback set to None is skipped.

    Args:
        signal_agents_fn: Signal all agents to stop.
        drain_agents_fn: Wait for agents to finish.
        flush_wal_fn: Flush the write-ahead log.
        save_state_fn: Save session state to disk.
        close_connections_fn: Close HTTP client connections.
        stop_server_fn: Stop the task server process.
        final_cleanup_fn: Thread pool shutdown and lock release.
        timeout_s: Global timeout for the entire sequence.

    Returns:
        Configured shutdown sequence ready for execution.
    """
    seq = ShutdownSequence(timeout_s=timeout_s)

    if signal_agents_fn is not None:
        seq.add_phase("signal_agents", signal_agents_fn)
    if drain_agents_fn is not None:
        seq.add_phase("drain_agents", drain_agents_fn)
    if flush_wal_fn is not None:
        seq.add_phase("flush_wal", flush_wal_fn)
    if save_state_fn is not None:
        seq.add_phase("save_state", save_state_fn)
    if close_connections_fn is not None:
        seq.add_phase("close_connections", close_connections_fn)
    if stop_server_fn is not None:
        seq.add_phase("stop_server", stop_server_fn)
    if final_cleanup_fn is not None:
        seq.add_phase("final_cleanup", final_cleanup_fn)

    return seq
