"""Rate-limit-aware scheduling: per-provider throttle tracking and 429 detection.

Implements:
- Exponential-backoff throttling when a 429 is detected (60s → 120s → … ≤ 3600s)
- Log scanning to infer 429 events from agent subprocess output
- Auto-recovery when the throttle window expires (called once per tick)
- Active-agent counts per provider for load-spreading in router scoring
- Unattended retry policy: persistent retry with heartbeats for headless runs
- Background query throttling: during overload, background requests
  (housekeeping, cache warming) are suppressed while foreground requests
  (task-critical agent spawns) retain fair retry access.
"""

from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.router import TierAwareRouter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request priority classification
# ---------------------------------------------------------------------------


class RequestPriority(enum.Enum):
    """Priority for API requests under rate-limit conditions.

    FOREGROUND requests are task-critical (agent spawning, task completion).
    BACKGROUND requests are housekeeping (cache warming, session memory,
    metrics export, bulletin board updates).

    Under provider overload (429/529), BACKGROUND requests are suppressed
    while FOREGROUND requests retain their standard retry behaviour.
    """

    FOREGROUND = "foreground"
    BACKGROUND = "background"


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
    "hit your limit",
    "usage cap",
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

# Text patterns that indicate an authentication error (401, 403) in agent logs.
_AUTH_ERROR_PATTERNS: tuple[str, ...] = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "invalid_client",
    "invalid_token",
    "expired_token",
    "AuthenticationError",
    "PermissionDeniedError",
)

