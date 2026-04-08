"""Model fallback tracker — consecutive provider errors trigger model switch (T444, AGENT-004).

After consecutive error responses (529, 429, 503, timeouts) from a provider,
the tracker signals that the agent should switch to a configured fallback
model.  Counters are scoped per session to avoid cross-talk between agents.

AGENT-004 extends the original 529-only tracker to handle all common error
types with a configurable fallback chain.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Default number of consecutive errors before fallback is triggered.
DEFAULT_529_STRIKE_LIMIT: int = 3

#: HTTP status codes that count toward fallback strikes.
DEFAULT_FALLBACK_STATUS_CODES: frozenset[int] = frozenset({429, 503, 529})

#: Sentinel for timeout errors (not a real HTTP code).
TIMEOUT_STATUS_CODE: int = 0

#: Sentinel status code used when a "model not available" error is detected
#: in the response body (not via HTTP status code).
MODEL_UNAVAILABLE_STATUS_CODE: int = -1

#: Regex patterns that indicate a "model not available" API error.
#: Matched case-insensitively against the response error text/body.
_MODEL_UNAVAILABLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"model.{0,20}not.{0,10}(available|found|exist)", re.IGNORECASE),
    re.compile(r"(invalid|unknown|unsupported).{0,10}model", re.IGNORECASE),
    re.compile(r"model.{0,10}(does not exist|unavailable)", re.IGNORECASE),
    re.compile(r"no such model", re.IGNORECASE),
]


def is_model_unavailable_error(text: str) -> bool:
    """Check whether an error message indicates a "model not available" failure.

    Matches common API error patterns from Anthropic, OpenAI, and compatible
    providers.  Used to trigger model fallback on 400/404 responses whose body
    indicates the specific model is unavailable rather than a request problem.

    Args:
        text: Error text or JSON body from the provider response.

    Returns:
        True if the text matches a known "model not available" pattern.
    """
    return any(pat.search(text) for pat in _MODEL_UNAVAILABLE_PATTERNS)


@dataclass
class FallbackChainConfig:
    """Configuration for a model fallback chain (AGENT-004).

    Defines which status codes trigger fallback, the strike limit, and
    an ordered list of fallback models to try in sequence.

    Attributes:
        trigger_codes: HTTP status codes that count as strikes.
        include_timeouts: Whether timeout errors (status_code=0) count.
        strike_limit: Consecutive errors before fallback triggers.
        fallback_chain: Ordered list of fallback models.  The tracker
            advances through this list on each successive fallback.
    """

    trigger_codes: frozenset[int] = DEFAULT_FALLBACK_STATUS_CODES
    include_timeouts: bool = True
    strike_limit: int = DEFAULT_529_STRIKE_LIMIT
    fallback_chain: list[str] = field(default_factory=list[str])


@dataclass
class FallbackState:
    """Per-session fallback tracking state.

    Attributes:
        consecutive_529_errors: Number of consecutive fallback-triggering errors.
        fallback_model: Model to switch to when strike limit is reached.
        is_fallback: Whether the session is currently in fallback mode.
        total_529_count: Total fallback-triggering errors ever seen.
        fallback_chain: Ordered list of fallback models for this session.
        fallback_chain_index: Current position in the fallback chain.
    """

    consecutive_529_errors: int = 0
    fallback_model: str | None = None
    is_fallback: bool = False
    total_529_count: int = 0
    fallback_chain: list[str] = field(default_factory=list[str])
    fallback_chain_index: int = 0


@dataclass
class FallbackResult:
    """Result of recording an HTTP response for fallback checking.

    Attributes:
        should_fallback: True when the session should switch to fallback model.
        strike_count: Current consecutive error count.
        strike_limit: Threshold at which fallback triggers.
        status_code: Raw HTTP status code that was recorded.
        error_type: Human-readable error type (e.g. "rate_limit", "overloaded").
    """

    should_fallback: bool
    strike_count: int
    strike_limit: int
    status_code: int
    error_type: str = ""


def _classify_error_type(status_code: int) -> str:
    """Classify an HTTP status code into a human-readable error type.

    Args:
        status_code: HTTP status code, TIMEOUT_STATUS_CODE (0), or
            MODEL_UNAVAILABLE_STATUS_CODE (-1).

    Returns:
        Human-readable error type string.
    """
    if status_code == MODEL_UNAVAILABLE_STATUS_CODE:
        return "model_unavailable"
    if status_code == 0:
        return "timeout"
    if status_code == 429:
        return "rate_limit"
    if status_code == 503:
        return "service_unavailable"
    if status_code == 529:
        return "overloaded"
    return f"http_{status_code}"


class ModelFallbackTracker:
    """Track consecutive provider errors per session and signal fallback (T444, AGENT-004).

    When a session hits the configured number of consecutive error responses
    (429, 503, 529, or timeouts), ``record_response()`` returns a
    ``FallbackResult`` with ``should_fallback=True``.  The spawner should
    use the session's fallback model instead of the primary model.

    The fallback chain is configurable: when the first fallback model also
    fails, the tracker advances to the next model in the chain.

    A successful (non-error) response resets the consecutive counter.
    Manually calling ``reset()`` also resets the counter and clears fallback
    mode.

    Args:
        strike_limit: Number of consecutive errors before fallback triggers.
            Defaults to 3.
        chain_config: Optional configuration for which status codes trigger
            fallback and the default fallback chain.
    """

    def __init__(
        self,
        strike_limit: int = DEFAULT_529_STRIKE_LIMIT,
        chain_config: FallbackChainConfig | None = None,
    ) -> None:
        self._strike_limit = strike_limit
        self._sessions: dict[str, FallbackState] = {}
        self._chain_config = chain_config or FallbackChainConfig(strike_limit=strike_limit)
        self._trigger_codes = self._chain_config.trigger_codes
        self._include_timeouts = self._chain_config.include_timeouts

    @property
    def trigger_codes(self) -> frozenset[int]:
        """HTTP status codes that count toward fallback strikes."""
        return self._trigger_codes

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_session(
        self,
        session_id: str,
        fallback_model: str | None = None,
        fallback_chain: list[str] | None = None,
    ) -> None:
        """Register or update tracking state for a session.

        Args:
            session_id: Agent session identifier.
            fallback_model: Optional fallback model to use on strike limit.
            fallback_chain: Optional ordered list of fallback models.  When
                provided, overrides fallback_model — the first entry becomes
                the initial fallback target.
        """
        chain = list(fallback_chain) if fallback_chain else list(self._chain_config.fallback_chain)
        effective_fallback = fallback_model
        if chain and not effective_fallback:
            effective_fallback = chain[0]
        state = FallbackState(
            fallback_model=effective_fallback,
            fallback_chain=chain,
        )
        self._sessions[session_id] = state

    def session_exists(self, session_id: str) -> bool:
        """Check if a session has been registered."""
        return session_id in self._sessions

    def _is_trigger_code(self, status_code: int) -> bool:
        """Return True if the status code should count as a fallback strike.

        Args:
            status_code: HTTP status code, TIMEOUT_STATUS_CODE (0) for
                timeouts, or MODEL_UNAVAILABLE_STATUS_CODE (-1) for model
                not available errors.

        Returns:
            True if this code triggers strike counting.
        """
        if status_code == TIMEOUT_STATUS_CODE and self._include_timeouts:
            return True
        if status_code == MODEL_UNAVAILABLE_STATUS_CODE:
            return True
        return status_code in self._trigger_codes

    def record_model_unavailable(self, session_id: str) -> FallbackResult:
        """Record a "model not available" error for fallback tracking.

        Use when the provider returns a response indicating the requested
        model does not exist or is unavailable (typically detected by parsing
        the response body rather than the HTTP status code).

        Args:
            session_id: Agent session identifier.

        Returns:
            FallbackResult with decision on whether to fallback.
        """
        return self.record_response(session_id, MODEL_UNAVAILABLE_STATUS_CODE)

    def record_response(self, session_id: str, status_code: int) -> FallbackResult:
        """Record an HTTP response for a session's fallback tracking.

        Status codes in the trigger set (429, 503, 529, and optionally
        timeouts via status_code=0) increment the consecutive counter.
        Any other status code resets the counter.

        Args:
            session_id: Agent session identifier.
            status_code: HTTP status code from the provider response.
                Use ``TIMEOUT_STATUS_CODE`` (0) for timeout errors.

        Returns:
            FallbackResult with decision on whether to fallback.
        """
        if session_id not in self._sessions:
            self.ensure_session(session_id)

        state = self._sessions[session_id]

        if self._is_trigger_code(status_code):
            state.consecutive_529_errors += 1
            state.total_529_count += 1
        else:
            # Any non-error response resets the counter
            state.consecutive_529_errors = 0
            if state.is_fallback and 200 <= status_code < 300:
                state.is_fallback = False

        return FallbackResult(
            should_fallback=(not state.is_fallback and state.consecutive_529_errors >= self._strike_limit),
            strike_count=state.consecutive_529_errors,
            strike_limit=self._strike_limit,
            status_code=status_code,
            error_type=_classify_error_type(status_code) if self._is_trigger_code(status_code) else "",
        )

    def activate_fallback(self, session_id: str) -> str | None:
        """Mark a session as being in fallback mode.

        After calling this, ``get_active_model()`` will return the fallback
        model instead of the primary.  If a fallback chain is configured,
        advances to the next model in the chain on each successive activation.

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

        # Advance the fallback chain if one is configured
        if state.fallback_chain and state.fallback_chain_index < len(state.fallback_chain):
            state.fallback_model = state.fallback_chain[state.fallback_chain_index]
            state.fallback_chain_index += 1

        if state.fallback_model:
            logger.warning(
                "Session %s activated fallback mode: %s (after %d consecutive errors, chain pos %d/%d)",
                session_id,
                state.fallback_model,
                state.total_529_count,
                state.fallback_chain_index,
                len(state.fallback_chain),
            )
        return state.fallback_model

    def has_more_fallbacks(self, session_id: str) -> bool:
        """Check if the session has more models in its fallback chain.

        Args:
            session_id: Agent session identifier.

        Returns:
            True when there are more fallback models available.
        """
        state = self._sessions.get(session_id)
        if state is None:
            return False
        return state.fallback_chain_index < len(state.fallback_chain)

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


