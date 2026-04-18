"""Tests for budget enforcement actions (COST-005)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from bernstein.core.budget_actions import (
    BudgetAction,
    BudgetActionResult,
    BudgetPolicy,
    BudgetThresholdRule,
    apply_policy,
    suggest_downgrade,
)


@dataclass
class _FakeTask:
    """Minimal task-like object used to exercise apply_policy() mutation."""

    id: str
    model: str = ""


def test_default_policy_creation() -> None:
    """Default policy has three rules."""
    policy = BudgetPolicy.default()
    assert len(policy.rules) == 3


def test_continue_below_all_thresholds() -> None:
    """Under all thresholds returns CONTINUE."""
    policy = BudgetPolicy.default()
    result = policy.evaluate(0.5)
    assert result.action == BudgetAction.CONTINUE


def test_pause_at_80_percent() -> None:
    """At 80% spend, action is PAUSE."""
    policy = BudgetPolicy.default()
    result = policy.evaluate(0.82)
    assert result.action == BudgetAction.PAUSE


def test_downgrade_at_90_percent() -> None:
    """At 90% spend, action is DOWNGRADE_MODEL."""
    policy = BudgetPolicy.default()
    result = policy.evaluate(0.92)
    assert result.action == BudgetAction.DOWNGRADE_MODEL


def test_abort_at_100_percent() -> None:
    """At 100% spend, action is ABORT."""
    policy = BudgetPolicy.default()
    result = policy.evaluate(1.0)
    assert result.action == BudgetAction.ABORT


def test_abort_over_100_percent() -> None:
    """Over 100% still returns ABORT."""
    policy = BudgetPolicy.default()
    result = policy.evaluate(1.5)
    assert result.action == BudgetAction.ABORT


def test_custom_policy() -> None:
    """Custom policy with a single rule."""
    policy = BudgetPolicy(
        rules=[
            BudgetThresholdRule(threshold_pct=0.50, action=BudgetAction.ABORT, message="Half gone"),
        ]
    )
    assert policy.evaluate(0.3).action == BudgetAction.CONTINUE
    assert policy.evaluate(0.6).action == BudgetAction.ABORT


def test_highest_threshold_wins() -> None:
    """When multiple rules match, the highest threshold rule wins."""
    policy = BudgetPolicy(
        rules=[
            BudgetThresholdRule(threshold_pct=0.50, action=BudgetAction.PAUSE),
            BudgetThresholdRule(threshold_pct=0.90, action=BudgetAction.ABORT),
        ]
    )
    result = policy.evaluate(0.95)
    assert result.action == BudgetAction.ABORT


def test_policy_serialisation_roundtrip() -> None:
    """Policy survives to_dict/from_dict roundtrip."""
    policy = BudgetPolicy.default()
    d = policy.to_dict()
    restored = BudgetPolicy.from_dict(d)
    assert len(restored.rules) == len(policy.rules)
    for orig, rest in zip(policy.rules, restored.rules, strict=True):
        assert orig.threshold_pct == rest.threshold_pct
        assert orig.action == rest.action


def test_result_to_dict() -> None:
    """BudgetActionResult.to_dict has expected keys."""
    result = BudgetActionResult(
        action=BudgetAction.PAUSE,
        threshold_pct=0.8,
        percentage_used=0.85,
        message="Budget warning",
    )
    d = result.to_dict()
    assert d["action"] == "pause"
    assert d["threshold_pct"] == pytest.approx(0.8)
    assert "message" in d


def test_suggest_downgrade_opus() -> None:
    """Downgrade from opus should suggest sonnet."""
    assert suggest_downgrade("opus") == "sonnet"


def test_suggest_downgrade_sonnet() -> None:
    """Downgrade from sonnet should suggest haiku."""
    assert suggest_downgrade("sonnet") == "haiku"


def test_suggest_downgrade_haiku() -> None:
    """Downgrade from haiku returns None (no cheaper option)."""
    assert suggest_downgrade("haiku") is None


def test_suggest_downgrade_unknown() -> None:
    """Unknown model returns None."""
    assert suggest_downgrade("unknown-model-xyz") is None


# ---------------------------------------------------------------------------
# apply_policy() — policy evaluation + task-model mutation (audit-058)
# ---------------------------------------------------------------------------


def test_apply_policy_continue_leaves_tasks_untouched() -> None:
    """Under all thresholds no tasks are mutated and action is CONTINUE."""
    policy = BudgetPolicy.default()
    tasks = [_FakeTask(id="t1", model="opus"), _FakeTask(id="t2", model="sonnet")]
    result = apply_policy(policy, 0.2, tasks=tasks)
    assert result.action == BudgetAction.CONTINUE
    assert tasks[0].model == "opus"
    assert tasks[1].model == "sonnet"


def test_apply_policy_pause_does_not_mutate_tasks() -> None:
    """PAUSE is a spawn-gate signal and must not rewrite model fields."""
    policy = BudgetPolicy.default()
    tasks = [_FakeTask(id="t1", model="opus")]
    result = apply_policy(policy, 0.85, tasks=tasks)
    assert result.action == BudgetAction.PAUSE
    assert tasks[0].model == "opus"


def test_apply_policy_downgrade_rewrites_task_model() -> None:
    """DOWNGRADE_MODEL mutates each task's model to the cheaper tier."""
    policy = BudgetPolicy.default()
    tasks = [
        _FakeTask(id="t1", model="opus"),
        _FakeTask(id="t2", model="sonnet"),
        _FakeTask(id="t3", model="haiku"),
    ]
    result = apply_policy(policy, 0.92, tasks=tasks)
    assert result.action == BudgetAction.DOWNGRADE_MODEL
    assert tasks[0].model == "sonnet"  # opus -> sonnet
    assert tasks[1].model == "haiku"  # sonnet -> haiku
    assert tasks[2].model == "haiku"  # already cheapest, unchanged