# Text patterns that indicate a context-overflow / prompt-too-long (413) error.
_CONTEXT_OVERFLOW_PATTERNS: tuple[str, ...] = (
    "413",
    "prompt is too long",
    "prompt too long",
    "context window",
    "context_length_exceeded",
    "max_tokens",
    "maximum context length",
    "token limit exceeded",
    "request too large",
    "payload too large",
    "input is too long",
    "prompt_too_long",
    "context length exceeded",
    "PromptTooLongError",
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
    background_suppressed_until: float = 0.0  # Unix timestamp when background suppression ends


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
        background_max_delay: float = 30.0,
    ) -> None:
        self._base_s = base_throttle_s
        self._max_s = max_throttle_s
        self._background_max_delay = background_max_delay
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
        throttle_end = time.time() + duration_s
        # Background suppression: first trigger only suppresses retries beyond
        # the first attempt (suppressed_until = throttle_end). Higher triggers
        # suppress for the background cooldown window.
        if trigger_count <= 1:
            bg_suppressed_until = throttle_end
        else:
            bg_suppressed_until = throttle_end - (duration_s - self._background_max_delay)
            bg_suppressed_until = max(bg_suppressed_until, time.time())
        self._throttles[provider] = ThrottleState(
            provider=provider,
            throttled_until=throttle_end,
            trigger_count=trigger_count,
            background_suppressed_until=bg_suppressed_until,
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
    # Background query throttling
    # ------------------------------------------------------------------

    def suppress_background_request(
        self,
        provider: str,
        *,
        attempt: int = 1,
    ) -> bool:
        """Return True if a background request should be suppressed.

        During provider overload (429/529), background requests are
        aggressively suppressed to reduce load while foreground requests
        (agent spawning, task completion) retain standard retries.

        Suppression policy:
        - Not throttled: never suppress background requests.
        - Throttled 1st trigger: suppress background retries beyond attempt 1.
        - Throttled 2nd+ trigger: drop all background requests immediately
          for the first ``_background_max_delay`` seconds (default 30 s),
          then retry once.

        Args:
            provider: Provider name for the request.
            attempt: Retry attempt number (1-based, 1 = first attempt).

        Returns:
            True if the background request should be skipped.
        """
        state = self._throttles.get(provider)
        if state is None:
            return False  # No overload — let background proceed

        now = time.time()
        if now >= state.throttled_until:
            return False  # Throttle expired — let background proceed

        # Provider is currently throttled — apply background suppression
        if state.trigger_count == 1:
            # First trigger: only suppress beyond the first retry
            return attempt > 1

        # Higher triggers: suppress all background requests until
        # background_suppressed_until expires.
        return time.time() < state.background_suppressed_until

    def classify_request(self, *, has_task_id: bool = False, is_spawn: bool = False) -> RequestPriority:
        """Classify a request as FOREGROUND or BACKGROUND.

        Requests related to task execution (spawning agents, completing tasks)
        are FOREGROUND. Everything else is BACKGROUND.

        Args:
            has_task_id: True if the request is associated with a task.
            is_spawn: True if the request is an agent spawn operation.

        Returns:
            RequestPriority classification.
        """
        if is_spawn or has_task_id:
            return RequestPriority.FOREGROUND
        return RequestPriority.BACKGROUND

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

    def scan_log_for_auth_error(self, log_path: Path) -> bool:
        """Scan the tail of *log_path* for auth error patterns (401, 403).

        Args:
            log_path: Path to the agent's subprocess log file.

        Returns:
            True if an auth error indicator was found, False otherwise.
        """
        return self._scan_log_for_patterns(log_path, _AUTH_ERROR_PATTERNS)

    def scan_log_for_context_overflow(self, log_path: Path) -> bool:
        """Scan the tail of *log_path* for context-overflow / 413 patterns.

        Detects prompt-too-long errors emitted by providers when the agent's
        context window is exceeded (HTTP 413 or equivalent error messages).

        Args:
            log_path: Path to the agent's subprocess log file.

        Returns:
            True if a context-overflow indicator was found, False otherwise.
        """
        return self._scan_log_for_patterns(log_path, _CONTEXT_OVERFLOW_PATTERNS)

    def detect_failure_type(self, log_path: Path) -> str | None:
        """Scan an agent log and return the detected failure type.

        Checks for rate limits first, then context overflow, then timeouts,
        then auth errors, then general API errors.

        Args:
            log_path: Path to the agent's subprocess log file.

        Returns:
            One of ``"rate_limit"``, ``"context_overflow"``, ``"timeout"``,
            ``"auth_error"``, ``"api_error"``, or ``None`` if no failure
            pattern was detected.
        """
        if self.scan_log_for_429(log_path):
            return "rate_limit"
        if self.scan_log_for_context_overflow(log_path):
            return "context_overflow"
        if self.scan_log_for_timeout(log_path):
            return "timeout"
        if self.scan_log_for_auth_error(log_path):
            return "auth_error"
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
# Unattended retry policy: persistent retry with heartbeats for headless runs
# ------------------------------------------------------------------

_UNATTENDED_RETRY_ENV_VAR = "BERNSTEIN_UNATTENDED"
_DEFAULT_MAX_RETRIES = 10
_DEFAULT_BASE_DELAY = 5.0
_DEFAULT_MAX_DELAY = 300.0
_DEFAULT_HEARTBEAT_INTERVAL = 30.0

# HTTP status codes that should trigger unattended retry.
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 529})


def is_unattended_mode() -> bool:
    """Return True when running in unattended (headless) mode.

    Checks the ``BERNSTEIN_UNATTENDED`` environment variable. When set to
    any truthy value (``"1"``, ``"true"``, ``"yes"``), unattended retry
    policies are applied.

    Returns:
        True if unattended mode is enabled.
    """
    return os.environ.get(_UNATTENDED_RETRY_ENV_VAR, "").lower() in {"1", "true", "yes"}


