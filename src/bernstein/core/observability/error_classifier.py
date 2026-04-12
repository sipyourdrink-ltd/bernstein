"""Classify spawner exceptions into recoverable vs fatal categories.

Provides a ``FailureCategory`` enum and an ``ErrorClassifier`` that maps
exception types (and their messages) to one of four categories:

- **transient** — temporary failures that will likely resolve on retry
  (network glitches, rate limits, 503s).
- **permanent** — the task or configuration is fundamentally broken and
  retrying is pointless (bad prompt, invalid model name).
- **resource_exhaustion** — the provider or local machine ran out of a
  finite resource (token budget, disk, OOM).
- **config_error** — a misconfiguration that requires human intervention
  (missing API key, wrong endpoint URL).

The spawner uses the classification to decide whether to retry, backoff,
or fail the task permanently.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class FailureCategory(StrEnum):
    """Category of a spawner or HTTP failure."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    CONFIG_ERROR = "config_error"


@dataclass(frozen=True)
class ClassifiedError:
    """An exception classified into a failure category.

    Attributes:
        category: The failure category.
        original: The original exception.
        reason: Human-readable explanation of the classification.
        retryable: Whether the caller should attempt a retry.
    """

    category: FailureCategory
    original: Exception
    reason: str
    retryable: bool


# ---------------------------------------------------------------------------
# Pattern-based classification rules
# ---------------------------------------------------------------------------

_TRANSIENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"429", re.IGNORECASE),
    re.compile(r"503", re.IGNORECASE),
    re.compile(r"502", re.IGNORECASE),
    re.compile(r"504", re.IGNORECASE),
    re.compile(r"529", re.IGNORECASE),
    re.compile(r"overloaded", re.IGNORECASE),
    re.compile(r"temporarily\s+unavailable", re.IGNORECASE),
    re.compile(r"connection\s+(reset|refused|timed?\s*out)", re.IGNORECASE),
    re.compile(r"timeout", re.IGNORECASE),
    re.compile(r"ECONNRESET", re.IGNORECASE),
    re.compile(r"network\s+(error|unreachable)", re.IGNORECASE),
]

_RESOURCE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"out\s+of\s+memory", re.IGNORECASE),
    re.compile(r"OOM", re.IGNORECASE),
    re.compile(r"token\s+(budget|limit)\s+exceeded", re.IGNORECASE),
    re.compile(r"context\s+(length|window)\s+exceeded", re.IGNORECASE),
    re.compile(r"max.?tokens", re.IGNORECASE),
    re.compile(r"disk\s+(full|space)", re.IGNORECASE),
    re.compile(r"quota\s+exceeded", re.IGNORECASE),
    re.compile(r"413", re.IGNORECASE),
]

_CONFIG_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(api.?key|auth.*token)\s*(missing|invalid|expired)", re.IGNORECASE),
    re.compile(r"401", re.IGNORECASE),
    re.compile(r"403", re.IGNORECASE),
    re.compile(r"invalid\s+model", re.IGNORECASE),
    re.compile(r"model\s+not\s+found", re.IGNORECASE),
    re.compile(r"permission\s+denied", re.IGNORECASE),
    re.compile(r"authentication\s+failed", re.IGNORECASE),
    re.compile(r"not\s+authorized", re.IGNORECASE),
]

_PERMANENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"400", re.IGNORECASE),
    re.compile(r"invalid\s+(request|prompt|input)", re.IGNORECASE),
    re.compile(r"malformed", re.IGNORECASE),
    re.compile(r"unsupported", re.IGNORECASE),
]