def test_apply_policy_downgrade_defaults_empty_model_to_cheapest() -> None:
    """Tasks with an unset model get the cheapest tier explicitly set."""
    policy = BudgetPolicy.default()
    tasks = [_FakeTask(id="t1", model="")]
    result = apply_policy(policy, 0.95, tasks=tasks)
    assert result.action == BudgetAction.DOWNGRADE_MODEL
    assert tasks[0].model == "haiku"


def test_apply_policy_abort_leaves_tasks_untouched() -> None:
    """ABORT is a spawn-stop signal; mutation is pointless and must not occur."""
    policy = BudgetPolicy.default()
    tasks = [_FakeTask(id="t1", model="opus")]
    result = apply_policy(policy, 1.05, tasks=tasks)
    assert result.action == BudgetAction.ABORT
    assert tasks[0].model == "opus"


def test_apply_policy_handles_none_tasks() -> None:
    """apply_policy without a task list is a pure evaluation."""
    policy = BudgetPolicy.default()
    result = apply_policy(policy, 0.92, tasks=None)
    assert result.action == BudgetAction.DOWNGRADE_MODEL


def test_apply_policy_custom_policy_switch_model_rule() -> None:
    """A single switch-model rule triggers downgrade at its threshold."""
    policy = BudgetPolicy(
        rules=[
            BudgetThresholdRule(
                threshold_pct=0.5,
                action=BudgetAction.DOWNGRADE_MODEL,
                message="Half-budget; switch model.",
            ),
        ]
    )
    tasks = [_FakeTask(id="t1", model="opus")]
    result = apply_policy(policy, 0.6, tasks=tasks)
    assert result.action == BudgetAction.DOWNGRADE_MODEL
    assert tasks[0].model == "sonnet"


def test_apply_policy_returns_result_with_metadata() -> None:
    """The returned BudgetActionResult carries threshold + spend data."""
    policy = BudgetPolicy.default()
    result = apply_policy(policy, 0.82, tasks=None)
    assert isinstance(result, BudgetActionResult)
    assert result.threshold_pct == pytest.approx(0.80)
    assert abs(result.percentage_used - 0.82) < 1e-9
    assert result.message != ""
