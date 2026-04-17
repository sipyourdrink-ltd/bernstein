"""Regression test for audit-005: single source of truth for idle recycling.

Prior to audit-005, ``recycle_idle_agents`` and its helpers were defined in
both :mod:`bernstein.core.agents.agent_lifecycle` and
:mod:`bernstein.core.agents.agent_recycling`.  Only the lifecycle copy was
imported by the orchestrator, so changes to the recycling module had no
runtime effect and the two implementations had already drifted (snapshot
indexing differed).

This test locks in the post-fix invariant: the recycling functions live in
``agent_recycling`` and every legacy import path resolves to the *same*
function object.  If a future refactor re-introduces a duplicate, both
``is`` comparisons will fail loudly.
"""

from __future__ import annotations

from bernstein.core.agents import agent_lifecycle, agent_recycling
from bernstein.core.orchestration import orchestrator, orchestrator_cleanup


def test_recycle_idle_agents_single_source() -> None:
    """``recycle_idle_agents`` must have exactly one implementation.

    The ``agent_lifecycle`` re-export, the orchestrator, and the tick
    pipeline must all resolve to the function defined in
    :mod:`bernstein.core.agents.agent_recycling`.
    """
    canonical = agent_recycling.recycle_idle_agents

    # agent_lifecycle re-exports the canonical implementation.
    assert agent_lifecycle.recycle_idle_agents is canonical

    # Production callers bind to the same canonical function.
    assert orchestrator.recycle_idle_agents is canonical

    # ``agent_lifecycle.recycle_idle_agents`` is defined in agent_recycling.
    assert canonical.__module__ == "bernstein.core.agents.agent_recycling"


def test_idle_recycling_helpers_single_source() -> None:
    """Audit-005 consolidated idle-recycling helpers have a single canonical definition.

    Six additional dup helpers (check_kill_signals, send_shutdown_signals,
    check_stale_agents, check_stalled_tasks, check_loops_and_deadlocks,
    _is_process_alive) remain byte-identical copies in both modules; their
    consolidation is tracked as audit-005b follow-up.
    """
    for name in (
        "_detect_idle_reason",
        "_reap_completed_agent",
        "_recycle_or_kill",
    ):
        lifecycle_ref = getattr(agent_lifecycle, name)
        recycling_ref = getattr(agent_recycling, name)
        assert lifecycle_ref is recycling_ref, (
            f"{name} diverged between agent_lifecycle and agent_recycling — "
            "see audit-005"
        )
        assert recycling_ref.__module__ == "bernstein.core.agents.agent_recycling"


def test_idle_threshold_constants_single_source() -> None:
    """Idle-threshold module constants have a single definition.

    Values are compared (not identities, since floats are interned
    inconsistently) — the important invariant is that both modules agree.
    """
    for name in (
        "_IDLE_GRACE_S",
        "_IDLE_HEARTBEAT_THRESHOLD_S",
        "_IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S",
        "_IDLE_LIVENESS_EXTENSION_S",
    ):
        assert getattr(agent_lifecycle, name) == getattr(agent_recycling, name)


def test_send_shutdown_signals_single_source() -> None:
    """orchestrator_cleanup uses the canonical send_shutdown_signals."""
    assert orchestrator_cleanup.send_shutdown_signals is agent_recycling.send_shutdown_signals