@dataclass
class UnattendedRetryPolicy:
    """Persistent retry policy for unattended/headless mode.

    When a spawn fails with a 429 or 529 (rate-limit / provider overload)
    response, this policy:

    - Determines whether the error is retryable
    - Computes an exponential backoff delay (capped at *max_delay*)
    - Emits periodic heartbeat signals so upstream monitors know the run
      is still alive and not stuck
    - Tracks the retry attempt count so callers can decide when to bail out

    Args:
        max_retries: Maximum number of retry attempts before giving up.
        base_delay: Base delay in seconds for the first retry.
        max_delay: Maximum delay ceiling (seconds) for exponential backoff.
        heartbeat_interval: How often (seconds) to emit a heartbeat during
            retry waits.
    """

    max_retries: int = _DEFAULT_MAX_RETRIES
    base_delay: float = _DEFAULT_BASE_DELAY
    max_delay: float = _DEFAULT_MAX_DELAY
    heartbeat_interval: float = _DEFAULT_HEARTBEAT_INTERVAL

    def should_retry(self, attempt: int, response_code: int) -> bool:
        """Return True if the response code warrants another retry.

        Checks both the HTTP status code (429 / 529) and whether the
        maximum retry count has been exceeded.

        Args:
            attempt: Current 1-based attempt number.
            response_code: HTTP status code from the failed response.

        Returns:
            True if another retry attempt should be made.
        """
        if response_code not in _RETRYABLE_STATUS_CODES:
            return False
        return attempt < self.max_retries

    def next_delay(self, attempt: int) -> float:
        """Compute the backoff delay for the next retry attempt.

        Uses exponential backoff: ``base_delay * 2^(attempt-1)``,
        capped at ``max_delay``.

        Args:
            attempt: 1-based attempt number for which to compute delay.

        Returns:
            Delay in seconds before the next retry.
        """
        return min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)

    def emit_heartbeat(
        self,
        session_id: str,
        attempt: int,
        reason: str,
        signals_dir: Path | None = None,
    ) -> None:
        """Write a heartbeat signal so upstream monitors know the run is alive.

        Writes a ``HEARTBEAT`` file to ``.sdd/runtime/signals/<session_id>/``
        and logs at INFO level.

        Args:
            session_id: Agent session identifier (used for signal path).
            attempt: Current retry attempt number.
            reason: Human-readable reason for the retry (e.g. "429 rate limit").
            signals_dir: Optional path override; defaults to ``.sdd/runtime/signals/``
                in the current working directory.
        """
        if signals_dir is None:
            signals_dir = Path.cwd() / ".sdd" / "runtime" / "signals"
        signal_dir = signals_dir / session_id
        try:
            signal_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.debug("Failed to create signal dir for unattended heartbeat: %s", exc)
            return
        content = f"# UNATTENDED RETRY HEARTBEAT\nAttempt: {attempt}\nReason: {reason}\nTimestamp: {time.time()}\n"
        try:
            (signal_dir / "HEARTBEAT").write_text(content, encoding="utf-8")
        except OSError as exc:
            logger.debug("Failed to write unattended heartbeat: %s", exc)
        logger.info(
            "Unattended retry heartbeat: session=%s attempt=%d reason=%s",
            session_id,
            attempt,
            reason,
        )

    def wait_with_heartbeats(
        self,
        session_id: str,
        attempt: int,
        reason: str,
        signals_dir: Path | None = None,
    ) -> None:
        """Sleep for the computed backoff, emitting heartbeats at intervals.

        This is a blocking call intended for unattended mode where the
        orchestrator must signal it is still alive while waiting.

        Args:
            session_id: Agent session identifier.
            attempt: Current 1-based retry attempt.
            reason: Human-readable reason for the retry.
            signals_dir: Optional path override for signal files.
        """
        delay = self.next_delay(attempt)
        self.emit_heartbeat(session_id, attempt, reason, signals_dir)
        remaining = delay
        while remaining > 0:
            sleep_for = min(self.heartbeat_interval, remaining)
            time.sleep(sleep_for)
            remaining -= sleep_for
            if remaining > 0:
                self.emit_heartbeat(session_id, attempt, reason, signals_dir)


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
