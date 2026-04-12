"""Background query throttling — tag API calls as foreground vs background.

Implements T496:

* QueryPriority enum: FOREGROUND (task-critical) vs BACKGROUND (housekeeping,
  session memory, cache warming).
* QueryThrottle class tracking foreground vs background call metrics.
* should_retry_background(attempt, response_code, is_overloaded) decides when
  background retries are dropped.
* When overloaded, background requests get stricter backoff or are dropped
  entirely while foreground requests retain normal retry behaviour.
* track_call(priority, success_or_failure) records per-priority metrics.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

_SUCCESS = "success"
_FAILURE = "failure"

# HTTP status codes that indicate provider overload.
_OVERLOAD_STATUS_CODES: frozenset[int] = frozenset({429, 529})

# Maximum background retries under overload (foreground keeps the caller's
# own retry policy; this only guards background calls).
_MAX_BACKGROUND_RETRIES_UNDER_OVERLOAD: int = 1

# Seconds to suppress all background traffic after repeated overload events.
_BACKGROUND_COOLDOWN_SECONDS: float = 60.0


class QueryPriority(enum.Enum):
    """Priority classification for API queries.

    FOREGROUND queries are task-critical: agent spawning, task completion,
    task decomposition — the core orchestration loop depends on these.

    BACKGROUND queries are housekeeping: session memory updates, cache
    warming, metrics export, bulletin board sync — these can be deferred
    or dropped during provider overload.
    """

    FOREGROUND = "foreground"
    BACKGROUND = "background"


@dataclass
class QueryMetrics:
    """Running metrics for calls at a given priority."""

    success_count: int = 0
    failure_count: int = 0
    last_failure_code: int | None = None
    last_failure_time: float | None = None
    total_retries_suppressed: int = 0


@dataclass(frozen=True)
class RetryDecision:
    """Result of should_retry_background evaluation.

    Attributes:
        should_retry: Whether the caller should issue another retry.
        reason: Human-readable reason for the decision.
        backoff_seconds: Suggested wait time before the next retry (0 = no wait).
    """

    should_retry: bool
    reason: str
    backoff_seconds: float


@dataclass
class _PriorityBucket:
    """Internal per-priority counter bucket."""

    foreground: QueryMetrics = field(default_factory=QueryMetrics)
    background: QueryMetrics = field(default_factory=QueryMetrics)


class QueryThrottle:
    """Track foreground vs background API calls and throttle under overload.

    Responsibilities:
    - Track per-priority success/failure metrics
    - Decide when background requests should be dropped or retried
    - Maintain a suppression window after repeated overload events
    - Provide a summary of call metrics for observability

    Usage in the orchestrator or LLM client::

        throttle = QueryThrottle()
        throttle.track_call(QueryPriority.FOREGROUND, success=True)
        throttle.track_call(QueryPriority.BACKGROUND, success=False, response_code=529)

        decision = throttle.should_retry_background(
            attempt=2,
            response_code=529,
            is_overloaded=True,
        )
        if decision.should_retry:
            await time.sleep(decision.backoff_seconds)
    """

    def __init__(
        self,
        *,
        max_background_retries_under_overload: int = _MAX_BACKGROUND_RETRIES_UNDER_OVERLOAD,
        background_cooldown_s: float = _BACKGROUND_COOLDOWN_SECONDS,
    ) -> None:
        self._max_bg_retries = max_background_retries_under_overload
        self._cooldown_s = background_cooldown_s
        self._buckets = _PriorityBucket()
        self._overload_detected_at: float | None = None
        self._background_suppressed_until: float = 0.0

    # ------------------------------------------------------------------
    # Call tracking
    # ------------------------------------------------------------------

    def track_call(
        self,
        priority: QueryPriority,
        *,
        success: bool,
        response_code: int | None = None,
    ) -> None:
        """Record an API call result for the given priority.

        Args:
            priority: The priority classification of the call.
            success: Whether the call succeeded (HTTP 2xx).
            response_code: The HTTP status code, useful for overload detection
                on failure. Pass None for successful calls.
        """
        bucket = self._buckets.foreground if priority == QueryPriority.FOREGROUND else self._buckets.background

        if success:
            bucket.success_count += 1
        else:
            bucket.failure_count += 1
            bucket.last_failure_code = response_code
            bucket.last_failure_time = time.time()

            # Detect overload status — triggers background suppression.
            if response_code in _OVERLOAD_STATUS_CODES:
                now = time.time()
                if self._overload_detected_at is None:
                    self._overload_detected_at = now
                self._background_suppressed_until = now + self._cooldown_s

    # ------------------------------------------------------------------
    # Retry decisions
    # ------------------------------------------------------------------

    def should_retry_background(
        self,
        *,
        attempt: int,
        response_code: int,
        is_overloaded: bool,
    ) -> RetryDecision:
        """Decide whether a background request should be retried.

        Under normal conditions, background requests may retry. Under provider
        overload (429/529), background retries are aggressively suppressed.

        Policy:
          - Not overloaded: always retry (up to caller's normal limit).
          - Overloaded, 1st background attempt: allow one retry.
          - Overloaded, 2nd+ background attempt: suppress all further retries.
          - During suppression window: drop immediately.

        Args:
            attempt: 1-based retry attempt number.
            response_code: HTTP status code from the failed call.
            is_overloaded: Whether the provider is in an overloaded state.

        Returns:
            RetryDecision with should_retry flag, reason, and backoff.
        """
        if not is_overloaded:
            return RetryDecision(
                should_retry=True,
                reason="not overloaded",
                backoff_seconds=0.0,
            )

        # Check if we're still in the background suppression window.
        now = time.time()
        if now < self._background_suppressed_until:
            self._buckets.background.total_retries_suppressed += 1
            remaining = self._background_suppressed_until - now
            return RetryDecision(
                should_retry=False,
                reason=f"background suppressed ({remaining:.0f}s remaining)",
                backoff_seconds=remaining,
            )

        # Outside suppression window but still overloaded.
        # Allow at most max_bg_retries under overload conditions.
        if attempt >= self._max_bg_retries:
            self._buckets.background.total_retries_suppressed += 1
            self._foreground_retry = True  # type: ignore[attr-defined]
            return RetryDecision(
                should_retry=False,
                reason=f"background retries exhausted ({self._max_bg_retries} max under overload)",
                backoff_seconds=self._cooldown_s,
            )

        allow_strict_backoff = response_code in _OVERLOAD_STATUS_CODES
        backoff = self._cooldown_s if allow_strict_backoff else 5.0
        return RetryDecision(
            should_retry=True,
            reason=f"background retry {attempt} under overload (strict backoff)",
            backoff_seconds=backoff,
        )

    # ------------------------------------------------------------------
    # Overload state accessors
    # ------------------------------------------------------------------

    @property
    def is_background_suppressed(self) -> bool:
        """Return True if background requests are currently suppressed."""
        return time.time() < self._background_suppressed_until

    @property
    def overload_cooldown_remaining(self) -> float:
        """Return seconds until background suppression ends (0 if ended)."""
        remaining = self._background_suppressed_until - time.time()
        return max(0.0, remaining)

    def reset(self) -> None:
        """Clear all throttle state, metrics, and suppression windows."""
        self._buckets = _PriorityBucket()
        self._overload_detected_at = None
        self._background_suppressed_until = 0.0

    # ------------------------------------------------------------------
    # Summary / observability
    # ------------------------------------------------------------------

    def summary(self) -> dict[Literal["foreground", "background"], dict[str, object]]:
        """Return a summary of foreground vs background call metrics.

        Returns:
            Dict with foreground and background keys, each containing
            success_count, failure_count, last_failure_code,
            total_retries_suppressed, and is_suppressed.
        """
        fg = self._buckets.foreground
        bg = self._buckets.background
        return {
            "foreground": {
                "success_count": fg.success_count,
                "failure_count": fg.failure_count,
                "last_failure_code": fg.last_failure_code,
                "total_retries_suppressed": fg.total_retries_suppressed,
                "is_suppressed": False,
            },
            "background": {
                "success_count": bg.success_count,
                "failure_count": bg.failure_count,
                "last_failure_code": bg.last_failure_code,
                "last_failure_time": round(bg.last_failure_time, 1) if bg.last_failure_time else None,
                "total_retries_suppressed": bg.total_retries_suppressed,
                "is_suppressed": self.is_background_suppressed,
                "overload_cooldown_remaining": round(self.overload_cooldown_remaining, 1),
            },
        }

    def foreground_total(self) -> int:
        """Return total foreground calls (success + failure)."""
        b = self._buckets.foreground
        return b.success_count + b.failure_count

    def background_total(self) -> int:
        """Return total background calls (success + failure)."""
        b = self._buckets.background
        return b.success_count + b.failure_count


# ------------------------------------------------------------------
# Helpers for integration
# ------------------------------------------------------------------


def is_overload_status(code: int, *, include_529: bool = True) -> bool:
    """Return True if HTTP *code* indicates provider overload.

    Args:
        code: HTTP status code from the response.
        include_529: Include 529 (Cloudflare overloaded) in the check.

    Returns:
        True if the status code signals overload.
    """
    if include_529:
        return code in (429, 529)
    return code == 429
