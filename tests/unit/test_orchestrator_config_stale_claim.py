"""Tests that ``OrchestratorConfig`` reads tuning overrides for timeout fields.

Regression test for audit-148: ``stale_claim_timeout_s`` (and ``drain_timeout_s``)
were hard-coded on :class:`OrchestratorConfig`, so ``override()`` against the
``ORCHESTRATOR`` singleton was silently ignored when a fresh config was built.
"""

from __future__ import annotations

import pytest

from bernstein.core import defaults
from bernstein.core.defaults import override, reset
from bernstein.core.tasks.models import OrchestratorConfig


@pytest.fixture(autouse=True)
def _reset_defaults() -> None:
    """Reset global defaults before each test to avoid cross-test bleed."""
    reset()


def test_stale_claim_timeout_defaults_match_singleton() -> None:
    cfg = OrchestratorConfig()
    assert cfg.stale_claim_timeout_s == pytest.approx(
        defaults.ORCHESTRATOR.stale_claim_timeout_s,
    )


def test_drain_timeout_defaults_match_singleton() -> None:
    cfg = OrchestratorConfig()
    assert cfg.drain_timeout_s == pytest.approx(
        defaults.ORCHESTRATOR.drain_timeout_s,
    )


def test_override_flows_into_new_stale_claim_timeout() -> None:
    override("orchestrator", {"stale_claim_timeout_s": 120.0})
    cfg = OrchestratorConfig()
    assert cfg.stale_claim_timeout_s == pytest.approx(120.0)


def test_override_flows_into_new_drain_timeout() -> None:
    override("orchestrator", {"drain_timeout_s": 15.0})
    cfg = OrchestratorConfig()
    assert cfg.drain_timeout_s == pytest.approx(15.0)


def test_reset_restores_original_stale_claim_timeout() -> None:
    override("orchestrator", {"stale_claim_timeout_s": 42.0})
    reset()
    cfg = OrchestratorConfig()
    assert cfg.stale_claim_timeout_s == pytest.approx(900.0)
    assert cfg.drain_timeout_s == pytest.approx(60.0)


def test_stalled_manager_threshold_defaults_match_singleton() -> None:
    """Regression test for #2179: stalled_manager_threshold_s must flow the same way."""
    cfg = OrchestratorConfig()
    assert cfg.stalled_manager_threshold_s == pytest.approx(
        defaults.ORCHESTRATOR.stalled_manager_threshold_s,
    )
    assert cfg.stalled_manager_threshold_s == pytest.approx(170.0)


def test_override_flows_into_new_stalled_manager_threshold() -> None:
    override("orchestrator", {"stalled_manager_threshold_s": 300.0})
    cfg = OrchestratorConfig()
    assert cfg.stalled_manager_threshold_s == pytest.approx(300.0)


def test_reset_restores_original_stalled_manager_threshold() -> None:
    override("orchestrator", {"stalled_manager_threshold_s": 42.0})
    reset()
    cfg = OrchestratorConfig()
    assert cfg.stalled_manager_threshold_s == pytest.approx(170.0)


def test_max_agent_runtime_defaults_match_singleton() -> None:
    """Same gap class: max_agent_runtime_s was a plain hardcoded field, not tuning-backed."""
    cfg = OrchestratorConfig()
    assert cfg.max_agent_runtime_s == defaults.ORCHESTRATOR.max_agent_runtime_s
    assert cfg.max_agent_runtime_s == 1800


def test_override_flows_into_new_max_agent_runtime() -> None:
    override("orchestrator", {"max_agent_runtime_s": 3600})
    cfg = OrchestratorConfig()
    assert cfg.max_agent_runtime_s == 3600


def test_reset_restores_original_max_agent_runtime() -> None:
    override("orchestrator", {"max_agent_runtime_s": 60})
    reset()
    cfg = OrchestratorConfig()
    assert cfg.max_agent_runtime_s == 1800
