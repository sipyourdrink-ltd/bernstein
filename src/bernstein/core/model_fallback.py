"""Model fallback tracker — three consecutive HTTP 529 errors trigger model switch (T444).

After three consecutive HTTP 529 (overloaded) responses from a provider,
the tracker signals that the agent should switch to a configured fallback
model.  Counters are scoped per session to avoid cross-talk between agents.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Default number of consecutive 529 errors before fallback is triggered.
DEFAULT_529_STRIKE_LIMIT: int = 3


@dataclass
class FallbackState:
    """Per-session fallback tracking state.

    Attributes:
        consecutive_529_errors: Number of consecutive 529 errors observed.
        fallback_model: Model to switch to when strike limit is reached.
        is_fallback: Whether the session is currently in fallback mode.
        total_529_count: Total 529 errors ever seen for this session.
    """

    consecutive_529_errors: int = 0
    fallback_model: str | None = None
    is_fallback: bool = False
    total_529_count: int = 0


@dataclass
class FallbackResult:
    """Result of recording an HTTP response for fallback checking.

    Attributes:
        should_fallback: True when the session should switch to fallback model.
        strike_count: Current consecutive 529 error count.
        strike_limit: Threshold at which fallback triggers.
        status_code: Raw HTTP status code that was recorded.
    """

    should_fallback: bool
    strike_count: int
    strike_limit: int
    status_code: int


class ModelFallbackTracker:
    """Track consecutive 529 errors per session and signal fallback (T444).

    When a session hits the configured number of consecutive 529 (overloaded)
    responses, ``record_response()`` returns a ``FallbackResult`` with
    ``should_fallback=True``.  The spawner should use the session's
    ``fallback_model`` instead of the primary model.

    A successful (non-529) response resets the consecutive counter.
    Manually calling ``reset()`` also resets the counter and clears fallback
    mode.

    Args:
        strike_limit: Number of consecutive 529s before fallback triggers.
            Defaults to 3.
    """

    def __init__(self, strike_limit: int = DEFAULT_529_STRIKE_LIMIT) -> None:
        self._strike_limit = strike_limit
        self._sessions: dict[str, FallbackState] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_session(
        self,
        session_id: str,
        fallback_model: str | None = None,
    ) -> None:
        """Register or update tracking state for a session.

        Args:
            session_id: Agent session identifier.
            fallback_model: Optional fallback model to use on strike limit.
        """
        state = FallbackState(fallback_model=fallback_model)
        self._sessions[session_id] = state

    def session_exists(self, session_id: str) -> bool:
        """Check if a session has been registered."""
        return session_id in self._sessions

    def record_response(self, session_id: str, status_code: int) -> FallbackResult:
        """Record an HTTP response for a session's fallback tracking.

        A 529 increments the consecutive error counter. Any other status
        code resets the consecutive counter to zero.

        Args:
            session_id: Agent session identifier.
            status_code: HTTP status code from the provider response.

        Returns:
            FallbackResult with decision on whether to fallback.
        """
        if session_id not in self._sessions:
            self.ensure_session(session_id)

        state = self._sessions[session_id]

        if status_code == 529:
            state.consecutive_529_errors += 1
            state.total_529_count += 1
        else:
            # Any non-529 response resets the counter
            state.consecutive_529_errors = 0
            if state.is_fallback and status_code >= 200 and status_code < 300:
                state.is_fallback = False

        return FallbackResult(
            should_fallback=(not state.is_fallback and state.consecutive_529_errors >= self._strike_limit),
            strike_count=state.consecutive_529_errors,
            strike_limit=self._strike_limit,
            status_code=status_code,
        )

    def activate_fallback(self, session_id: str) -> str | None:
        """Mark a session as being in fallback mode.

        After calling this, ``get_active_model()`` will return the fallback
        model instead of the primary.

        Args:
            session_id: Agent session identifier.

        Returns:
            The fallback model name, or None if not configured.
        """
        state = self._sessions.get(session_id)
        if state is None:
            return None
        state.is_fallback = True
        state.consecutive_529_errors = 0
        if state.fallback_model:
            logger.warning(
                "Session %s activated fallback mode: %s (after %d consecutive 529s)",
                session_id,
                state.fallback_model,
                state.total_529_count,
            )
        return state.fallback_model

    def get_active_model(self, session_id: str, primary: str) -> str:
        """Return the model to use for a session (fallback if active).

        Args:
            session_id: Agent session identifier.
            primary: Primary model name for the session.

        Returns:
            Fallback model if active, otherwise the primary model.
        """
        state = self._sessions.get(session_id)
        if state and state.is_fallback and state.fallback_model:
            return state.fallback_model
        return primary

    def reset(self, session_id: str) -> None:
        """Reset consecutive error counter and clear fallback mode.

        Args:
            session_id: Agent session identifier.
        """
        state = self._sessions.get(session_id)
        if state:
            state.consecutive_529_errors = 0
            state.is_fallback = False

    def remove_session(self, session_id: str) -> None:
        """Remove tracking state for a session.

        Args:
            session_id: Agent session identifier.
        """
        self._sessions.pop(session_id, None)

    def get_strike_count(self, session_id: str) -> int:
        """Return the current consecutive 529 strike count for a session.

        Args:
            session_id: Agent session identifier.

        Returns:
            Current consecutive strike count, zero if session unknown.
        """
        return self._sessions.get(session_id, FallbackState()).consecutive_529_errors

    def is_fallback_active(self, session_id: str) -> bool:
        """Check if a session is in fallback mode.

        Args:
            session_id: Agent session identifier.

        Returns:
            True when fallback mode is active.
        """
        return self._sessions.get(session_id, FallbackState()).is_fallback


# ---------------------------------------------------------------------------
# Module-level singleton (lazily created)
# ---------------------------------------------------------------------------

_tracker: ModelFallbackTracker | None = None


def get_fallback_tracker() -> ModelFallbackTracker:
    """Return the process-global ``ModelFallbackTracker`` singleton."""
    global _tracker
    if _tracker is None:
        _tracker = ModelFallbackTracker()
    return _tracker


def reset_fallback_tracker() -> None:
    """Reset the global tracker (test helper)."""
    global _tracker
    _tracker = None
