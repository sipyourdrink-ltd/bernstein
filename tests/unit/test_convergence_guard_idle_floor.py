"""A guard that blocks at zero agents can never unblock itself (#4336).

Gates 3 and 4 are backpressure: they exist to slow a system down while it
is under load. With no agent running there is no load, and the spawn they
block is the only thing that could bring the error rate back down, so the
run stalls until its timeout instead of recovering.
"""

from __future__ import annotations

from bernstein.core.orchestration.convergence_guard import ConvergenceGuard


def _guard() -> ConvergenceGuard:
    return ConvergenceGuard()


def test_high_error_rate_does_not_block_when_nothing_is_running() -> None:
    status = _guard().is_converged(active_agents=0, error_rate=1.0)
    assert status.ready, status.reasons


def test_high_spawn_rate_does_not_block_when_nothing_is_running() -> None:
    status = _guard().is_converged(active_agents=0, spawn_rate=10_000.0)
    assert status.ready, status.reasons


def test_high_error_rate_still_blocks_while_agents_are_running() -> None:
    status = _guard().is_converged(active_agents=1, error_rate=1.0)
    assert not status.ready
    assert any("error rate" in r.lower() for r in status.reasons)


def test_agent_cap_still_blocks_at_its_own_limit() -> None:
    """The idle floor covers the rate gates only; the cap is not backpressure."""
    guard = _guard()
    status = guard.is_converged(active_agents=guard._cfg.max_active_agents, error_rate=1.0)
    assert not status.ready


def test_unknown_agent_count_keeps_the_rate_gates_armed() -> None:
    """``active_agents=None`` means unmeasured, not zero."""
    status = _guard().is_converged(error_rate=1.0)
    assert not status.ready
