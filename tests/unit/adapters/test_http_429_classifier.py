"""Unit tests for :mod:`bernstein.adapters.http_429_classifier`.

Covers the three classification cases: rate-limit-shaped bodies,
cap-shaped bodies, and unknown bodies.
"""

from __future__ import annotations

from bernstein.adapters.http_429_classifier import (
    HTTP429Classification,
    classify_429,
)


def test_rate_limit_body_classified_as_retryable() -> None:
    """A rate-limit-shaped body maps to RETRYABLE."""

    result = classify_429("too many requests, slow down")
    assert result is HTTP429Classification.RETRYABLE


def test_rate_limit_phrase_classified_as_retryable() -> None:
    """The literal ``rate limit`` phrase maps to RETRYABLE."""

    result = classify_429("rate limit exceeded")
    assert result is HTTP429Classification.RETRYABLE


def test_session_cap_body_classified_as_standing() -> None:
    """``maximum number of active sessions`` maps to STANDING."""

    result = classify_429("maximum number of active sessions reached")
    assert result is HTTP429Classification.STANDING


def test_close_unused_sessions_classified_as_standing() -> None:
    """``close unused sessions`` maps to STANDING."""

    result = classify_429("please close unused sessions and retry")
    assert result is HTTP429Classification.STANDING


def test_spend_cap_body_classified_as_standing() -> None:
    """Spend-cap wording maps to STANDING."""

    for body in ("spending limit exceeded", "daily limit reached", "budget exceeded"):
        assert classify_429(body) is HTTP429Classification.STANDING


def test_unknown_body_classified_as_retryable() -> None:
    """An unrecognised body falls back to RETRYABLE."""

    result = classify_429("some opaque provider error")
    assert result is HTTP429Classification.RETRYABLE


def test_matching_is_case_insensitive() -> None:
    """Body matching is case-insensitive."""

    result = classify_429("MAXIMUM NUMBER OF ACTIVE SESSIONS")
    assert result is HTTP429Classification.STANDING


def test_retry_after_parameter_is_accepted() -> None:
    """The ``retry_after`` parameter is accepted without changing the result."""

    result = classify_429("rate limit exceeded", retry_after="30")
    assert result is HTTP429Classification.RETRYABLE
