"""Tests for budget enforcement actions (COST-005)."""

from __future__ import annotations

from bernstein.core.budget_actions import (
    BudgetAction,
    BudgetActionResult,
    BudgetPolicy,
    BudgetThresholdRule,
    suggest_downgrade,
)


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
    assert d["threshold_pct"] == 0.8
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
