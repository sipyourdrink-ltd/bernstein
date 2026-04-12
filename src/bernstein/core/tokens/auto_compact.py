"""Auto-compact trigger with circuit breaker."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import auto

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class AutoCompactConfig:
    """Configuration for automatic context compaction.

    Attributes:
        threshold_pct: Context-window utilization percentage (0-100) that
            triggers an auto-compaction attempt.  Defaults to 80%.
        max_consecutive_failures: Number of consecutive compaction failures
            before the circuit breaker opens.  Defaults to 3.
        retry_delay_s: Seconds to wait before retrying compaction after the
            circuit breaker opens.  Defaults to 120s.
    """

    threshold_pct: float = 80.0
    max_consecutive_failures: int = 3
    retry_delay_s: float = 120.0


# ---------------------------------------------------------------------------
# Circuit breaker state
# ---------------------------------------------------------------------------


class CircuitState:
    """Circuit breaker states for auto-compaction."""

    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


# ---------------------------------------------------------------------------
# Auto-compact trigger
# ---------------------------------------------------------------------------


@dataclass
class AutoCompactTrigger:
    """Manages auto-compaction decisions with a circuit breaker.

    Tracks per-session compaction attempts and prevents infinite compaction
    loops by opening the circuit breaker after consecutive failures.

    State machine::

        CLOSED -> OPEN: consecutive_failures >= max_consecutive_failures
        OPEN -> HALF_OPEN: now - last_failure_ts >= retry_delay_s
        HALF_OPEN -> CLOSED: successful compaction (reset)
        HALF_OPEN -> OPEN: failed compaction (back to OPEN)

    Attributes:
        session_id: The agent session this trigger belongs to.
        config: Compaction configuration.
        state: Current circuit breaker state (default: CLOSED).
        consecutive_failures: Number of consecutive compaction failures.
        last_failure_ts: Timestamp of the last failure (for cooldown timing).
        last_attempt_ts: Timestamp of the last compaction attempt.
        total_attempts: Lifelong count of compaction attempts.
        total_successes: Lifelong count of successful compactions.
    """

    session_id: str
    config: AutoCompactConfig
    state: int = field(default=CircuitState.CLOSED)
    consecutive_failures: int = 0
    last_failure_ts: float = 0.0
    last_attempt_ts: float = 0.0
    total_attempts: int = 0
    total_successes: int = 0

    def should_compact(
        self,
        current_tokens: int,
        max_tokens: int,
        now: float | None = None,
    ) -> bool:
        """Return True if auto-compaction should be attempted.

        Compaction is considered when estimated prompt tokens approach the
        model's context window (exceeding the configured threshold).  The
        circuit breaker determines whether an attempt is actually allowed.

        Args:
            current_tokens: Current estimated token count.
            max_tokens: Maximum context window tokens for the model.
            now: Current timestamp (for cooldown checking; defaults to
                ``time.time()``).

        Returns:
            True when compaction should be triggered.
        """
        if max_tokens <= 0:
            return False

        utilization_pct = (current_tokens / max_tokens) * 100.0

        if utilization_pct < self.config.threshold_pct:
            return False

        return self._circuit_allows(now=now)

    def _circuit_allows(self, now: float | None = None) -> bool:
        """Return True when the circuit breaker permits an attempt.

        In CLOSED state: always allow.
        In OPEN state: allow only after cooldown has elapsed.
        In HALF_OPEN state: always allow (one attempt).

        Args:
            now: Current timestamp (defaults to ``time.time()``).

        Returns:
            True when compaction should proceed.
        """
        now = now if now is not None else time.time()
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if now - self.last_failure_ts >= self.config.retry_delay_s:
                self.state = CircuitState.HALF_OPEN
                logger.info(
                    "Circuit breaker for session %s: OPEN -> HALF_OPEN (cooldown elapsed)",
                    self.session_id,
                )
                return True
            return False
        # HALF_OPEN: allow one attempt
        return True

    def record_compaction_success(self) -> None:
        """Record a successful compaction, resetting the circuit breaker.

        Transitions:
            CLOSED -> reset counters
            HALF_OPEN -> CLOSED
            OPEN -> CLOSED (utilization dropped below threshold)
        """
        self.total_successes += 1
        if self.state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
            logger.info(
                "Circuit breaker for session %s: %s -> CLOSED (success)",
                self.session_id,
                self._state_name(),
            )
            self.state = CircuitState.CLOSED
        self.consecutive_failures = 0

    def record_compaction_failure(self, now: float | None = None) -> None:
        """Record a compaction failure, potentially opening the circuit breaker.

        Increments the failure counter.  If failures >= max_consecutive_failures,
        transitions from CLOSED to OPEN.

        HALF_OPEN -> OPEN always (one failure opens it again).

        Args:
            now: Current timestamp (defaults to time.time()).
        """
        now = now if now is not None else time.time()
        self.consecutive_failures += 1
        self.last_failure_ts = now
        self.total_attempts += 1

        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            logger.error(
                "Circuit breaker for session %s: HALF_OPEN -> OPEN (failure during probe)",
                self.session_id,
            )
            return

        if self.consecutive_failures >= self.config.max_consecutive_failures:
            self.state = CircuitState.OPEN
            logger.error(
                "Circuit breaker for session %s: CLOSED -> OPEN (%d consecutive failures)",
                self.session_id,
                self.consecutive_failures,
            )

    def is_circuit_open(self) -> bool:
        """Return True if the circuit breaker is open (blocking compaction).

        Returns:
            True when compaction attempts are blocked.
        """
        return self.state == CircuitState.OPEN

    def reset_circuit(self) -> None:
        """Reset the circuit breaker to the CLOSED state.

        Clears consecutive failures and restores to initial state.
        """
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        logger.info(
            "Circuit breaker for session %s: reset to CLOSED",
            self.session_id,
        )

    def _state_name(self) -> str:
        """Return the name of the current circuit breaker state."""
        state_names = {
            CircuitState.CLOSED: "CLOSED",
            CircuitState.OPEN: "OPEN",
            CircuitState.HALF_OPEN: "HALF_OPEN",
        }
        return state_names.get(self.state, "UNKNOWN")
