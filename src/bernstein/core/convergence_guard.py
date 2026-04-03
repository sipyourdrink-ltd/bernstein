"""Convergence guard: block spawn waves when the system is overloaded.

Checks merge queue depth, active agent count, recent error rate, and
spawn frequency before allowing a new batch of agents to be spawned.

Usage inside ``claim_and_spawn_batches``::

    guard = ConvergenceGuard()
    status = guard.is_converged(...)
    if not status.ready:
        logger.debug("Spawn blocked by convergence guard: %s", status.reasons)
        return  # skip this batch
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from bernstein.core.models import ConvergenceGuardConfig

__all__ = ["ConvergenceGuard", "ConvergenceStatus"]

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConvergenceStatus:
    """Result of a convergence check.

    Attributes:
        ready: True when all gates pass — safe to spawn.
        reasons: Human-readable reasons why convergence is not met (empty when ready).
        pending_merges: Current pending merge count.
        active_agents: Current alive agent count.
        error_rate: Recent error rate (0.0-1.0, or -1.0 when unavailable).
        spawn_rate: Recent spawns per minute.
    """

    ready: bool
    reasons: list[str] = field(default_factory=list[str])
    pending_merges: int = 0
    active_agents: int = 0
    error_rate: float = -1.0
    spawn_rate: float = 0.0

    def __post_init__(self) -> None:
        if self.ready and self.reasons:
            raise ValueError("ready=True but reasons is non-empty")


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


class ConvergenceGuard:
    """Gate that checks system convergence before allowing a spawn wave.

    Monitors four signals:
    - **pending_merges**: depth of the merge queue
    - **active_agents**: number of alive, non-idle agents
    - **error_rate**: recent failures / total attempts within a sliding window
    - **spawn_rate**: number of spawns per minute within a sliding window

    When any signal exceeds its configured threshold the guard returns a
    ``ConvergenceStatus(ready=False, …)`` with human-readable reasons so
    callers can log why spawns were blocked.

    Args:
        config: Threshold configuration. Defaults to ``ConvergenceGuardConfig()``.

    >>> guard = ConvergenceGuard()
    >>> status = guard.is_converged(
    ...     pending_merges=2,
    ...     active_agents=3,
    ...     error_rate=0.1,
    ... )
    >>> status.ready
    True
    """

    def __init__(self, config: ConvergenceGuardConfig | None = None) -> None:
        self._cfg = config or ConvergenceGuardConfig()
        self._spawn_timestamps: deque[float] = deque()
        self._success_timestamps: deque[float] = deque()
        self._failure_timestamps: deque[float] = deque()

    # -- Public API --------------------------------------------------------

    @property
    def config(self) -> ConvergenceGuardConfig:
        """Return the current threshold configuration."""
        return self._cfg

    def is_converged(
        self,
        pending_merges: int | None = None,
        active_agents: int | None = None,
        error_rate: float | None = None,
        spawn_rate: float | None = None,
    ) -> ConvergenceStatus:
        """Check whether the system is converged enough for a new spawn wave.

        All four parameters are optional.  When ``None`` the metric is
        skipped (treated as passing).  Callers that want to check *all*
        gates should pass concrete values.

        Args:
            pending_merges: Current depth of the merge queue.
            active_agents: Number of alive agent processes.
            error_rate: Recent failure rate (0.0-1.0).
            spawn_rate: Recent spawns per minute.

        Returns:
            ``ConvergenceStatus`` with ``ready`` and ``reasons``.
        """
        reasons: list[str] = []
        pending_merges_count = 0
        active_agents_count = 0
        computed_error_rate = 0.0
        computed_spawn_rate = 0.0

        # Gate 1: merge queue depth
        if pending_merges is not None:
            pending_merges_count = pending_merges
            if pending_merges > self._cfg.max_pending_merges:
                reasons.append(f"Merge queue overloaded ({pending_merges}/{self._cfg.max_pending_merges})")

        # Gate 2: active agent count
        if active_agents is not None:
            active_agents_count = active_agents
            if active_agents >= self._cfg.max_active_agents:
                reasons.append(f"Too many active agents ({active_agents}/{self._cfg.max_active_agents})")

        # Gate 3: error rate
        if error_rate is not None:
            computed_error_rate = error_rate
            if error_rate > self._cfg.max_error_rate:
                reasons.append(f"High error rate ({error_rate:.0%} > {self._cfg.max_error_rate:.0%})")

        # Gate 4: spawn rate
        if spawn_rate is not None:
            computed_spawn_rate = spawn_rate
            if spawn_rate > self._cfg.max_spawn_rate:
                reasons.append(f"Spawn rate too high ({spawn_rate:.1f}/min > {self._cfg.max_spawn_rate:.1f}/min)")

        ready = len(reasons) == 0
        return ConvergenceStatus(
            ready=ready,
            reasons=reasons,
            pending_merges=pending_merges_count,
            active_agents=active_agents_count,
            error_rate=computed_error_rate,
            spawn_rate=computed_spawn_rate,
        )

    # -- Sliding-window helpers --------------------------------------------

    def record_spawn(self, now: float | None = None) -> None:
        """Record a spawn event for rate tracking.

        Args:
            now: Epoch timestamp. Defaults to ``time.time()``.
        """
        ts = now if now is not None else time.time()
        self._spawn_timestamps.append(ts)
        self._prune(self._spawn_timestamps, self._cfg.spawn_rate_window_seconds, now=now)

    def record_success(self, now: float | None = None) -> None:
        """Record a successful task completion.

        Args:
            now: Epoch timestamp. Defaults to ``time.time()``.
        """
        ts = now if now is not None else time.time()
        self._success_timestamps.append(ts)
        self._prune(self._success_timestamps, self._cfg.error_rate_window_seconds, now=now)

    def record_failure(self, now: float | None = None) -> None:
        """Record a failed task completion.

        Args:
            now: Epoch timestamp. Defaults to ``time.time()``.
        """
        ts = now if now is not None else time.time()
        self._failure_timestamps.append(ts)
        self._prune(self._failure_timestamps, self._cfg.error_rate_window_seconds, now=now)

    def current_spawn_rate(self, now: float | None = None) -> float:
        """Return spawns per minute over the configured window.

        Args:
            now: Epoch timestamp. Defaults to ``time.time()``.

        Returns:
            Spawns per minute.  Returns 0.0 when no spawns in window.
        """
        self._prune(self._spawn_timestamps, self._cfg.spawn_rate_window_seconds, now=now)
        if not self._spawn_timestamps:
            return 0.0
        window_minutes = self._cfg.spawn_rate_window_seconds / 60.0
        return len(self._spawn_timestamps) / window_minutes

    def current_error_rate(self, now: float | None = None) -> float:
        """Return failure rate (failures / total) over the configured window.

        Returns -1.0 when there are no samples (cannot compute rate).

        Args:
            now: Epoch timestamp. Defaults to ``time.time()``.

        Returns:
            Error rate 0.0-1.0, or -1.0 when no data.
        """
        self._prune(self._success_timestamps, self._cfg.error_rate_window_seconds, now=now)
        self._prune(self._failure_timestamps, self._cfg.error_rate_window_seconds, now=now)
        total = len(self._success_timestamps) + len(self._failure_timestamps)
        if total == 0:
            return -1.0
        return len(self._failure_timestamps) / total

    def reset(self) -> None:
        """Clear all sliding-window buffers."""
        self._spawn_timestamps.clear()
        self._success_timestamps.clear()
        self._failure_timestamps.clear()

    # -- Internals ---------------------------------------------------------

    def _prune(
        self,
        timestamps: deque[float],
        window_seconds: int,
        now: float | None = None,
    ) -> None:
        """Remove entries older than the window from a deque.

        Args:
            timestamps: Sorted deque of epoch timestamps.
            window_seconds: How many seconds to retain (newest entries).
            now: Reference epoch. Defaults to ``time.time()``.
        """
        cutoff = (now if now is not None else time.time()) - window_seconds
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
