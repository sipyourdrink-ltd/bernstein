"""Unit tests for GrowthBook-style tunable poll intervals."""

from __future__ import annotations

import pytest

from bernstein.core.poll_config import (
    MAX_INTERVAL_MS,
    MIN_INTERVAL_MS,
    PollConfig,
    PollConfigValidationError,
    validate_poll_config,
)

# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_valid_with_heartbeat() -> None:
    cfg = validate_poll_config(
        {"poll_interval_ms": 5_000, "heartbeat_interval_ms": 30_000}
    )
    assert cfg.poll_interval_ms == 5_000
    assert cfg.heartbeat_interval_ms == 30_000
    assert cfg.watchdog_interval_ms is None


def test_valid_with_watchdog() -> None:
    cfg = validate_poll_config(
        {"poll_interval_ms": 1_000, "watchdog_interval_ms": 60_000}
    )
    assert cfg.poll_interval_ms == 1_000
    assert cfg.watchdog_interval_ms == 60_000
    assert cfg.heartbeat_interval_ms is None


def test_valid_with_both_liveness_mechanisms() -> None:
    cfg = validate_poll_config(
        {
            "poll_interval_ms": 2_000,
            "heartbeat_interval_ms": 10_000,
            "watchdog_interval_ms": 120_000,
        }
    )
    assert isinstance(cfg, PollConfig)


def test_minimum_boundary_accepted() -> None:
    cfg = validate_poll_config(
        {"poll_interval_ms": MIN_INTERVAL_MS, "heartbeat_interval_ms": MIN_INTERVAL_MS}
    )
    assert cfg.poll_interval_ms == MIN_INTERVAL_MS


def test_maximum_boundary_accepted() -> None:
    cfg = validate_poll_config(
        {"poll_interval_ms": MAX_INTERVAL_MS, "watchdog_interval_ms": MAX_INTERVAL_MS}
    )
    assert cfg.poll_interval_ms == MAX_INTERVAL_MS


# ---------------------------------------------------------------------------
# Rejection tests
# ---------------------------------------------------------------------------


def test_missing_poll_interval_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"heartbeat_interval_ms": 5_000})
    assert any("poll_interval_ms is required" in e for e in exc_info.value.errors)


def test_poll_interval_below_minimum_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"poll_interval_ms": 99, "heartbeat_interval_ms": 5_000})
    assert any("below the minimum" in e for e in exc_info.value.errors)


def test_poll_interval_above_maximum_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config(
            {"poll_interval_ms": 600_001, "heartbeat_interval_ms": 5_000}
        )
    assert any("above the maximum" in e for e in exc_info.value.errors)


def test_no_liveness_mechanism_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"poll_interval_ms": 5_000})
    assert any("liveness" in e for e in exc_info.value.errors)


def test_heartbeat_below_minimum_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"poll_interval_ms": 5_000, "heartbeat_interval_ms": 50})
    assert any("below the minimum" in e for e in exc_info.value.errors)


def test_watchdog_above_maximum_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config(
            {"poll_interval_ms": 5_000, "watchdog_interval_ms": 700_000}
        )
    assert any("above the maximum" in e for e in exc_info.value.errors)


def test_non_integer_poll_interval_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config(
            {"poll_interval_ms": "5000", "heartbeat_interval_ms": 5_000}
        )
    assert any("poll_interval_ms" in e for e in exc_info.value.errors)


def test_non_integer_heartbeat_raises() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config(
            {"poll_interval_ms": 5_000, "heartbeat_interval_ms": 30.5}
        )
    assert any("heartbeat_interval_ms" in e for e in exc_info.value.errors)


def test_multiple_errors_collected() -> None:
    """Validator collects all errors before raising, not just the first."""
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"poll_interval_ms": 50, "heartbeat_interval_ms": 50})
    # Both poll_interval_ms and heartbeat_interval_ms should be below minimum
    assert len(exc_info.value.errors) >= 2


def test_error_message_mentions_field_names() -> None:
    with pytest.raises(PollConfigValidationError) as exc_info:
        validate_poll_config({"poll_interval_ms": 99, "heartbeat_interval_ms": 5_000})
    assert "poll_interval_ms" in str(exc_info.value)