def initialize_fallback_tracker(
    fallback_chain: list[str] | None = None,
    strike_limit: int = DEFAULT_529_STRIKE_LIMIT,
    include_timeouts: bool = True,
    trigger_codes: frozenset[int] | None = None,
) -> ModelFallbackTracker:
    """Configure and return the process-global fallback tracker singleton.

    Call this once at startup after loading ``bernstein.yaml`` to wire the
    configured fallback chain into the global tracker.  Subsequent calls to
    :func:`get_fallback_tracker` will return the configured instance.

    Args:
        fallback_chain: Ordered list of fallback model names.
        strike_limit: Consecutive errors before triggering fallback.
        include_timeouts: Whether timeouts count toward the strike limit.
        trigger_codes: HTTP status codes that trigger strike counting.
            Defaults to the module-level ``DEFAULT_FALLBACK_STATUS_CODES``.

    Returns:
        The newly configured global ``ModelFallbackTracker``.
    """
    global _tracker
    chain_config = FallbackChainConfig(
        trigger_codes=trigger_codes if trigger_codes is not None else DEFAULT_FALLBACK_STATUS_CODES,
        include_timeouts=include_timeouts,
        strike_limit=strike_limit,
        fallback_chain=list(fallback_chain) if fallback_chain else [],
    )
    _tracker = ModelFallbackTracker(strike_limit=strike_limit, chain_config=chain_config)
    logger.info(
        "Fallback tracker initialized: strike_limit=%d, chain=%s, codes=%s",
        strike_limit,
        fallback_chain,
        sorted(chain_config.trigger_codes),
    )
    return _tracker
