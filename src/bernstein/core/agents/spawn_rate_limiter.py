"""Spawn rate limiter — prevent API throttling from too-rapid spawns (AGENT-007).

Limits the number of agent spawns per provider within a configurable time
window.  Default: max 2 spawns per 10 seconds per provider.

This is a local, in-memory token-bucket-style rate limiter scoped to the
orchestrator process.  It does NOT replace provider-side rate limits — it
prevents hitting them in the first place.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Default maximum spawns per window per provider.
DEFAULT_MAX_SPAWNS: int = 2

#: Default rate limit window in seconds.
DEFAULT_WINDOW_SECONDS: float = 10.0


class SpawnRateLimitExceeded(Exception):
    """Raised when spawn rate limit is exceeded for a provider.

    Attributes:
        provider: The rate-limited provider name.
        retry_after_s: Seconds to wait before retrying.
    """

    def __init__(self, provider: str, retry_after_s: float) -> None:
        self.provider = provider
        self.retry_after_s = retry_after_s
        super().__init__(f"Spawn rate limit exceeded for provider '{provider}'. Retry after {retry_after_s:.1f}s.")


@dataclass
class SpawnRateLimitConfig:
    """Configuration for the spawn rate limiter.

    Attributes:
        max_spawns: Maximum number of spawns per window per provider.
        window_seconds: Length of the sliding window in seconds.
        per_provider_overrides: Optional per-provider max_spawns overrides.
    """

    max_spawns: int = DEFAULT_MAX_SPAWNS
    window_seconds: float = DEFAULT_WINDOW_SECONDS
    per_provider_overrides: dict[str, int] = field(default_factory=dict[str, int])


class SpawnRateLimiter:
    """Sliding-window rate limiter for agent spawns per provider.

    Thread-safe.  Each provider has its own window of recent spawn timestamps.
    A spawn is allowed if the number of timestamps within the window is below
    the configured maximum.

    Args:
        config: Rate limit configuration.  When None, uses defaults
            (2 spawns per 10 seconds).
    """

    def __init__(self, config: SpawnRateLimitConfig | None = None) -> None:
        self._config = config or SpawnRateLimitConfig()
        self._lock = threading.Lock()
        self._spawn_times: dict[str, list[float]] = defaultdict(list)

    @property
    def config(self) -> SpawnRateLimitConfig:
        """Current rate limit configuration."""
        return self._config

    def _max_for_provider(self, provider: str) -> int:
        """Return the max spawns for a provider (with override support).

        Args:
            provider: Provider name.

        Returns:
            Maximum allowed spawns in the current window.
        """
        return self._config.per_provider_overrides.get(provider, self._config.max_spawns)

    def _prune_old(self, provider: str, now: float) -> None:
        """Remove timestamps outside the current window.

        Args:
            provider: Provider name.
            now: Current monotonic time.
        """
        cutoff = now - self._config.window_seconds
        timestamps = self._spawn_times[provider]
        self._spawn_times[provider] = [t for t in timestamps if t > cutoff]

    def check(self, provider: str) -> float:
        """Check if a spawn is allowed for the given provider.

        Does NOT record a spawn — call ``record()`` after a successful spawn.

        Args:
            provider: Provider name to check.

        Returns:
            0.0 if the spawn is allowed, otherwise the number of seconds
            to wait before the next spawn slot opens.
        """
        with self._lock:
            now = time.monotonic()
            self._prune_old(provider, now)
            max_spawns = self._max_for_provider(provider)
            timestamps = self._spawn_times[provider]

            if len(timestamps) < max_spawns:
                return 0.0

            # Earliest timestamp in the window — retry after it expires
            oldest = min(timestamps)
            retry_after = (oldest + self._config.window_seconds) - now
            return max(0.0, retry_after)

    def record(self, provider: str) -> None:
        """Record a spawn for the given provider.

        Args:
            provider: Provider name.
        """
        with self._lock:
            now = time.monotonic()
            self._prune_old(provider, now)
            self._spawn_times[provider].append(now)

    def acquire(self, provider: str) -> None:
        """Check and record a spawn, raising if the limit is exceeded.

        Convenience method that combines ``check()`` and ``record()``.

        Args:
            provider: Provider name.

        Raises:
            SpawnRateLimitExceeded: If the rate limit is exceeded.
        """
        retry_after = self.check(provider)
        if retry_after > 0:
            raise SpawnRateLimitExceeded(provider, retry_after)
        self.record(provider)

    def wait_and_acquire(
        self,
        provider: str,
        *,
        max_wait: float = 30.0,
    ) -> float:
        """Block until a spawn slot is available, then record it.

        Args:
            provider: Provider name.
            max_wait: Maximum seconds to wait.  Raises after this.

        Returns:
            Seconds actually waited (0.0 if no wait was needed).

        Raises:
            SpawnRateLimitExceeded: If still rate-limited after max_wait.
        """
        waited = 0.0
        while True:
            retry_after = self.check(provider)
            if retry_after <= 0:
                self.record(provider)
                return waited
            if waited + retry_after > max_wait:
                raise SpawnRateLimitExceeded(provider, retry_after)
            time.sleep(retry_after)
            waited += retry_after

    def reset(self, provider: str | None = None) -> None:
        """Reset spawn timestamps.

        Args:
            provider: Provider to reset.  None = reset all providers.
        """
        with self._lock:
            if provider is not None:
                self._spawn_times.pop(provider, None)
            else:
                self._spawn_times.clear()

    def stats(self) -> dict[str, int]:
        """Return current spawn counts within the window per provider.

        Returns:
            Dict mapping provider name to number of spawns in the current window.
        """
        with self._lock:
            now = time.monotonic()
            result: dict[str, int] = {}
            for provider in list(self._spawn_times.keys()):
                self._prune_old(provider, now)
                count = len(self._spawn_times[provider])
                if count > 0:
                    result[provider] = count
            return result