class ErrorClassifier:
    """Classify exceptions into failure categories.

    The classifier checks the exception type first, then falls back to
    regex matching on the string representation of the exception message.

    Usage::

        classifier = ErrorClassifier()
        result = classifier.classify(some_exception)
        if result.retryable:
            # schedule retry with backoff
        else:
            # fail the task permanently
    """

    def classify(self, exc: Exception) -> ClassifiedError:
        """Classify an exception into a failure category.

        Args:
            exc: The exception to classify.

        Returns:
            A ClassifiedError with the category and retry recommendation.
        """
        # 1. Check by exception type first
        classified = self._classify_by_type(exc)
        if classified is not None:
            return classified

        # 2. Fall back to message pattern matching
        msg = str(exc)
        classified = self._classify_by_message(exc, msg)
        if classified is not None:
            return classified

        # 3. Check chained exceptions
        cause = exc.__cause__ or exc.__context__
        if cause is not None and isinstance(cause, Exception):
            classified = self._classify_by_message(exc, str(cause))
            if classified is not None:
                return classified

        # 4. Default: treat unknown exceptions as transient (safe default
        #    that allows a retry before giving up)
        return ClassifiedError(
            category=FailureCategory.TRANSIENT,
            original=exc,
            reason=f"Unclassified exception: {type(exc).__name__}: {exc}",
            retryable=True,
        )

    def _classify_by_type(self, exc: Exception) -> ClassifiedError | None:
        """Classify based on exception type hierarchy.

        Args:
            exc: The exception to classify.

        Returns:
            ClassifiedError if type matches, None otherwise.
        """
        # Import adapter exceptions lazily to avoid circular imports
        from bernstein.adapters.base import RateLimitError, SpawnError

        if isinstance(exc, RateLimitError):
            return ClassifiedError(
                category=FailureCategory.TRANSIENT,
                original=exc,
                reason=f"Rate limit error: {exc}",
                retryable=True,
            )

        if isinstance(exc, SpawnError):
            # SpawnError is the base — could be permanent or transient
            # depending on message content; fall through to pattern match
            pass

        if isinstance(exc, (ConnectionError, TimeoutError)):
            return ClassifiedError(
                category=FailureCategory.TRANSIENT,
                original=exc,
                reason=f"Network error: {type(exc).__name__}: {exc}",
                retryable=True,
            )

        if isinstance(exc, PermissionError):
            return ClassifiedError(
                category=FailureCategory.CONFIG_ERROR,
                original=exc,
                reason=f"Permission error: {exc}",
                retryable=False,
            )

        if isinstance(exc, MemoryError):
            return ClassifiedError(
                category=FailureCategory.RESOURCE_EXHAUSTION,
                original=exc,
                reason="Out of memory",
                retryable=False,
            )

        if isinstance(exc, (FileNotFoundError, ModuleNotFoundError)):
            return ClassifiedError(
                category=FailureCategory.CONFIG_ERROR,
                original=exc,
                reason=f"Missing dependency or file: {exc}",
                retryable=False,
            )

        return None

    def _classify_by_message(
        self,
        exc: Exception,
        msg: str,
    ) -> ClassifiedError | None:
        """Classify based on regex patterns against the exception message.

        Args:
            exc: The original exception.
            msg: The message string to match against.

        Returns:
            ClassifiedError if a pattern matches, None otherwise.
        """
        # Order matters: config_error before transient so "401" isn't
        # treated as a transient failure.
        for pattern in _CONFIG_PATTERNS:
            if pattern.search(msg):
                return ClassifiedError(
                    category=FailureCategory.CONFIG_ERROR,
                    original=exc,
                    reason=f"Config error (matched {pattern.pattern!r}): {msg}",
                    retryable=False,
                )

        for pattern in _RESOURCE_PATTERNS:
            if pattern.search(msg):
                return ClassifiedError(
                    category=FailureCategory.RESOURCE_EXHAUSTION,
                    original=exc,
                    reason=f"Resource exhaustion (matched {pattern.pattern!r}): {msg}",
                    retryable=False,
                )

        for pattern in _PERMANENT_PATTERNS:
            if pattern.search(msg):
                return ClassifiedError(
                    category=FailureCategory.PERMANENT,
                    original=exc,
                    reason=f"Permanent failure (matched {pattern.pattern!r}): {msg}",
                    retryable=False,
                )

        for pattern in _TRANSIENT_PATTERNS:
            if pattern.search(msg):
                return ClassifiedError(
                    category=FailureCategory.TRANSIENT,
                    original=exc,
                    reason=f"Transient failure (matched {pattern.pattern!r}): {msg}",
                    retryable=True,
                )

        return None
