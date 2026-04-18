"""Tests for bernstein.core.defaults override/reset mechanics."""

from __future__ import annotations

import dataclasses
from types import MappingProxyType

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


# ---------------------------------------------------------------------------
# audit-155 — freeze guarantees
# ---------------------------------------------------------------------------


def test_defaults_dataclasses_are_frozen() -> None:
    """Every ``*Defaults`` dataclass must be ``frozen=True`` (audit-155)."""
    reset()
    singletons = (
        defaults.ORCHESTRATOR,
        defaults.SPAWN,
        defaults.AGENT,
        defaults.TASK,
        defaults.TOKEN,
        defaults.COST,
        defaults.GATE,
        defaults.PARALLELISM,
        defaults.APPROVAL,
        defaults.PROTOCOL,
        defaults.PLAN,
        defaults.TRIGGER,
        defaults.JANITOR,
    )
    for singleton in singletons:
        params = singleton.__dataclass_params__
        assert params.frozen is True, (
            f"{type(singleton).__name__} must be frozen (audit-155)"
        )


def test_direct_attribute_mutation_raises_frozen_instance_error() -> None:
    """``COST.foo = 1`` must raise FrozenInstanceError (audit-155)."""
    reset()
    with pytest.raises(dataclasses.FrozenInstanceError):
        defaults.COST.fallback_cost_per_1k_tokens = 0.999  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        defaults.ORCHESTRATOR.tick_interval_s = 0.1  # type: ignore[misc]


def test_inner_dict_mutation_raises_type_error() -> None:
    """Dict fields are ``MappingProxyType``; item assignment is blocked."""
    reset()
    with pytest.raises(TypeError):
        defaults.COST.effort_base_turns["max"] = 0  # type: ignore[index]
    with pytest.raises(TypeError):
        defaults.TASK.scope_timeout_s["small"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        defaults.PLAN.tokens_by_scope["small"] = 99  # type: ignore[index]


def test_dict_fields_are_mapping_proxy() -> None:
    """Dict default factories must hand out ``MappingProxyType`` views."""
    reset()
    assert isinstance(defaults.COST.effort_base_turns, MappingProxyType)
    assert isinstance(defaults.COST.scope_budget_usd, MappingProxyType)
    assert isinstance(defaults.COST.scope_multipliers, MappingProxyType)
    assert isinstance(defaults.TASK.scope_timeout_s, MappingProxyType)
    assert isinstance(defaults.PLAN.tokens_by_scope, MappingProxyType)
    assert isinstance(defaults.PLAN.model_by_complexity, MappingProxyType)


def test_override_rebinds_singleton_atomically() -> None:
    """``override`` must rebind the module attribute, not mutate in place."""
    reset()
    before = defaults.ORCHESTRATOR
    override("orchestrator", {"tick_interval_s": 4.2})
    after = defaults.ORCHESTRATOR
    # Fresh object — the old snapshot keeps its values.
    assert before is not after
    assert before.tick_interval_s == pytest.approx(3.0)
    assert after.tick_interval_s == pytest.approx(4.2)
    # Override survives subsequent scalar updates on unrelated fields.
    override("orchestrator", {"drain_timeout_s": 42.0})
    assert defaults.ORCHESTRATOR.tick_interval_s == pytest.approx(4.2)
    assert defaults.ORCHESTRATOR.drain_timeout_s == pytest.approx(42.0)
    reset()


def test_override_preserves_frozen_invariant() -> None:
    """After ``override``, the rebound singleton is still frozen."""
    reset()
    override("cost", {"opus_budget_multiplier": 3.0})
    with pytest.raises(dataclasses.FrozenInstanceError):
        defaults.COST.opus_budget_multiplier = 4.0  # type: ignore[misc]
    # Merged dict remains read-only.
    override("cost", {"effort_base_turns": {"max": 250}})
    assert defaults.COST.effort_base_turns["max"] == 250
    assert defaults.COST.effort_base_turns["high"] == 50
    with pytest.raises(TypeError):
        defaults.COST.effort_base_turns["max"] = 1  # type: ignore[index]
    reset()


def test_reset_restores_defaults_and_rebinds() -> None:
    """``reset`` must produce brand-new instances equal to fresh factories."""
    reset()
    override("cost", {"opus_budget_multiplier": 99.0})
    override("task", {"xl_timeout_s": 1.0})
    override("janitor", {"run_retention_count": 1})
    assert defaults.COST.opus_budget_multiplier == pytest.approx(99.0)
    assert defaults.TASK.xl_timeout_s == pytest.approx(1.0)
    assert defaults.JANITOR.run_retention_count == 1

    reset()

    fresh_cost = defaults.CostDefaults()
    fresh_task = defaults.TaskDefaults()
    fresh_janitor = defaults.JanitorDefaults()
    assert defaults.COST.opus_budget_multiplier == pytest.approx(
        fresh_cost.opus_budget_multiplier,
    )
    assert defaults.TASK.xl_timeout_s == pytest.approx(fresh_task.xl_timeout_s)
    assert defaults.JANITOR.run_retention_count == fresh_janitor.run_retention_count
