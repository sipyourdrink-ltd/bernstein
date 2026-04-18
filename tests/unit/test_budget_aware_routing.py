"""Tests for audit-102: budget-aware router downgrades opus to sonnet near cap."""

from __future__ import annotations

import pytest
from bernstein.core.models import Complexity, Scope, Task

from bernstein.core.routing.router_core import (
    BUDGET_AWARE_OPUS_MARGIN,
    DEFAULT_OPUS_TASK_COST_USD,
    _check_opus_override,
    clear_budget_context,
    route_task,
    set_budget_context,
)


def _make_task(
    *,
    id: str = "T-budget-102",
    role: str = "backend",
    title: str = "Implement feature",
    description: str = "Write the code.",
    scope: Scope = Scope.MEDIUM,
    complexity: Complexity = Complexity.MEDIUM,
    priority: int = 2,
) -> Task:
    return Task(
        id=id,
        title=title,
        description=description,
        role=role,
        scope=scope,
        complexity=complexity,
        priority=priority,
    )


@pytest.fixture(autouse=True)
def _reset_budget_context() -> None:
    """Ensure module-level budget state starts clean for every test."""
    clear_budget_context()
    yield
    clear_budget_context()


class TestCheckOpusOverrideBudgetAware:
    """audit-102: `_check_opus_override` respects the budget threshold."""

    def test_near_cap_priority_1_architect_downgrades(self) -> None:
        """budget=1.0, spend=0.95, priority=1/architect → None (route sonnet)."""
        task = _make_task(role="architect", priority=1)
        remaining = 1.0 - 0.95  # $0.05 left

        assert (
            _check_opus_override(
                task,
                budget_remaining_usd=remaining,
                budget_aware_routing_enabled=True,
            )
            is None
        )

    def test_near_cap_high_stakes_role_downgrades(self) -> None:
        task = _make_task(role="manager", priority=2)

        assert (
            _check_opus_override(
                task,
                budget_remaining_usd=0.10,
                budget_aware_routing_enabled=True,
            )
            is None
        )

    def test_near_cap_large_scope_downgrades(self) -> None:
        task = _make_task(scope=Scope.LARGE, complexity=Complexity.HIGH)

        assert (
            _check_opus_override(
                task,
                budget_remaining_usd=0.50,
                budget_aware_routing_enabled=True,
            )
            is None
        )

    def test_ample_budget_still_escalates_to_opus(self) -> None:
        """When remaining >= 2x est opus cost, override returns the reason."""
        task = _make_task(role="architect", priority=1)
        plenty = BUDGET_AWARE_OPUS_MARGIN * DEFAULT_OPUS_TASK_COST_USD + 1.0

        reason = _check_opus_override(
            task,
            budget_remaining_usd=plenty,
            budget_aware_routing_enabled=True,
        )

        assert reason is not None
        assert "architect" in reason

    def test_flag_disabled_preserves_legacy_behavior(self) -> None:
        """When the feature flag is off, near-cap tasks still escalate."""
        task = _make_task(role="manager")

        reason = _check_opus_override(
            task,
            budget_remaining_usd=0.01,
            budget_aware_routing_enabled=False,
        )

        assert reason is not None
        assert "manager" in reason

    def test_unknown_budget_preserves_legacy_behavior(self) -> None:
        """None budget means unknown — no downgrade."""
        task = _make_task(role="security")

        reason = _check_opus_override(
            task,
            budget_remaining_usd=None,
            budget_aware_routing_enabled=True,
        )

        assert reason is not None

    def test_infinite_budget_preserves_legacy_behavior(self) -> None:
        """Unlimited budgets (inf) never trigger a downgrade."""
        task = _make_task(role="security")

        reason = _check_opus_override(
            task,
            budget_remaining_usd=float("inf"),
            budget_aware_routing_enabled=True,
        )

        assert reason is not None

    def test_non_opus_task_unaffected_by_budget(self) -> None:
        """A regular backend task never had opus override → still returns None."""
        task = _make_task(role="backend", priority=2)

        assert (
            _check_opus_override(
                task,
                budget_remaining_usd=0.01,
                budget_aware_routing_enabled=True,
            )
            is None
        )
        assert (
            _check_opus_override(
                task,
                budget_remaining_usd=100.0,
                budget_aware_routing_enabled=True,
            )
            is None
        )

    def test_threshold_boundary_exactly_at_margin(self) -> None:
        """remaining == 2x est opus cost does NOT downgrade (strict `<`)."""
        task = _make_task(role="architect", priority=1)
        boundary = BUDGET_AWARE_OPUS_MARGIN * DEFAULT_OPUS_TASK_COST_USD

        reason = _check_opus_override(
            task,
            budget_remaining_usd=boundary,
            budget_aware_routing_enabled=True,
        )

        assert reason is not None

    def test_threshold_just_below_margin_downgrades(self) -> None:
        """remaining just below 2x est opus cost downgrades."""
        task = _make_task(role="architect", priority=1)
        just_below = BUDGET_AWARE_OPUS_MARGIN * DEFAULT_OPUS_TASK_COST_USD - 0.01

        assert (
            _check_opus_override(
                task,
                budget_remaining_usd=just_below,
                budget_aware_routing_enabled=True,
            )
            is None
        )


