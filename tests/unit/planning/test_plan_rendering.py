"""Deterministic plan rendering and SHA-256 hash tests (#3839 slice 3)."""

from __future__ import annotations

from dataclasses import fields, replace

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


# ---------------------------------------------------------------------------
# Per-field digest sensitivity (#4502)
# ---------------------------------------------------------------------------
#
# The determinism and ordering properties above prove the digest is *stable*.
# They say nothing about what it is stable *over*: dropping ``title`` from
# ``_estimate_dict`` passed this entire file before these cases existed, and a
# refactor that quietly narrows the hashed payload narrows what plan approval
# protects without anything going red.


# Each pair mutates exactly one field of a task estimate to a value that must
# move the digest. ``estimated_cost_usd`` is rounded to six places in the
# payload, so its mutation has to clear that precision to mean anything.
ESTIMATE_FIELD_MUTATIONS = [
    # "t-0" not "t-9": the estimates are sorted by task_id before
    # serialisation, so a mutation that reorders them would move the digest
    # through position alone and pass even with task_id out of the payload.
    ("task_id", "t-0"),
    ("title", "Implement something else"),
    ("role", "frontend"),
    ("model", "opus"),
    ("estimated_tokens", 5001),
    ("estimated_cost_usd", 0.51),
    ("risk_level", "high"),
    ("risk_reasons", ["touches billing"]),
]

# Plan-level fields the payload carries, and a mutation for each.
PLAN_FIELD_MUTATIONS = [
    ("plan_id", "plan-002"),
    ("goal", "Ship feature Y"),
    ("total_cost", 1.75),
    ("total_minutes", 121),
    ("high_risk", ["t-1"]),
]

# Lifecycle fields the payload deliberately omits. ``verify_rendering_hash``
# recomputes the digest at approve/reject time, so binding any of these would
# make verification fail at exactly the moment it runs.
UNHASHED_PLAN_FIELDS = frozenset({"status", "created_at", "decided_at", "decision_reason", "rendering_hash"})


def _digest(plan: TaskPlan) -> str:
    return compute_plan_rendering(plan).rendering_hash


@pytest.mark.parametrize(("field_name", "new_value"), ESTIMATE_FIELD_MUTATIONS)
def test_every_estimate_field_is_bound_into_the_digest(field_name: str, new_value: object) -> None:
    baseline = _make_plan()
    original = baseline.task_estimates[0]
    # A "mutation" that matches the baseline would pass whatever the payload
    # contained, which is the failure mode this whole sweep exists to catch.
    assert getattr(original, field_name) != new_value, f"{field_name} mutation does not change the value"

    mutated = replace(original, **{field_name: new_value})
    plan = _make_plan(estimates=[mutated, baseline.task_estimates[1]])

    assert _digest(plan) != _digest(baseline), (
        f"mutating {field_name} left the digest unchanged, so plan approval does not bind that field"
    )


def test_the_estimate_sweep_covers_every_field_of_the_dataclass() -> None:
    """A new field on TaskCostEstimate must fail here until someone decides.

    Without this, adding a field is silently a decision to leave it out of
    the digest, taken by whoever adds it and reviewed by nobody.
    """
    covered = {name for name, _ in ESTIMATE_FIELD_MUTATIONS}
    declared = {f.name for f in fields(TaskCostEstimate)}

    assert covered == declared, f"uncovered: {declared - covered}; stale: {covered - declared}"


@pytest.mark.parametrize(("kwarg", "new_value"), PLAN_FIELD_MUTATIONS)
def test_every_plan_level_field_is_bound_into_the_digest(kwarg: str, new_value: object) -> None:
    baseline = _make_plan()
    plan = _make_plan(**{kwarg: new_value})

    assert _digest(plan) != _digest(baseline), f"mutating {kwarg} left the digest unchanged"


@pytest.mark.parametrize("field_name", sorted(UNHASHED_PLAN_FIELDS))
def test_lifecycle_fields_stay_out_of_the_digest(field_name: str) -> None:
    """The digest has to survive the plan being approved.

    ``PlanStore.verify_rendering_hash`` recomputes it before promoting a
    plan, by which point status and the decision fields have moved. Binding
    them would refuse every plan at the gate they exist to protect.
    """
    baseline = _make_plan()
    moved = {
        "status": PlanStatus.APPROVED,
        "created_at": 1_700_000_000.0,
        "decided_at": 1_700_000_001.0,
        "decision_reason": "looks fine",
        "rendering_hash": "0" * 64,
    }[field_name]
    plan = replace(baseline, **{field_name: moved})

    assert _digest(plan) == _digest(baseline), (
        f"{field_name} moved the digest, so a plan cannot be verified after its own approval"
    )


def test_a_cost_change_below_the_rounding_precision_does_not_move_the_digest() -> None:
    """Six decimal places is the payload's stated precision, not an accident.

    Pinning it keeps the sensitivity sweep above honest: its cost mutation
    has to clear this threshold, and a future change to the rounding shows
    up here rather than as a mysteriously flaky sensitivity case.
    """
    baseline = _make_plan()
    original = baseline.task_estimates[0]
    nudged = replace(original, estimated_cost_usd=original.estimated_cost_usd + 1e-9)
    plan = _make_plan(estimates=[nudged, baseline.task_estimates[1]])

    assert _digest(plan) == _digest(baseline)


# ---------------------------------------------------------------------------
# Golden digest (#4502)
# ---------------------------------------------------------------------------


def _frozen_plan() -> TaskPlan:
    """A plan with every hashed field set to a literal.

    Deliberately not built from ``_make_plan``: the golden digest should move
    only when the payload format moves, and a helper other cases tune would
    make it move for reasons that have nothing to do with the format.
    """
    return TaskPlan(
        id="plan-golden",
        goal="Freeze the rendering payload",
        task_estimates=[
            TaskCostEstimate(
                task_id="t-a",
                title="First",
                role="backend",
                model="sonnet",
                estimated_tokens=1000,
                estimated_cost_usd=0.125,
                risk_level="low",
                risk_reasons=["one", "two"],
            ),
            TaskCostEstimate(
                task_id="t-b",
                title="Second",
                role="qa",
                model="haiku",
                estimated_tokens=2000,
                estimated_cost_usd=0.25,
                risk_level="high",
                risk_reasons=[],
            ),
        ],
        total_estimated_cost_usd=0.375,
        total_estimated_minutes=42,
        high_risk_tasks=["t-b"],
        status=PlanStatus.PENDING,
    )


GOLDEN_DIGEST = "eece8526b7ab3159251b471c5f7b048764e8bdf56be921b7b26c3763e5765081"


def test_the_frozen_plan_still_hashes_to_the_recorded_digest() -> None:
    """An accidental format change should be loud rather than merely different.

    Every stored ``rendering_hash`` predating the change stops verifying, so
    a payload edit is a migration. If this case fails on a deliberate one,
    update the constant in the same commit that changes the format.
    """
    assert _digest(_frozen_plan()) == GOLDEN_DIGEST
