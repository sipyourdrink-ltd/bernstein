"""Tests for auto_compact — AutoCompactTrigger and AutoCompactConfig."""

from __future__ import annotations

from bernstein.core.auto_compact import AutoCompactConfig, AutoCompactTrigger, CircuitState

# ---------------------------------------------------------------------------
# should_compact
# ---------------------------------------------------------------------------


class TestShouldCompact:
    def test_below_threshold_returns_false(self) -> None:
        config = AutoCompactConfig(threshold_pct=80.0)
        trigger = AutoCompactTrigger(session_id="s1", config=config)
        assert trigger.should_compact(8000, 200_000) is False

    def test_above_threshold_circuit_closed_returns_true(self) -> None:
        config = AutoCompactConfig(threshold_pct=80.0)
        trigger = AutoCompactTrigger(session_id="s1", config=config)
        # 90% utilization > 80% threshold, circuit is CLOSED
        assert trigger.should_compact(90_000, 100_000) is True

    def test_zero_max_tokens_returns_false(self) -> None:
        config = AutoCompactConfig(threshold_pct=80.0)
        trigger = AutoCompactTrigger(session_id="s1", config=config)
        assert trigger.should_compact(1000, 0) is False

    def test_negative_max_tokens_returns_false(self) -> None:
        config = AutoCompactConfig(threshold_pct=80.0)
        trigger = AutoCompactTrigger(session_id="s1", config=config)
        assert trigger.should_compact(1000, -1) is False

    def test_exact_threshold_circuits_returns_true(self) -> None:
        """At exactly the threshold percentage, compaction should trigger."""
        config = AutoCompactConfig(threshold_pct=80.0)
        trigger = AutoCompactTrigger(session_id="s1", config=config)
        assert trigger.should_compact(80_000, 100_000) is True


# ---------------------------------------------------------------------------
# Circuit breaker — CLOSED → OPEN transition
# ---------------------------------------------------------------------------


class TestCircuitClosedToOpen:
    def test_opens_after_max_consecutive_failures(self) -> None:
        config = AutoCompactConfig(threshold_pct=80.0, max_consecutive_failures=3)
        trigger = AutoCompactTrigger(session_id="s1", config=config)

        trigger.record_compaction_failure(now=0.0)
        assert not trigger.is_circuit_open()
        trigger.record_compaction_failure(now=1.0)
        assert not trigger.is_circuit_open()
        trigger.record_compaction_failure(now=2.0)
        assert trigger.is_circuit_open()

    def test_success_resets_consecutive_failures(self) -> None:
        config = AutoCompactConfig(threshold_pct=80.0, max_consecutive_failures=3)
        trigger = AutoCompactTrigger(session_id="s1", config=config)

        trigger.record_compaction_failure(now=0.0)
        trigger.record_compaction_failure(now=1.0)
        trigger.record_compaction_success()

        # After success, failures reset
        assert trigger.consecutive_failures == 0
        assert trigger.is_circuit_open() is False

    def test_failure_after_success_does_not_immediately_open(self) -> None:
        config = AutoCompactConfig(threshold_pct=80.0, max_consecutive_failures=3)
        trigger = AutoCompactTrigger(session_id="s1", config=config)

        trigger.record_compaction_failure(now=0.0)
        trigger.record_compaction_success()  # resets to 0 failures
        trigger.record_compaction_failure(now=1.0)
        assert not trigger.is_circuit_open()  # only 1 of 3 failures


# ---------------------------------------------------------------------------
# Circuit breaker — OPEN → HALF_OPEN → CLOSED
# ---------------------------------------------------------------------------


class TestCircuitHalfOpen:
    def test_open_allows_after_cooldown(self) -> None:
        config = AutoCompactConfig(
            threshold_pct=80.0,
            max_consecutive_failures=1,
            retry_delay_s=60.0,
        )
        trigger = AutoCompactTrigger(session_id="s1", config=config)

        # Trigger opens circuit
        trigger.record_compaction_failure(now=0.0)
        assert trigger.is_circuit_open()

        # Before cooldown elapsed (30s < 60s): blocked
        assert trigger.should_compact(90_000, 100_000, now=30.0) is False

        # After cooldown (90s > 60s): allowed (transitions to HALF_OPEN)
        assert trigger.should_compact(90_000, 100_000, now=90.0) is True

    def test_half_open_success_resets_to_closed(self) -> None:
        config = AutoCompactConfig(
            threshold_pct=80.0,
            max_consecutive_failures=1,
            retry_delay_s=60.0,
        )
        trigger = AutoCompactTrigger(session_id="s1", config=config)

        trigger.record_compaction_failure(now=0.0)
        # Wait out cooldown
        trigger.should_compact(90_000, 100_000)  # transitions to HALF_OPEN
        trigger.record_compaction_success()
        assert trigger.state == CircuitState.CLOSED

    def test_half_open_failure_returns_to_open(self) -> None:
        config = AutoCompactConfig(
            threshold_pct=80.0,
            max_consecutive_failures=1,
            retry_delay_s=60.0,
        )
        trigger = AutoCompactTrigger(session_id="s1", config=config)

        trigger.record_compaction_failure(now=0.0)
        # Wait out cooldown
        trigger.should_compact(90_000, 100_000)  # transitions to HALF_OPEN
        trigger.record_compaction_failure(now=70.0)
        assert trigger.is_circuit_open()


# ---------------------------------------------------------------------------
# reset_circuit
# ---------------------------------------------------------------------------


class TestResetCircuit:
    def test_reset_restores_closed_and_clears_failures(self) -> None:
        config = AutoCompactConfig(threshold_pct=80.0, max_consecutive_failures=3)
        trigger = AutoCompactTrigger(session_id="s1", config=config)

        trigger.record_compaction_failure(now=0.0)
        trigger.record_compaction_failure(now=1.0)
        trigger.record_compaction_failure(now=2.0)

        assert trigger.is_circuit_open()
        assert trigger.consecutive_failures == 3

        trigger.reset_circuit()

        assert trigger.state == CircuitState.CLOSED
        assert trigger.consecutive_failures == 0

    def test_reset_on_unused_circuit_is_noop(self) -> None:
        config = AutoCompactConfig()
        trigger = AutoCompactTrigger(session_id="s1", config=config)
        trigger.reset_circuit()  # should not raise
        assert trigger.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Statistics counters
# ---------------------------------------------------------------------------


class TestCounters:
    def test_total_attempts_increments_on_failure(self) -> None:
        config = AutoCompactConfig()
        trigger = AutoCompactTrigger(session_id="s1", config=config)
        trigger.record_compaction_failure(now=0.0)
        trigger.record_compaction_failure(now=1.0)
        assert trigger.total_attempts == 2

    def test_total_successes_increments(self) -> None:
        config = AutoCompactConfig()
        trigger = AutoCompactTrigger(session_id="s1", config=config)
        trigger.record_compaction_success()
        trigger.record_compaction_success()
        assert trigger.total_successes == 2


# ---------------------------------------------------------------------------
# AutoCompactConfig defaults
# ---------------------------------------------------------------------------


class TestAutoCompactConfigDefaults:
    def test_default_threshold_pct(self) -> None:
        config = AutoCompactConfig()
        assert config.threshold_pct == 80.0

    def test_default_max_consecutive_failures(self) -> None:
        config = AutoCompactConfig()
        assert config.max_consecutive_failures == 3

    def test_default_retry_delay_s(self) -> None:
        config = AutoCompactConfig()
        assert config.retry_delay_s == 120.0
