"""Regression tests for audit-010 — single source of truth for idle detection.

Before audit-010, ``_detect_idle_reason`` and the ``_IDLE_*`` thresholds
existed in both :mod:`bernstein.core.agents.agent_lifecycle` and
:mod:`bernstein.core.agents.agent_recycling`.  Tuning one copy had no effect
on the other, which silently broke recycling behaviour.

These tests lock the invariant that both modules expose the *same* objects
so the drift can never happen again without a test failure.
"""

from __future__ import annotations

from bernstein.core.agent_lifecycle import (
    _IDLE_GRACE_S as lifecycle_grace,
)
from bernstein.core.agent_lifecycle import (
    _IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S as lifecycle_evolve,
)
from bernstein.core.agent_lifecycle import (
    _IDLE_HEARTBEAT_THRESHOLD_S as lifecycle_threshold,
)
from bernstein.core.agent_lifecycle import (
    _IDLE_LIVENESS_EXTENSION_S as lifecycle_extension,
)
from bernstein.core.agent_lifecycle import (
    _detect_idle_reason as lifecycle_detect,
)
from bernstein.core.agent_lifecycle import (
    _reap_completed_agent as lifecycle_reap,
)
from bernstein.core.agent_lifecycle import (
    _recycle_or_kill as lifecycle_recycle_or_kill,
)
from bernstein.core.agent_lifecycle import (
    recycle_idle_agents as lifecycle_recycle,
)

from bernstein.core.agents import agent_recycling


class TestIdleDetectionSingleSourceOfTruth:
    """The canonical implementation is agent_recycling; agent_lifecycle re-exports."""

    def test_detect_idle_reason_is_same_callable(self) -> None:
        """_detect_idle_reason from both modules must be the exact same function object."""
        assert lifecycle_detect is agent_recycling._detect_idle_reason

    def test_recycle_idle_agents_is_same_callable(self) -> None:
        """recycle_idle_agents from both modules must be the exact same function object."""
        assert lifecycle_recycle is agent_recycling.recycle_idle_agents

    def test_reap_completed_agent_is_same_callable(self) -> None:
        """_reap_completed_agent must be shared across both modules."""
        assert lifecycle_reap is agent_recycling._reap_completed_agent

    def test_recycle_or_kill_is_same_callable(self) -> None:
        """_recycle_or_kill must be shared across both modules."""
        assert lifecycle_recycle_or_kill is agent_recycling._recycle_or_kill

    def test_idle_thresholds_are_identical(self) -> None:
        """All four _IDLE_* thresholds must match across both modules."""
        assert lifecycle_grace == agent_recycling._IDLE_GRACE_S
        assert lifecycle_threshold == agent_recycling._IDLE_HEARTBEAT_THRESHOLD_S
        assert lifecycle_evolve == agent_recycling._IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S
        assert lifecycle_extension == agent_recycling._IDLE_LIVENESS_EXTENSION_S

    def test_canonical_module_is_agent_recycling(self) -> None:
        """The source definition lives in agent_recycling, not agent_lifecycle."""
        assert lifecycle_detect.__module__ == "bernstein.core.agents.agent_recycling"
        assert lifecycle_recycle.__module__ == "bernstein.core.agents.agent_recycling"
