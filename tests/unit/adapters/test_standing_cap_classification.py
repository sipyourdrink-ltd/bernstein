"""Unit tests for standing cap 429 classification (#4378)."""

from __future__ import annotations

import pytest
from bernstein.core.models import Task

from bernstein.adapters.base import (
    RateLimitError,
    StandingCapError,
)
from bernstein.adapters.http_429_classifier import (
    HTTP429Classification,
    classify_429,
)
from bernstein.core.agents.spawn_analyzer import SpawnAnalyzer


def test_classify_standing_cap_detects_session_cap() -> None:
    body = (
        '429 {"message":"You have reached the maximum number of active sessions (48). '
        'Please close unused sessions or wait for them to expire.","type":"rate_limit_error"}'
    )
    assert classify_429(body) is HTTP429Classification.STANDING


@pytest.mark.parametrize(
    "body",
    [
        "daily spend limit reached for this account",
        "insufficient_quota: project quota exceeded",
        "credit balance is zero",
        "RESOURCE_EXHAUSTED: quota exceeded for 1m",
    ],
)
def test_classify_standing_cap_detects_various_caps(body: str) -> None:
    assert classify_429(body) is HTTP429Classification.STANDING


def test_unrecognized_429_falls_back_to_retryable_rate_limit() -> None:
    body = '429 {"message": "Too Many Requests", "type": "rate_limit_error"}'
    assert classify_429(body) is HTTP429Classification.RETRYABLE


def test_spawn_analyzer_classifies_standing_cap_as_non_transient() -> None:
    error = StandingCapError("Maximum active sessions reached", reason_code="session_cap_exceeded")
    task = Task(id="task-1", title="test", description="", role="backend")
    analyzer = SpawnAnalyzer()

    analysis = analyzer.analyze(error, task)

    assert analysis.is_transient is False
    assert analysis.error_type == "session_cap_exceeded"
    assert analysis.recommended_action == "abort"


def test_spawn_analyzer_keeps_ordinary_rate_limit_as_transient() -> None:
    error = RateLimitError("Rate limit exceeded")
    task = Task(id="task-1", title="test", description="", role="backend")
    analyzer = SpawnAnalyzer()

    analysis = analyzer.analyze(error, task)

    assert analysis.is_transient is True
    assert analysis.error_type == "rate_limit"
    assert analysis.recommended_action == "wait"
