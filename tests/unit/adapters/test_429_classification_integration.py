"""End-to-end integration tests for 429 classification (issue #4378).

These tests prove the three acceptance scenarios work through the full
adapter spawn-probe path:

1. A retryable 429 body still backs off exponentially: the meter records
   a hit and ``RateLimitError`` is raised.
2. A standing-cap 429 body short-circuits: ``StandingCapError`` is raised
   instead of ``RateLimitError``, no meter hit is recorded, and the
   exception classifies to ``reason_code=standing_cap`` with
   ``transient=False``.
3. An unrecognised 429 body falls back to retryable behaviour: meter hit
   recorded, ``RateLimitError`` raised, exponential backoff.

The adapter spawn path is simulated with a fake subprocess and a log file
so ``_read_last_lines`` picks up the 429 body text exactly as it would in
production.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bernstein.adapters.base import (
    CLIAdapter,
    RateLimitError,
    SpawnResult,
    StandingCapError,
    reset_rate_limit_meters,
    set_rate_limit_emit_callback,
)
from bernstein.adapters.http_429_classifier import (
    HTTP429Classification,
    classify_429,
)
from bernstein.core.orchestration.failure_taxonomy import classify_failure

#: The issue #4378 example body: a standing session cap.
STANDING_CAP_BODY = (
    "HTTP 429: You have reached the maximum number of active sessions (48). "
    "Please close unused sessions or wait for them to expire."
)

#: A retryable rate-limit body.
RETRYABLE_BODY = "HTTP 429: too many requests, rate limit exceeded"

#: An unrecognised 429 body with no recognisable message.
UNKNOWN_BODY = "HTTP 429"


@pytest.fixture(autouse=True)
def _clean_meters() -> Any:
    """Reset the process-local meter registry around every test."""
    reset_rate_limit_meters()
    set_rate_limit_emit_callback(None)
    yield
    reset_rate_limit_meters()
    set_rate_limit_emit_callback(None)


class _ProbeAdapter(CLIAdapter):
    """Minimal adapter exposing the base fast-exit probe for testing."""

    def name(self) -> str:
        return "probe"

    def spawn(self, **kwargs: Any) -> SpawnResult:  # pragma: no cover - unused
        raise NotImplementedError


class _FakeProc:
    """WaitableProcess stand-in that exits with a fixed code."""

    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code

    def wait(self, timeout: float | None = None) -> int:
        return self._exit_code


def _write_log(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _probe_once(adapter: _ProbeAdapter, log_path: Path, body: str) -> None:
    """Run the fast-exit probe once against a log file containing ``body``."""
    _write_log(log_path, body)
    adapter._probe_fast_exit(_FakeProc(1), log_path, provider_name="probe")


# ---------------------------------------------------------------------------
# Scenario 1: retryable 429 still backs off exponentially
# ---------------------------------------------------------------------------


def test_classify_429_retryable_body_is_retryable() -> None:
    """The retryable body maps to RETRYABLE at the classifier level."""

    assert classify_429(RETRYABLE_BODY) is HTTP429Classification.RETRYABLE


def test_retryable_429_records_hit_and_raises_rate_limit(tmp_path: Path) -> None:
    """A retryable 429 records a meter hit and raises ``RateLimitError``."""

    adapter = _ProbeAdapter()
    log_path = tmp_path / "agent.log"

    with pytest.raises(RateLimitError):
        _probe_once(adapter, log_path, RETRYABLE_BODY)

    meter = adapter.rate_limit_meter
    assert meter.consecutive_429_count == 1
    assert meter.hits_in_window() == 1
    assert meter.backoff_seconds_current == pytest.approx(1.0)


def test_retryable_429_backoff_grows_exponentially(tmp_path: Path) -> None:
    """Consecutive retryable 429s grow the meter backoff 1s, 2s, 4s."""

    adapter = _ProbeAdapter()
    log_path = tmp_path / "agent.log"

    for _ in range(3):
        with pytest.raises(RateLimitError):
            _probe_once(adapter, log_path, RETRYABLE_BODY)

    meter = adapter.rate_limit_meter
    assert meter.consecutive_429_count == 3
    assert meter.hits_in_window() == 3
    # Exponential backoff: 1s, 2s, 4s.
    assert meter.backoff_seconds_current == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Scenario 2: standing-cap 429 short-circuits with the new reason
# ---------------------------------------------------------------------------


def test_classify_429_standing_cap_body_is_standing() -> None:
    """The issue example body maps to STANDING at the classifier level."""

    assert classify_429(STANDING_CAP_BODY) is HTTP429Classification.STANDING


def test_standing_cap_429_raises_without_meter_hit(tmp_path: Path) -> None:
    """A standing-cap 429 raises ``StandingCapError`` and skips the meter."""

    adapter = _ProbeAdapter()
    log_path = tmp_path / "agent.log"

    with pytest.raises(StandingCapError):
        _probe_once(adapter, log_path, STANDING_CAP_BODY)

    # Standing caps must not consume the retry budget or touch the meter.
    meter = adapter.rate_limit_meter
    assert meter.consecutive_429_count == 0
    assert meter.hits_in_window() == 0
    assert meter.backoff_seconds_current == 0.0


def test_standing_cap_propagates_through_classify_failure(tmp_path: Path) -> None:
    """``StandingCapError`` classifies to ``standing_cap``, non-transient."""

    adapter = _ProbeAdapter()
    log_path = tmp_path / "agent.log"

    with pytest.raises(StandingCapError) as excinfo:
        _probe_once(adapter, log_path, STANDING_CAP_BODY)

    result = classify_failure(excinfo.value)
    assert result.reason_code == "standing_cap"
    assert result.transient is False


# ---------------------------------------------------------------------------
# Scenario 3: unrecognised 429 body falls back to retryable
# ---------------------------------------------------------------------------


def test_classify_429_unknown_body_is_retryable() -> None:
    """An unrecognised body falls back to RETRYABLE at the classifier level."""

    assert classify_429(UNKNOWN_BODY) is HTTP429Classification.RETRYABLE


def test_unknown_429_falls_back_to_retryable(tmp_path: Path) -> None:
    """An unrecognised 429 records a hit and raises ``RateLimitError``."""

    adapter = _ProbeAdapter()
    log_path = tmp_path / "agent.log"

    with pytest.raises(RateLimitError):
        _probe_once(adapter, log_path, UNKNOWN_BODY)

    meter = adapter.rate_limit_meter
    assert meter.consecutive_429_count == 1
    assert meter.hits_in_window() == 1
    assert meter.backoff_seconds_current == pytest.approx(1.0)


def test_unknown_429_backoff_grows_exponentially(tmp_path: Path) -> None:
    """Consecutive unknown 429s grow the meter backoff like retryable ones."""

    adapter = _ProbeAdapter()
    log_path = tmp_path / "agent.log"

    for _ in range(3):
        with pytest.raises(RateLimitError):
            _probe_once(adapter, log_path, UNKNOWN_BODY)

    meter = adapter.rate_limit_meter
    assert meter.consecutive_429_count == 3
    assert meter.backoff_seconds_current == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Full-path taxonomy mapping
# ---------------------------------------------------------------------------


def test_retryable_429_classifies_as_rate_limit(tmp_path: Path) -> None:
    """A retryable 429 raised through the probe classifies as ``rate_limit``."""

    adapter = _ProbeAdapter()
    log_path = tmp_path / "agent.log"

    with pytest.raises(RateLimitError) as excinfo:
        _probe_once(adapter, log_path, RETRYABLE_BODY)

    result = classify_failure(excinfo.value)
    assert result.reason_code == "rate_limit"
    assert result.transient is True
