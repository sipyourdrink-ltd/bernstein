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


def test_janitor_defaults_documented() -> None:
    """audit-081: retention/rotation knobs must live in JanitorDefaults."""
    reset()
    assert defaults.JANITOR.run_retention_count == 20
    assert defaults.JANITOR.wal_retention_count == 50
    assert defaults.JANITOR.bridge_lineage_rotate_bytes > 0
    assert defaults.JANITOR.task_notifications_rotate_bytes > 0
    assert defaults.JANITOR.idempotency_rotate_bytes > 0
    assert defaults.JANITOR.file_health_rotate_bytes > 0
    assert defaults.JANITOR.file_health_touches_rotate_bytes > 0
    assert defaults.JANITOR.replay_rotate_bytes > 0


def test_janitor_override_round_trip() -> None:
    """``override("janitor", …)`` must tune retention without breaking reset()."""
    reset()
    try:
        override("janitor", {"run_retention_count": 5, "wal_retention_count": 7})
        assert defaults.JANITOR.run_retention_count == 5
        assert defaults.JANITOR.wal_retention_count == 7
    finally:
        reset()
    assert defaults.JANITOR.run_retention_count == 20
    assert defaults.JANITOR.wal_retention_count == 50
