"""The error-rate gate needs enough observations before it may block a wave.

The gate is backpressure, and blocking on it removes the only thing that can
clear the condition: no wave spawns, so no further task completes, so the rate
never moves. With no floor on the sample count, one task failing its quality
gate read as a 100% error rate and stopped the run for the rest of its
timeout -- observed as 93 consecutive "High error rate (100% > 50%)" blocks on
a run whose first completed task was reopened by the janitor.
"""

from __future__ import annotations

import pytest

from bernstein.core.orchestration.convergence_guard import ConvergenceGuard


def _ask(guard: ConvergenceGuard, *, active_agents: int = 1):
    """Run the gate exactly the way ``claim_and_spawn_batches`` runs it."""
    rate = guard.current_error_rate()
    return guard.is_converged(
        pending_merges=0,
        active_agents=active_agents,
        error_rate=rate if rate >= 0 else None,
        spawn_rate=guard.current_spawn_rate(),
    )


def test_one_failure_really_does_read_as_a_total_error_rate() -> None:
    """Guards the rest of the file: without this the floor tests are vacuous."""
    guard = ConvergenceGuard()
    guard.record_failure()

    assert guard.current_error_rate() == 1.0
    assert guard.current_error_rate() > guard.config.max_error_rate


@pytest.mark.parametrize("failures", [1, 2])
def test_a_thin_window_does_not_block_the_wave(failures: int) -> None:
    """The deadlock: one or two failures must not stop the run recovering."""
    guard = ConvergenceGuard()
    for _ in range(failures):
        guard.record_failure()

    status = _ask(guard)

    assert status.ready, status.reasons
    assert not any("error rate" in r for r in status.reasons)


def test_the_gate_engages_once_the_window_holds_enough() -> None:
    """The floor delays the gate; it does not remove it."""
    guard = ConvergenceGuard()
    for _ in range(guard.config.min_error_rate_samples):
        guard.record_failure()

    status = _ask(guard)

    assert not status.ready
    assert any("error rate" in r for r in status.reasons)


def test_a_rate_under_the_threshold_still_passes_at_full_sample() -> None:
    """The floor must not turn into a blanket pass once samples accumulate."""
    guard = ConvergenceGuard()
    for _ in range(2):
        guard.record_failure()
    for _ in range(2):
        guard.record_success()

    assert _ask(guard).ready


def test_an_externally_supplied_rate_is_still_trusted() -> None:
    """A caller that passes a rate this guard never recorded keeps the old gate.

    The floor is computed from the guard's own window, so a guard with no
    samples of its own has nothing to judge the caller's number against.
    """
    guard = ConvergenceGuard()

    status = guard.is_converged(pending_merges=0, active_agents=1, error_rate=0.9)

    assert not status.ready
    assert any("error rate" in r for r in status.reasons)
