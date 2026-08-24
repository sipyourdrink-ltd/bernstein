"""Deterministic plan rendering and SHA-256 hash tests (#3839 slice 3)."""

from __future__ import annotations

import pytest

from bernstein.core.planning.plan_rendering import PlanRendering, compute_plan_rendering
from bernstein.core.tasks.models import PlanStatus, TaskCostEstimate, TaskPlan

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plan(
    *,
    plan_id: str = "plan-001",
    goal: str = "Ship feature X",
    estimates: list[TaskCostEstimate] | None = None,
    total_cost: float = 1.5,
    total_minutes: int = 120,
    high_risk: list[str] | None = None,
) -> TaskPlan:
    if estimates is None:
        estimates = [
            TaskCostEstimate(
                task_id="t-1",
                title="Implement core",
                role="backend",
                model="sonnet",
                estimated_tokens=5000,
                estimated_cost_usd=0.50,
                risk_level="medium",
                risk_reasons=["touches auth"],
            ),
            TaskCostEstimate(
                task_id="t-2",
                title="Write tests",
                role="qa",
                model="haiku",
                estimated_tokens=3000,
                estimated_cost_usd=0.25,
                risk_level="low",
                risk_reasons=[],
            ),
        ]
    return TaskPlan(
        id=plan_id,
        goal=goal,
        task_estimates=estimates,
        total_estimated_cost_usd=total_cost,
        total_estimated_minutes=total_minutes,
        high_risk_tasks=high_risk or [],
        status=PlanStatus.PENDING,
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_inputs_produce_same_hash() -> None:
    plan = _make_plan()
    r1 = compute_plan_rendering(plan)
    r2 = compute_plan_rendering(plan)
    assert r1.rendering_hash == r2.rendering_hash
    assert r1.text == r2.text


def test_hash_is_64_hex_chars() -> None:
    plan = _make_plan()
    rendering = compute_plan_rendering(plan)
    assert len(rendering.rendering_hash) == 64
    int(rendering.rendering_hash, 16)  # must not raise


# ---------------------------------------------------------------------------
# Task-order stability
# ---------------------------------------------------------------------------


def test_task_order_does_not_affect_hash() -> None:
    est_a = TaskCostEstimate(
        task_id="t-a",
        title="A",
        role="backend",
        model="sonnet",
        estimated_tokens=1000,
        estimated_cost_usd=0.10,
        risk_level="low",
        risk_reasons=[],
    )
    est_b = TaskCostEstimate(
        task_id="t-b",
        title="B",
        role="qa",
        model="haiku",
        estimated_tokens=2000,
        estimated_cost_usd=0.20,
        risk_level="high",
        risk_reasons=["external dep"],
    )
    plan_fwd = _make_plan(estimates=[est_a, est_b])
    plan_rev = _make_plan(estimates=[est_b, est_a])
    r_fwd = compute_plan_rendering(plan_fwd)
    r_rev = compute_plan_rendering(plan_rev)
    assert r_fwd.rendering_hash == r_rev.rendering_hash
    assert r_fwd.text == r_rev.text


def test_risk_reason_sort_stability() -> None:
    est = TaskCostEstimate(
        task_id="t-1",
        title="T",
        role="r",
        model="m",
        estimated_tokens=0,
        estimated_cost_usd=0.0,
        risk_level="high",
        risk_reasons=["z-reason", "a-reason"],
    )
    plan = _make_plan(estimates=[est])
    rendering = compute_plan_rendering(plan)
    # Sorted in text
    assert "a-reason; z-reason" in rendering.text


# ---------------------------------------------------------------------------
# Journal head binding
# ---------------------------------------------------------------------------


def test_different_journal_head_produces_different_hash() -> None:
    plan = _make_plan()
    r1 = compute_plan_rendering(plan, journal_head="aaa")
    r2 = compute_plan_rendering(plan, journal_head="bbb")
    assert r1.rendering_hash != r2.rendering_hash
    assert "Journal head: aaa" in r1.text
    assert "Journal head: bbb" in r2.text


def test_no_journal_head_omits_line() -> None:
    plan = _make_plan()
    rendering = compute_plan_rendering(plan, journal_head=None)
    assert "Journal head" not in rendering.text
    assert rendering.journal_head is None


def test_with_journal_head_appears_in_text() -> None:
    plan = _make_plan()
    rendering = compute_plan_rendering(plan, journal_head="deadbeef")
    assert rendering.journal_head == "deadbeef"
    assert "Journal head: deadbeef" in rendering.text


# ---------------------------------------------------------------------------
# Empty plan edge case
# ---------------------------------------------------------------------------


def test_empty_plan_no_estimates() -> None:
    plan = _make_plan(estimates=[], total_cost=0.0, total_minutes=0, high_risk=[])
    rendering = compute_plan_rendering(plan)
    assert "Tasks:" in rendering.text
    assert rendering.rendering_hash
    # Hash should still be deterministic
    assert rendering.rendering_hash == compute_plan_rendering(plan).rendering_hash


def test_empty_plan_with_journal_head() -> None:
    plan = _make_plan(estimates=[], total_cost=0.0, total_minutes=0, high_risk=[])
    r1 = compute_plan_rendering(plan, journal_head="h1")
    r2 = compute_plan_rendering(plan, journal_head=None)
    assert r1.rendering_hash != r2.rendering_hash


# ---------------------------------------------------------------------------
# to_dict / from_dict round-trip
# ---------------------------------------------------------------------------


def test_roundtrip_to_dict_from_dict() -> None:
    plan = _make_plan()
    rendering = compute_plan_rendering(plan, journal_head="abc")
    d = rendering.to_dict()
    restored = PlanRendering.from_dict(d)
    assert restored == rendering


def test_roundtrip_without_journal_head() -> None:
    plan = _make_plan()
    rendering = compute_plan_rendering(plan)
    d = rendering.to_dict()
    restored = PlanRendering.from_dict(d)
    assert restored == rendering


# ---------------------------------------------------------------------------
# Content verification
# ---------------------------------------------------------------------------


def test_text_contains_plan_id_and_goal() -> None:
    plan = _make_plan(plan_id="p-42", goal="Fix login bug")
    rendering = compute_plan_rendering(plan)
    assert "Plan: p-42" in rendering.text
    assert "Goal: Fix login bug" in rendering.text


def test_text_contains_totals() -> None:
    plan = _make_plan(total_cost=3.14, total_minutes=90)
    rendering = compute_plan_rendering(plan)
    assert "$3.140000" in rendering.text
    assert "Total minutes: 90" in rendering.text


def test_text_contains_high_risk_tasks() -> None:
    plan = _make_plan(high_risk=["t-2", "t-1"])
    rendering = compute_plan_rendering(plan)
    # Sorted in text
    assert "['t-1', 't-2']" in rendering.text


def test_text_contains_rendering_hash() -> None:
    plan = _make_plan()
    rendering = compute_plan_rendering(plan)
    assert f"Rendering hash: {rendering.rendering_hash}" in rendering.text


def test_estimates_sorted_by_id_in_text() -> None:
    est_b = TaskCostEstimate(
        task_id="t-b",
        title="Second",
        role="qa",
        model="haiku",
        estimated_tokens=1000,
        estimated_cost_usd=0.10,
        risk_level="low",
        risk_reasons=[],
    )
    est_a = TaskCostEstimate(
        task_id="t-a",
        title="First",
        role="backend",
        model="sonnet",
        estimated_tokens=2000,
        estimated_cost_usd=0.20,
        risk_level="low",
        risk_reasons=[],
    )
    plan = _make_plan(estimates=[est_b, est_a])
    rendering = compute_plan_rendering(plan)
    idx_a = rendering.text.index("t-a:")
    idx_b = rendering.text.index("t-b:")
    assert idx_a < idx_b


# ---------------------------------------------------------------------------
# Frozen dataclass
# ---------------------------------------------------------------------------


def test_plan_rendering_is_frozen() -> None:
    r = PlanRendering(text="x", rendering_hash="y")
    with pytest.raises(AttributeError):
        r.text = "z"  # type: ignore[misc]
