"""Rate-limit-aware scheduling: per-provider throttle tracking and 429 detection.

Implements:
- Exponential-backoff throttling when a 429 is detected (60s → 120s → … ≤ 3600s)
- Log scanning to infer 429 events from agent subprocess output
- Auto-recovery when the throttle window expires (called once per tick)
- Active-agent counts per provider for load-spreading in router scoring
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.router import TierAwareRouter

logger = logging.getLogger(__name__)

# Text patterns that indicate a rate-limit / 429 event in agent logs.
# Checked case-insensitively against the last 500 lines of the log.
_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "429",
    "too many requests",
    "quota exceeded",
    "RateLimitError",
    "overloaded_error",
    "overloaded",
)

# Text patterns that indicate a timeout in agent logs.
_TIMEOUT_PATTERNS: tuple[str, ...] = (
    "timeout",
    "timed out",
    "time out",
    "deadline exceeded",
    "request timeout",
    "connect timeout",
    "read timeout",
    "ETIMEDOUT",
    "TimeoutError",
    "ConnectTimeoutError",
    "ReadTimeoutError",
    "504",
    "gateway timeout",
)

# Text patterns that indicate an API error (non-429, non-timeout) in agent logs.
_API_ERROR_PATTERNS: tuple[str, ...] = (
    "500 internal server error",
    "502 bad gateway",
    "503 service unavailable",
    "APIError",
    "api_error",
    "InternalServerError",
    "ServiceUnavailableError",
    "APIConnectionError",
    "connection refused",
    "connection reset",
    "ECONNREFUSED",
    "ECONNRESET",
)

_BASE_THROTTLE_S: float = 60.0
_MAX_THROTTLE_S: float = 3600.0
_LOG_SCAN_TAIL_LINES: int = 500


@dataclass
class ThrottleState:
    """Throttle entry for a single provider."""

    provider: str
    throttled_until: float  # Unix timestamp when the throttle expires
    trigger_count: int = 1  # Increases on each consecutive throttle


class RateLimitTracker:
    """Track per-provider rate-limit throttle state.

    Responsibilities:
    - Detect 429 events by scanning agent log files (no cloud API involved)
    - Apply exponential-backoff throttles to the affected provider
    - Auto-recover providers whose throttle window has expired
    - Track active agent counts per provider for load-spreading

    Usage in the orchestrator tick loop::

        # At start of each tick:
        orch._rate_limit_tracker.recover_expired_throttles(orch._router)

        # After a successful spawn:
        orch._rate_limit_tracker.increment_active(session.provider)

        # When an agent dies:
        orch._rate_limit_tracker.decrement_active(session.provider)
        if orch._rate_limit_tracker.scan_log_for_429(log_path):
            orch._rate_limit_tracker.throttle_provider(provider, orch._router)
    """

    def __init__(
        self,
        base_throttle_s: float = _BASE_THROTTLE_S,
        max_throttle_s: float = _MAX_THROTTLE_S,
    ) -> None:
        self._base_s = base_throttle_s
        self._max_s = max_throttle_s
        # provider -> ThrottleState
        self._throttles: dict[str, ThrottleState] = {}
        # provider -> number of currently-running agents
        self._active_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Active-agent accounting (used by router spreading score)
    # ------------------------------------------------------------------

    def increment_active(self, provider: str) -> None:
        """Record that a new agent was spawned for *provider*."""
        self._active_counts[provider] = self._active_counts.get(provider, 0) + 1

    def decrement_active(self, provider: str) -> None:
        """Record that an agent on *provider* has exited."""
        self._active_counts[provider] = max(0, self._active_counts.get(provider, 0) - 1)

    def get_active_count(self, provider: str) -> int:
        """Return the number of currently-running agents on *provider*."""
        return self._active_counts.get(provider, 0)

    def get_all_active_counts(self) -> dict[str, int]:
        """Return a snapshot of active-agent counts keyed by provider name."""
        return dict(self._active_counts)

    # ------------------------------------------------------------------
    # Throttle management
    # ------------------------------------------------------------------

    def is_throttled(self, provider: str) -> bool:
        """Return True if *provider* is currently within a throttle window."""
        state = self._throttles.get(provider)
        if state is None:
            return False
        if time.time() >= state.throttled_until:
            del self._throttles[provider]
            return False
        return True

    def throttle_provider(
        self,
        provider: str,
        router: TierAwareRouter | None = None,
    ) -> float:
        """Mark *provider* as throttled using exponential back-off.

        The first throttle lasts ``base_throttle_s`` seconds.  Each subsequent
        call while the provider is still in the throttle map doubles the window,
        up to ``max_throttle_s``.

        Args:
            provider: Provider name to throttle.
            router: When supplied, sets provider health status to RATE_LIMITED.

        Returns:
            Duration (seconds) of the throttle that was applied.
        """
        existing = self._throttles.get(provider)
        trigger_count = existing.trigger_count + 1 if existing else 1
        duration_s = min(self._base_s * (2 ** (trigger_count - 1)), self._max_s)
        self._throttles[provider] = ThrottleState(
            provider=provider,
            throttled_until=time.time() + duration_s,
            trigger_count=trigger_count,
        )
        logger.warning(
            "Provider %r throttled for %.0f s (trigger #%d)",
            provider,
            duration_s,
            trigger_count,
        )
        if router is not None:
            _set_router_rate_limited(router, provider)
        return duration_s

    def recover_expired_throttles(
        self,
        router: TierAwareRouter | None = None,
    ) -> list[str]:
        """Recover providers whose throttle window has expired.

        Intended to be called once per orchestrator tick, before spawning.

        Args:
            router: When supplied, restores recovered providers to HEALTHY.

        Returns:
            List of provider names that were recovered this call.
        """
        now = time.time()
        recovered: list[str] = []
        for provider, state in list(self._throttles.items()):
            if now >= state.throttled_until:
                del self._throttles[provider]
                recovered.append(provider)
                logger.info("Provider %r recovered from rate-limit throttle", provider)
                if router is not None:
                    _set_router_healthy(router, provider)
        return recovered

    def throttle_summary(self) -> dict[str, float]:
        """Return a {provider: seconds_remaining} map for all active throttles."""
        now = time.time()
        return {p: max(0.0, s.throttled_until - now) for p, s in self._throttles.items()}

    # ------------------------------------------------------------------
    # 429 detection via log scanning
    # ------------------------------------------------------------------

    def scan_log_for_429(self, log_path: Path) -> bool:
        """Scan the tail of *log_path* for rate-limit / 429 patterns.

        Checks the last ``_LOG_SCAN_TAIL_LINES`` lines only, keeping the scan
        fast even for large logs.

        Args:
            log_path: Path to the agent's subprocess log file.

        Returns:
            True if a rate-limit indicator was found, False otherwise (including
            when the file does not exist or cannot be read).
        """
        return self._scan_log_for_patterns(log_path, _RATE_LIMIT_PATTERNS)

    def scan_log_for_timeout(self, log_path: Path) -> bool:
        """Scan the tail of *log_path* for timeout patterns.

        Args:
            log_path: Path to the agent's subprocess log file.

        Returns:
            True if a timeout indicator was found, False otherwise.
        """
        return self._scan_log_for_patterns(log_path, _TIMEOUT_PATTERNS)

    def scan_log_for_api_error(self, log_path: Path) -> bool:
        """Scan the tail of *log_path* for API error patterns (non-429, non-timeout).

        Args:
            log_path: Path to the agent's subprocess log file.

        Returns:
            True if an API error indicator was found, False otherwise.
        """
        return self._scan_log_for_patterns(log_path, _API_ERROR_PATTERNS)

    def detect_failure_type(self, log_path: Path) -> str | None:
        """Scan an agent log and return the detected failure type.

        Checks for rate limits first, then timeouts, then general API errors.

        Args:
            log_path: Path to the agent's subprocess log file.

        Returns:
            One of ``"rate_limit"``, ``"timeout"``, ``"api_error"``, or ``None``
            if no failure pattern was detected.
        """
        if self.scan_log_for_429(log_path):
            return "rate_limit"
        if self.scan_log_for_timeout(log_path):
            return "timeout"
        if self.scan_log_for_api_error(log_path):
            return "api_error"
        return None

    def _scan_log_for_patterns(self, log_path: Path, patterns: tuple[str, ...]) -> bool:
        """Scan the tail of *log_path* for any of the given patterns.

        Args:
            log_path: Path to the agent's subprocess log file.
            patterns: Tuple of strings to search for (case-insensitive).

        Returns:
            True if any pattern was found, False otherwise.
        """
        if not log_path.exists():
            return False
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.debug("_scan_log_for_patterns: cannot read %s: %s", log_path, exc)
            return False

        lines = text.splitlines()[-_LOG_SCAN_TAIL_LINES:]
        snippet = "\n".join(lines).lower()
        return any(pat.lower() in snippet for pat in patterns)


# ------------------------------------------------------------------
# Router integration helpers (module-level to keep the class lean)
# ------------------------------------------------------------------


def _set_router_rate_limited(router: TierAwareRouter, provider: str) -> None:
    """Set a provider's health status to RATE_LIMITED in the router."""
    from bernstein.core.router import ProviderHealthStatus

    if provider in router.state.providers:
        router.state.providers[provider].health.status = ProviderHealthStatus.RATE_LIMITED
        logger.debug("Router: provider %r marked RATE_LIMITED", provider)


def _set_router_healthy(router: TierAwareRouter, provider: str) -> None:
    """Restore a provider's health status to HEALTHY (only if RATE_LIMITED)."""
    from bernstein.core.router import ProviderHealthStatus

    if provider in router.state.providers:
        p = router.state.providers[provider]
        if p.health.status == ProviderHealthStatus.RATE_LIMITED:
            p.health.status = ProviderHealthStatus.HEALTHY
            logger.debug("Router: provider %r restored to HEALTHY", provider)
