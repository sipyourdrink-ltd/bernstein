"""Fast mode coordinator with cooldown on rate limit (T498).

Provides a state machine that controls whether agents operate in **fast**
(high-throughput, aggressive parallelism) or **cooldown** (throttled,
conservative) mode.

State transitions:
    - **fast → cooldown**: triggered when a rate limit (429 or 529) is hit.
    - **cooldown → fast**: triggered after the cooldown window expires and
      rate limits have cleared.

The router consults ``is_fast_mode()`` before selecting a model or tier,
ensuring traffic backs off during cooldown periods.

Usage:
    >>> coord = FastModeCoordinator()
    >>> coord.is_fast_mode()
    True
    >>> coord.enter_cooldown(retry_after_seconds=120)
    >>> coord.is_fast_mode()
    False
    # 120 seconds later...
    >>> coord.is_fast_mode()
    True
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State machine states
# ---------------------------------------------------------------------------


@dataclass
class CooldownState:
    """Cooldown window metadata.

    Attributes:
        started_at: Unix timestamp when cooldown began.
        retry_after_seconds: Provider-suggested wait time from Retry-After.
        strike_count: Number of rate-limit hits that triggered this cooldown.
    """

    started_at: float
    retry_after_seconds: float
    strike_count: int = 1


class FastModeCoordinator:
    """Fast mode / cooldown state machine (T498).

    Args:
        min_cooldown_seconds: Minimum time in cooldown before fast mode can
            resume.  Defaults to 60 seconds.
        max_cooldown_seconds: Maximum time in cooldown before forced recovery.
            Defaults to 600 seconds (10 minutes).
        strike_decay_seconds: Seconds between each strike that reduces the
            required cooldown duration.  Higher strike counts require longer
            cooldowns (up to max).  Defaults to 30.
    """

    def __init__(
        self,
        min_cooldown_seconds: float = 60.0,
        max_cooldown_seconds: float = 600.0,
        strike_decay_seconds: float = 30.0,
    ) -> None:
        self._fast_mode = True
        self._cooldown: CooldownState | None = None
        self._min_cooldown = min_cooldown_seconds
        self._max_cooldown = max_cooldown_seconds
        self._strike_decay = strike_decay_seconds
        self._transition_count = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_fast_mode(self, now: float | None = None) -> bool:
        """Return True if fast mode is currently active.

        If the coordinator is in cooldown, this checks whether the cooldown
        window has expired.  If expired, it automatically transitions back
        to fast mode.

        Args:
            now: Current Unix timestamp.  Defaults to ``time.time()``.

        Returns:
            True when fast mode is active.
        """
        if self._fast_mode:
            return True

        current = now if now is not None else time.time()
        cooldown = self._cooldown

        if cooldown is None:
            self._fast_mode = True
            return True

        elapsed = current - cooldown.started_at
        required = self._required_cooldown(cooldown)

        if elapsed >= required:
            self._exit_cooldown()
            return True

        return False

    def enter_cooldown(
        self,
        retry_after_seconds: float | None = None,
        strike_count: int = 1,
        now: float | None = None,
    ) -> None:
        """Enter cooldown mode after a rate-limit event.

        Args:
            retry_after_seconds: Retry-After header value (seconds).  If not
                provided, defaults to ``_min_cooldown``.
            strike_count: Consecutive rate-limit strikes.  Used to increase
                the required cooldown duration.
            now: Current Unix timestamp.  Defaults to ``time.time()``.
        """
        current = now if now is not None else time.time()
        actual_retry = retry_after_seconds if retry_after_seconds is not None else self._min_cooldown
        cumulative_strikes = strike_count

        self._fast_mode = False
        self._cooldown = CooldownState(
            started_at=current,
            retry_after_seconds=actual_retry,
            strike_count=cumulative_strikes,
        )
        self._transition_count += 1
        logger.warning(
            "Fast mode entered cooldown: retry_after=%.0fs strikes=%d",
            actual_retry,
            cumulative_strikes,
        )

    def force_fast_mode(self, now: float | None = None) -> None:
        """Manually exit cooldown and return to fast mode.

        Args:
            now: Current Unix timestamp for logging.
        """
        self._fast_mode = True
        self._cooldown = None
        self._transition_count += 1
        current = now if now is not None else time.time()
        logger.info("Fast mode manually restored at %.0f", current)

    @property
    def transition_count(self) -> int:
        """Number of state transitions (fast→cooldown or cooldown→fast)."""
        return self._transition_count

    @property
    def cooldown_remaining(self) -> float:
        """Seconds remaining until cooldown expires, or 0 if not in cooldown."""
        return self.cooldown_remaining_now()

    def cooldown_remaining_now(self, now: float | None = None) -> float:
        """Seconds remaining until cooldown expires at *now*.

        Args:
            now: Unix timestamp. Defaults to ``time.time()``.

        Returns:
            Seconds remaining, or 0.0 if not in cooldown.
        """
        if self._fast_mode or self._cooldown is None:
            return 0.0
        current = now if now is not None else time.time()
        elapsed = current - self._cooldown.started_at
        required = self._required_cooldown(self._cooldown)
        return max(0.0, required - elapsed)

    def cooldown_info(self) -> dict[str, Any] | None:
        """Return information about the current cooldown state.

        Returns:
            Dict with cooldown details, or None if not in cooldown.
        """
        if self._fast_mode or self._cooldown is None:
            return None
        info: dict[str, Any] = {
            "started_at": self._cooldown.started_at,
            "retry_after_seconds": self._cooldown.retry_after_seconds,
            "strike_count": self._cooldown.strike_count,
            "remaining_seconds": self.cooldown_remaining,
            "required_seconds": self._required_cooldown(self._cooldown),
        }
        return info

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _required_cooldown(self, cooldown: CooldownState) -> float:
        """Calculate the required cooldown duration based on strikes."""
        base = max(self._min_cooldown, cooldown.retry_after_seconds)
        # Increase required time with each strike, capped at max
        strike_penalty = (cooldown.strike_count - 1) * self._strike_decay
        return min(base + strike_penalty, self._max_cooldown)

    def _exit_cooldown(self) -> None:
        """Transition from cooldown back to fast mode."""
        self._fast_mode = True
        self._cooldown = None
        self._transition_count += 1
        logger.info("Fast mode cooldown expired — resuming fast mode")
