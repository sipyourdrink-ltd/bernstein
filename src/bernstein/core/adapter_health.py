"""Per-adapter health monitoring (AGENT-009).

Tracks success/failure rates per adapter.  Adapters that exceed a 50%
failure rate are automatically disabled.  They are re-enabled after a
configurable cooldown period.

Usage::

    monitor = AdapterHealthMonitor()
    monitor.record_success("claude")
    monitor.record_failure("codex")
    if not monitor.is_healthy("codex"):
        # skip codex for now
        ...
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_FAILURE_THRESHOLD: float = 0.5  # 50%
_DEFAULT_COOLDOWN_SECONDS: float = 300.0  # 5 minutes
_DEFAULT_MIN_SAMPLES: int = 3  # Need at least this many results to judge


@dataclass
class AdapterHealthConfig:
    """Configuration for adapter health monitoring.

    Attributes:
        failure_threshold: Fraction of failures (0.0-1.0) above which
            an adapter is automatically disabled.
        cooldown_seconds: Seconds to wait before re-enabling a disabled adapter.
        min_samples: Minimum number of spawn attempts before judging health.
        window_seconds: Rolling window for tracking success/failure counts.
    """

    failure_threshold: float = _DEFAULT_FAILURE_THRESHOLD
    cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS
    min_samples: int = _DEFAULT_MIN_SAMPLES
    window_seconds: float = 600.0  # 10-minute rolling window


# ---------------------------------------------------------------------------
# Per-adapter stats
# ---------------------------------------------------------------------------


@dataclass
class AdapterStats:
    """Health statistics for a single adapter.

    Attributes:
        adapter_name: Name of the adapter.
        successes: Count of successful spawns in the current window.
        failures: Count of failed spawns in the current window.
        disabled: Whether this adapter is currently disabled.
        disabled_at: Monotonic timestamp when the adapter was disabled.
        timestamps: List of (timestamp, success) tuples for windowed tracking.
    """

    adapter_name: str
    successes: int = 0
    failures: int = 0
    disabled: bool = False
    disabled_at: float = 0.0
    timestamps: list[tuple[float, bool]] = field(default_factory=list[tuple[float, bool]])

    @property
    def total(self) -> int:
        """Total spawn attempts."""
        return self.successes + self.failures

    @property
    def failure_rate(self) -> float:
        """Current failure rate (0.0-1.0), or 0.0 if no samples."""
        if self.total == 0:
            return 0.0
        return self.failures / self.total


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class AdapterHealthMonitor:
    """Track per-adapter health and auto-disable failing adapters.

    Not thread-safe -- designed for single-threaded async orchestrator loop.

    Args:
        config: Health monitoring configuration.
    """

    def __init__(self, config: AdapterHealthConfig | None = None) -> None:
        self._config = config or AdapterHealthConfig()
        self._stats: dict[str, AdapterStats] = {}

    @property
    def config(self) -> AdapterHealthConfig:
        """Return the health monitoring configuration."""
        return self._config

    def _get_stats(self, adapter_name: str) -> AdapterStats:
        """Get or create stats for an adapter.

        Args:
            adapter_name: Adapter identifier.

        Returns:
            AdapterStats for the adapter.
        """
        if adapter_name not in self._stats:
            self._stats[adapter_name] = AdapterStats(adapter_name=adapter_name)
        return self._stats[adapter_name]

    def _prune_window(self, stats: AdapterStats) -> None:
        """Remove events outside the rolling window and recompute counts.

        Args:
            stats: Adapter stats to prune.
        """
        cutoff = time.monotonic() - self._config.window_seconds
        stats.timestamps = [(ts, ok) for ts, ok in stats.timestamps if ts >= cutoff]
        stats.successes = sum(1 for _, ok in stats.timestamps if ok)
        stats.failures = sum(1 for _, ok in stats.timestamps if not ok)

    def record_success(self, adapter_name: str) -> None:
        """Record a successful spawn for the given adapter.

        Args:
            adapter_name: Adapter identifier.
        """
        stats = self._get_stats(adapter_name)
        stats.timestamps.append((time.monotonic(), True))
        self._prune_window(stats)
        logger.debug(
            "Adapter %s: success (rate=%.0f%%, %d/%d)",
            adapter_name,
            (1 - stats.failure_rate) * 100,
            stats.successes,
            stats.total,
        )

    def record_failure(self, adapter_name: str) -> None:
        """Record a failed spawn for the given adapter.

        Auto-disables the adapter if the failure rate exceeds the threshold
        and enough samples have been collected.

        Args:
            adapter_name: Adapter identifier.
        """
        stats = self._get_stats(adapter_name)
        stats.timestamps.append((time.monotonic(), False))
        self._prune_window(stats)
        logger.debug(
            "Adapter %s: failure (rate=%.0f%%, %d/%d)",
            adapter_name,
            stats.failure_rate * 100,
            stats.failures,
            stats.total,
        )

        if (
            not stats.disabled
            and stats.total >= self._config.min_samples
            and stats.failure_rate > self._config.failure_threshold
        ):
            stats.disabled = True
            stats.disabled_at = time.monotonic()
            logger.warning(
                "Adapter %s auto-disabled: failure rate %.0f%% > %.0f%% threshold (%d samples)",
                adapter_name,
                stats.failure_rate * 100,
                self._config.failure_threshold * 100,
                stats.total,
            )

    def is_healthy(self, adapter_name: str) -> bool:
        """Check if an adapter is healthy (not disabled or cooldown expired).

        If the adapter was disabled and the cooldown has elapsed, it is
        automatically re-enabled.

        Args:
            adapter_name: Adapter identifier.

        Returns:
            True if the adapter can be used.
        """
        stats = self._stats.get(adapter_name)
        if stats is None:
            return True  # Unknown adapters are assumed healthy

        if not stats.disabled:
            return True

        # Check cooldown expiry
        elapsed = time.monotonic() - stats.disabled_at
        if elapsed >= self._config.cooldown_seconds:
            stats.disabled = False
            stats.timestamps.clear()
            stats.successes = 0
            stats.failures = 0
            logger.info(
                "Adapter %s re-enabled after %.0fs cooldown",
                adapter_name,
                elapsed,
            )
            return True

        return False

    def get_stats(self, adapter_name: str) -> AdapterStats | None:
        """Get current stats for an adapter.

        Args:
            adapter_name: Adapter identifier.

        Returns:
            AdapterStats or None if no data recorded.
        """
        stats = self._stats.get(adapter_name)
        if stats is not None:
            self._prune_window(stats)
        return stats

    def all_stats(self) -> dict[str, AdapterStats]:
        """Return stats for all tracked adapters.

        Returns:
            Dict mapping adapter name to AdapterStats.
        """
        for stats in self._stats.values():
            self._prune_window(stats)
        return dict(self._stats)

    def reset(self, adapter_name: str) -> None:
        """Reset all stats for an adapter.

        Args:
            adapter_name: Adapter identifier.
        """
        self._stats.pop(adapter_name, None)
