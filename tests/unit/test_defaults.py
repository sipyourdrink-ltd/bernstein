"""Tests for bernstein.core.defaults override/reset mechanics."""

from __future__ import annotations

import pytest

from bernstein.core import defaults
from bernstein.core.defaults import override, reset


def test_override_scalar() -> None:
    reset()
    assert defaults.ORCHESTRATOR.tick_interval_s == pytest.approx(3.0)
    override("orchestrator", {"tick_interval_s": 5.0})
    assert defaults.ORCHESTRATOR.tick_interval_s == pytest.approx(5.0)
    reset()


def test_override_dict_merges() -> None:
    reset()
    override("task", {"scope_timeout_s": {"large": 7200}})
    assert defaults.TASK.scope_timeout_s["large"] == 7200
    assert defaults.TASK.scope_timeout_s["small"] == 900  # unchanged
    reset()


def test_override_invalid_section() -> None:
    with pytest.raises(KeyError):
        override("nonexistent", {"foo": 1})


def test_override_invalid_field() -> None:
    reset()
    with pytest.raises(AttributeError):
        override("orchestrator", {"bogus_field": 1})
    reset()


def test_tick_interval_s_drives_orchestrator_poll_interval() -> None:
    """audit-149: ``ORCHESTRATOR.tick_interval_s`` is the canonical source for
    ``OrchestratorConfig.poll_interval_s``; overriding it must change the tick
    rate observed by freshly constructed configs.
    """
    from bernstein.core.tasks.models import OrchestratorConfig

    reset()
    baseline = OrchestratorConfig()
    assert baseline.poll_interval_s == int(defaults.ORCHESTRATOR.tick_interval_s)

    override("orchestrator", {"tick_interval_s": 7.0})
    bumped = OrchestratorConfig()
    assert bumped.poll_interval_s == 7
    assert isinstance(bumped.poll_interval_s, int)

    # Fractional values truncate to int (poll_interval_s is declared int).
    override("orchestrator", {"tick_interval_s": 2.9})
    fractional = OrchestratorConfig()
    assert fractional.poll_interval_s == 2

    reset()
    restored = OrchestratorConfig()
    assert restored.poll_interval_s == 3


def test_orchestrator_config_explicit_poll_interval_wins() -> None:
    """Explicit ``poll_interval_s`` arg overrides the default_factory."""
    from bernstein.core.tasks.models import OrchestratorConfig

    reset()
    override("orchestrator", {"tick_interval_s": 9.0})
    try:
        cfg = OrchestratorConfig(poll_interval_s=1)
        assert cfg.poll_interval_s == 1
    finally:
        reset()
