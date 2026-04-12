"""Token binding and replay prevention for API authentication.

Provides token expiration checks, run_id binding, and per-token rate
limiting to prevent token replay attacks.

Usage::

    validator = TokenBindingValidator(run_id="run-123")
    result = validator.validate(token_claims, client_ip="127.0.0.1")
    if not result.valid:
        return 403, result.reason
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Final

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_MAX_AGE_S: Final[int] = 86400  # 24 hours
DEFAULT_RATE_LIMIT_PER_MINUTE: Final[int] = 120
DEFAULT_RATE_LIMIT_WINDOW_S: Final[int] = 60


@dataclass(frozen=True)
class TokenValidationResult:
    """Result of token binding validation.

    Attributes:
        valid: Whether the token passed all binding checks.
        reason: Human-readable reason for rejection (empty if valid).
    """

    valid: bool
    reason: str = ""


@dataclass
class _TokenRateEntry:
    """Internal rate tracking for a single token.

    Attributes:
        token_jti: JWT ID (jti claim) for the token.
        request_timestamps: Timestamps of recent requests.
    """

    token_jti: str
    request_timestamps: list[float] = field(default_factory=list[float])


class TokenBindingValidator:
    """Validate tokens against binding constraints and rate limits.

    Checks:
    1. Token expiration (exp claim).
    2. Token-to-run binding (run_id in claims must match current run).
    3. Per-token rate limiting (sliding window).

    Args:
        run_id: The current orchestrator run ID. Tokens must carry this
            in their ``run_id`` claim to be accepted.
        max_age_s: Maximum token age in seconds.
        rate_limit_per_minute: Maximum requests per token per minute.
        enforce_run_binding: Whether to require run_id binding (can be
            disabled for legacy tokens).
    """

    def __init__(
        self,
        run_id: str = "",
        max_age_s: int = DEFAULT_TOKEN_MAX_AGE_S,
        rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        enforce_run_binding: bool = True,
    ) -> None:
        self._run_id = run_id
        self._max_age_s = max_age_s
        self._rate_limit = rate_limit_per_minute
        self._enforce_run_binding = enforce_run_binding
        self._rate_entries: dict[str, _TokenRateEntry] = {}

    @property
    def run_id(self) -> str:
        """The current run ID that tokens must be bound to."""
        return self._run_id

    def validate(
        self,
        claims: dict[str, Any],
        client_ip: str = "",
    ) -> TokenValidationResult:
        """Validate token claims against all binding constraints.

        Args:
            claims: Decoded JWT claims dict. Expected keys: ``exp``, ``iat``,
                ``jti``, ``run_id``.
            client_ip: Client IP address (logged for audit, not enforced).

        Returns:
            TokenValidationResult indicating pass/fail.
        """
        # Check 1: Expiration
        exp = claims.get("exp")
        if exp is not None:
            try:
                exp_float = float(exp)
            except (TypeError, ValueError):
                return TokenValidationResult(
                    valid=False,
                    reason="Token has invalid exp claim",
                )
            if time.time() > exp_float:
                return TokenValidationResult(
                    valid=False,
                    reason="Token has expired",
                )

        # Check 2: Maximum age (iat-based)
        iat = claims.get("iat")
        if iat is not None:
            try:
                iat_float = float(iat)
            except (TypeError, ValueError):
                return TokenValidationResult(
                    valid=False,
                    reason="Token has invalid iat claim",
                )
            age = time.time() - iat_float
            if age > self._max_age_s:
                return TokenValidationResult(
                    valid=False,
                    reason=f"Token exceeds maximum age ({int(age)}s > {self._max_age_s}s)",
                )

        # Check 3: Run ID binding
        if self._enforce_run_binding and self._run_id:
            token_run_id = claims.get("run_id", "")
            if token_run_id != self._run_id:
                logger.warning(
                    "Token run_id mismatch: expected %r, got %r (client=%s)",
                    self._run_id,
                    token_run_id,
                    client_ip,
                )
                return TokenValidationResult(
                    valid=False,
                    reason=f"Token bound to run_id={token_run_id!r}, current run is {self._run_id!r}",
                )

        # Check 4: Per-token rate limit
        jti = str(claims.get("jti", ""))
        if jti:
            rate_result = self._check_rate_limit(jti)
            if not rate_result.valid:
                logger.warning(
                    "Rate limit exceeded for token jti=%s (client=%s)",
                    jti,
                    client_ip,
                )
                return rate_result

        return TokenValidationResult(valid=True)

    def _check_rate_limit(self, jti: str) -> TokenValidationResult:
        """Check and update the rate limit for a token.

        Args:
            jti: JWT ID to track.

        Returns:
            TokenValidationResult indicating pass/fail.
        """
        now = time.time()
        window_start = now - DEFAULT_RATE_LIMIT_WINDOW_S

        entry = self._rate_entries.get(jti)
        if entry is None:
            entry = _TokenRateEntry(token_jti=jti)
            self._rate_entries[jti] = entry

        # Prune old timestamps outside the window
        entry.request_timestamps = [ts for ts in entry.request_timestamps if ts > window_start]

        if len(entry.request_timestamps) >= self._rate_limit:
            count = len(entry.request_timestamps)
            return TokenValidationResult(
                valid=False,
                reason=(
                    f"Rate limit exceeded: {count} requests "
                    f"in last {DEFAULT_RATE_LIMIT_WINDOW_S}s "
                    f"(limit={self._rate_limit})"
                ),
            )

        entry.request_timestamps.append(now)
        return TokenValidationResult(valid=True)

    def cleanup_stale_entries(self, max_age_s: int = 300) -> int:
        """Remove rate-limit entries for tokens that have been idle.

        Args:
            max_age_s: Remove entries with no requests in this many seconds.

        Returns:
            Number of entries removed.
        """
        now = time.time()
        stale_jtis: list[str] = []
        for jti, entry in self._rate_entries.items():
            if not entry.request_timestamps or now - entry.request_timestamps[-1] > max_age_s:
                stale_jtis.append(jti)

        for jti in stale_jtis:
            del self._rate_entries[jti]

        return len(stale_jtis)
