"""Unit tests for the compaction circuit breaker in TokenGrowthMonitor."""

from __future__ import annotations

from bernstein.core.token_budget import GROWTH_THRESHOLD, TokenGrowthMonitor


def test_circuit_breaker_opens_after_three_failures():
    monitor = TokenGrowthMonitor(session_id="test-session")

    # Trigger intervention
    monitor.growth_rate = GROWTH_THRESHOLD + 1.0
    monitor.intervention_triggered = True

    # Circuit should be closed initially
    assert monitor.should_compact() is True

    # Record 1 failure
    monitor.record_compaction_failure()
    assert monitor.compaction_fail_count == 1
    assert monitor.should_compact() is True

    # Record 2nd failure
    monitor.record_compaction_failure()
    assert monitor.compaction_fail_count == 2
    assert monitor.should_compact() is True

    # Record 3rd failure - Circuit should OPEN
    monitor.record_compaction_failure()
    assert monitor.compaction_fail_count == 3
    assert monitor.should_compact() is False

    # Record 4th failure - Circuit stays OPEN
    monitor.record_compaction_failure()
    assert monitor.compaction_fail_count == 4
    assert monitor.should_compact() is False


def test_circuit_breaker_resets_on_success():
    monitor = TokenGrowthMonitor(session_id="test-session")

    # Trigger intervention and open circuit
    monitor.intervention_triggered = True
    for _ in range(3):
        monitor.record_compaction_failure()

    assert monitor.should_compact() is False

    # Record success - Circuit should CLOSE and intervention reset
    monitor.record_compaction_success()
    assert monitor.compaction_fail_count == 0
    assert monitor.intervention_triggered is False
    assert monitor.should_compact() is False

    # Trigger intervention again - should allow compaction
    monitor.intervention_triggered = True
    assert monitor.should_compact() is True


def test_should_compact_respects_intervention_triggered():
    monitor = TokenGrowthMonitor(session_id="test-session")

    # No intervention triggered
    assert monitor.should_compact() is False

    # Record failure (but no intervention)
    monitor.record_compaction_failure()
    assert monitor.should_compact() is False

    # Trigger intervention
    monitor.intervention_triggered = True
    assert monitor.should_compact() is True
