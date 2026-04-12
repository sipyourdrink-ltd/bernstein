"""Task server connection pool with health-aware routing (road-053).

Manages a pool of connection slots to the task server, tracking per-slot
latency and error counts.  Unhealthy slots are automatically retired so
that callers always acquire a slot backed by a healthy connection.

Usage::

    pool = ConnectionPool("http://127.0.0.1:8052")
    slot = pool.acquire()
    if slot is not None:
        try:
            # ... use the connection ...
            pool.release(slot.slot_id, latency_ms=12.3, success=True)
        except Exception:
            pool.release(slot.slot_id, latency_ms=0.0, success=False)
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectionHealth:
    """Snapshot of health metrics for a single endpoint.

    Attributes:
        endpoint: The server URL being monitored.
        avg_latency_ms: Rolling average latency in milliseconds.
        error_count: Total number of errors recorded.
        last_success_at: Monotonic timestamp of the last successful request.
        last_error_at: Monotonic timestamp of the last failed request.
        is_healthy: Whether the endpoint is considered healthy.
    """

    endpoint: str
    avg_latency_ms: float
    error_count: int
    last_success_at: float | None
    last_error_at: float | None
    is_healthy: bool


@dataclass(frozen=True)
class PoolConfig:
    """Configuration knobs for the connection pool.

    Attributes:
        max_connections: Maximum number of slots in the pool.
        health_check_interval_s: Seconds between health-check sweeps.
        unhealthy_threshold: Number of consecutive errors before a slot
            is considered unhealthy.
        retire_after_errors: Total error count after which a slot is
            permanently retired (removed from the pool).
    """

    max_connections: int = 10
    health_check_interval_s: float = 30.0
    unhealthy_threshold: int = 3
    retire_after_errors: int = 10


@dataclass(frozen=True)
class ConnectionSlot:
    """An individual slot inside the pool.

    Attributes:
        slot_id: Unique identifier for this slot.
        endpoint: The server URL this slot connects to.
        created_at: Monotonic timestamp when the slot was created.
        request_count: Total requests routed through this slot.
        error_count: Total errors recorded on this slot.
        avg_latency_ms: Rolling average latency in milliseconds.
    """

    slot_id: str
    endpoint: str
    created_at: float
    request_count: int = 0
    error_count: int = 0
    avg_latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# Mutable internal tracker (not part of the public API)
# ---------------------------------------------------------------------------


@dataclass
class _SlotState:
    """Mutable bookkeeping for a slot inside the pool."""

    slot: ConnectionSlot
    in_use: bool = False
    last_success_at: float | None = None
    last_error_at: float | None = None
    total_latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------


class ConnectionPool:
    """Health-aware connection pool for the task server.

    Slots are created lazily on :meth:`acquire` up to ``config.max_connections``.
    Each :meth:`release` updates latency / error statistics.  Slots that exceed
    the ``retire_after_errors`` threshold are permanently removed by
    :meth:`retire_unhealthy`.

    Args:
        endpoint: Base URL of the task server.
        config: Pool configuration (uses defaults when *None*).
    """

    def __init__(
        self,
        endpoint: str,
        config: PoolConfig | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._config = config or PoolConfig()
        self._slots: dict[str, _SlotState] = {}

    # -- public API ----------------------------------------------------------

    def acquire(self) -> ConnectionSlot | None:
        """Reserve and return the healthiest available slot.

        Returns the idle slot with the lowest average latency.  If no idle
        slot exists and the pool has not reached ``max_connections``, a new
        slot is created.  Returns *None* when the pool is exhausted.
        """
        # 1. Find idle slots, pick the one with lowest latency
        idle: list[_SlotState] = [s for s in self._slots.values() if not s.in_use]
        if idle:
            best = min(idle, key=lambda s: s.slot.avg_latency_ms)
            best.in_use = True
            logger.debug("Acquired existing slot %s", best.slot.slot_id)
            return best.slot

        # 2. Create a new slot if room remains
        if len(self._slots) < self._config.max_connections:
            slot = ConnectionSlot(
                slot_id=uuid.uuid4().hex[:12],
                endpoint=self._endpoint,
                created_at=time.monotonic(),
            )
            state = _SlotState(slot=slot, in_use=True)
            self._slots[slot.slot_id] = state
            logger.debug("Created and acquired new slot %s", slot.slot_id)
            return slot

        # 3. Pool exhausted
        logger.warning(
            "Connection pool exhausted (%d/%d in use)",
            self.active_count(),
            self._config.max_connections,
        )
        return None

    def release(
        self,
        slot_id: str,
        latency_ms: float,
        success: bool,
    ) -> None:
        """Return a slot to the pool, recording latency and outcome.

        Args:
            slot_id: The slot to release.
            latency_ms: How long the request took.
            success: Whether the request succeeded.
        """
        state = self._slots.get(slot_id)
        if state is None:
            logger.warning("release() called for unknown slot %s", slot_id)
            return

        now = time.monotonic()
        new_request_count = state.slot.request_count + 1
        new_error_count = state.slot.error_count + (0 if success else 1)
        new_total_latency = state.total_latency_ms + latency_ms
        new_avg = new_total_latency / new_request_count

        # Replace the frozen slot with updated counters
        state.slot = ConnectionSlot(
            slot_id=state.slot.slot_id,
            endpoint=state.slot.endpoint,
            created_at=state.slot.created_at,
            request_count=new_request_count,
            error_count=new_error_count,
            avg_latency_ms=new_avg,
        )
        state.total_latency_ms = new_total_latency
        state.in_use = False

        if success:
            state.last_success_at = now
        else:
            state.last_error_at = now
            logger.debug(
                "Slot %s error_count=%d",
                slot_id,
                new_error_count,
            )

    def health_summary(self) -> ConnectionHealth:
        """Aggregate health snapshot across all slots.

        Returns a :class:`ConnectionHealth` summarising the pool's overall
        latency, error count, and health status.
        """
        if not self._slots:
            return ConnectionHealth(
                endpoint=self._endpoint,
                avg_latency_ms=0.0,
                error_count=0,
                last_success_at=None,
                last_error_at=None,
                is_healthy=True,
            )

        total_latency = 0.0
        total_requests = 0
        total_errors = 0
        last_success: float | None = None
        last_error: float | None = None

        for state in self._slots.values():
            total_latency += state.total_latency_ms
            total_requests += state.slot.request_count
            total_errors += state.slot.error_count
            if state.last_success_at is not None and (last_success is None or state.last_success_at > last_success):
                last_success = state.last_success_at
            if state.last_error_at is not None and (last_error is None or state.last_error_at > last_error):
                last_error = state.last_error_at

        avg_latency = total_latency / total_requests if total_requests else 0.0
        healthy_slots = sum(1 for s in self._slots.values() if s.slot.error_count < self._config.unhealthy_threshold)
        is_healthy = healthy_slots > 0

        return ConnectionHealth(
            endpoint=self._endpoint,
            avg_latency_ms=avg_latency,
            error_count=total_errors,
            last_success_at=last_success,
            last_error_at=last_error,
            is_healthy=is_healthy,
        )

    def active_count(self) -> int:
        """Return the number of slots currently in use."""
        return sum(1 for s in self._slots.values() if s.in_use)

    def retire_unhealthy(self) -> int:
        """Remove slots whose error count meets or exceeds ``retire_after_errors``.

        Returns the number of slots retired.
        """
        to_remove: list[str] = [
            sid for sid, state in self._slots.items() if state.slot.error_count >= self._config.retire_after_errors
        ]
        for sid in to_remove:
            logger.info("Retiring unhealthy slot %s", sid)
            del self._slots[sid]
        return len(to_remove)

    def stats(self) -> dict[str, ConnectionSlot]:
        """Return a mapping of slot_id to its current :class:`ConnectionSlot`."""
        return {sid: state.slot for sid, state in self._slots.items()}