class TestCheckOpusOverrideModuleState:
    """audit-102: module-level `set_budget_context` feeds `_check_opus_override`."""

    def test_set_budget_context_triggers_downgrade(self) -> None:
        task = _make_task(role="manager")
        set_budget_context(0.05, enabled=True)

        assert _check_opus_override(task) is None

    def test_set_budget_context_disabled_preserves_opus(self) -> None:
        task = _make_task(role="manager")
        set_budget_context(0.05, enabled=False)

        assert _check_opus_override(task) is not None

    def test_clear_budget_context_restores_default(self) -> None:
        task = _make_task(role="manager")
        set_budget_context(0.05, enabled=True)
        clear_budget_context()

        # Context cleared — no budget known, legacy behavior returns reason.
        assert _check_opus_override(task) is not None


class TestRouteTaskBudgetAware:
    """Integration test at the `route_task` layer."""

    def test_route_task_downgrades_to_sonnet_near_cap(self) -> None:
        """audit-102 repro: budget=1.0, spend=0.95, priority=1/architect → sonnet."""
        task = _make_task(role="architect", priority=1)

        config = route_task(
            task,
            budget_remaining_usd=0.05,
            budget_aware_routing_enabled=True,
        )

        assert config.model == "sonnet"

    def test_route_task_ample_budget_escalates_to_opus(self) -> None:
        task = _make_task(role="architect", priority=1)

        config = route_task(
            task,
            budget_remaining_usd=100.0,
            budget_aware_routing_enabled=True,
        )

        assert config.model == "opus"
        assert config.effort == "max"

    def test_route_task_flag_off_always_escalates(self) -> None:
        """Back-compat: with flag off, tight budget still gives opus."""
        task = _make_task(role="manager")

        config = route_task(
            task,
            budget_remaining_usd=0.01,
            budget_aware_routing_enabled=False,
        )

        assert config.model == "opus"

    def test_route_task_default_call_unchanged(self) -> None:
        """route_task() with no budget args preserves legacy behaviour."""
        task = _make_task(role="manager")

        config = route_task(task)

        assert config.model == "opus"
        assert config.effort == "max"

    def test_route_task_uses_module_context_when_no_kwarg(self) -> None:
        task = _make_task(role="security")
        set_budget_context(0.05, enabled=True)

        config = route_task(task)

        assert config.model == "sonnet"

    def test_manager_override_not_downgraded(self) -> None:
        """Manager-specified model wins regardless of budget."""
        task = _make_task(role="backend")
        task.model = "opus"
        task.effort = "max"

        config = route_task(
            task,
            budget_remaining_usd=0.01,
            budget_aware_routing_enabled=True,
        )

        assert config.model == "opus"
