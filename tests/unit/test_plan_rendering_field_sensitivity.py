"""Every hashed field must move the plan-rendering digest.

Covers ``compute_plan_rendering`` (``src/bernstein/core/planning/plan_rendering.py``).
The determinism and ordering properties are already well covered elsewhere;
what nothing pinned is *which fields the digest is sensitive to*. Dropping
``title`` from ``_estimate_dict`` passed the entire suite, and the route-level
tamper tests only ever mutate ``plan.goal``.

That matters because ``PlanStore.verify_rendering_hash`` re-derives this digest
before an approval is honoured (#3839). Whatever the payload omits is a field
an operator can change *after* review without the gate noticing - so a refactor
that quietly narrows the hashed payload silently narrows what approval
protects, and no test complains.

So each field is mutated one at a time and asserted to flip the digest. Two
boundaries get their own tests because a naive "mutate and compare" would get
them wrong in opposite directions:

* ``estimated_cost_usd`` is rounded to 6 decimal places on purpose, so a
  change below that precision *must not* move the hash - asserting otherwise
  would pin a bug;
* ``risk_reasons`` is sorted on purpose, so reordering must not move it while
  changing its contents must.
"""

from __future__ import annotations

import dataclasses

import pytest

from bernstein.core.planning.plan_rendering import compute_plan_rendering
from bernstein.core.tasks.models import PlanStatus, TaskCostEstimate, TaskPlan


def _estimate(**overrides: object) -> TaskCostEstimate:
    base = {
        "task_id": "t-1",
        "title": "Write the parser",
        "role": "backend",
        "model": "claude-opus-4",
        "estimated_tokens": 12_000,
        "estimated_cost_usd": 0.25,
        "risk_level": "medium",
        "risk_reasons": ["touches auth", "wide blast radius"],
    }
    base.update(overrides)
    return TaskCostEstimate(**base)  # type: ignore[arg-type]


def _plan(**overrides: object) -> TaskPlan:
    base = {
        "id": "plan-1",
        "goal": "Ship the importer",
        "task_estimates": [_estimate(), _estimate(task_id="t-2", title="Add tests")],
        "total_estimated_cost_usd": 0.5,
        "total_estimated_minutes": 90,
        "high_risk_tasks": ["t-2"],
    }
    base.update(overrides)
    return TaskPlan(**base)  # type: ignore[arg-type]


def _digest(plan: TaskPlan) -> str:
    return compute_plan_rendering(plan).rendering_hash


#: Every field of a task estimate, with a value that differs from the baseline.
ESTIMATE_MUTATIONS = [
    ("task_id", "t-99"),
    ("title", "Write the lexer"),
    ("role", "frontend"),
    ("model", "claude-sonnet-4"),
    ("estimated_tokens", 12_001),
    ("estimated_cost_usd", 0.26),
    ("risk_level", "high"),
    ("risk_reasons", ["touches auth", "wide blast radius", "no rollback"]),
]

#: Plan-level fields carried in the hashed payload.
PLAN_MUTATIONS = [
    ("id", "plan-2"),
    ("goal", "Ship the exporter"),
    ("total_estimated_cost_usd", 0.51),
    ("total_estimated_minutes", 91),
    ("high_risk_tasks", ["t-1", "t-2"]),
]


@pytest.mark.parametrize(("field", "value"), ESTIMATE_MUTATIONS, ids=[f for f, _ in ESTIMATE_MUTATIONS])
def test_estimate_field_moves_the_digest(field: str, value: object) -> None:
    baseline = _plan()
    mutated = _plan(
        task_estimates=[
            dataclasses.replace(baseline.task_estimates[0], **{field: value}),
            baseline.task_estimates[1],
        ]
    )

    assert _digest(mutated) != _digest(baseline), f"{field} is not covered by the approval digest"


@pytest.mark.parametrize(("field", "value"), PLAN_MUTATIONS, ids=[f for f, _ in PLAN_MUTATIONS])
def test_plan_field_moves_the_digest(field: str, value: object) -> None:
    baseline = _plan()
    mutated = _plan(**{field: value})

    assert _digest(mutated) != _digest(baseline), f"{field} is not covered by the approval digest"


def test_every_estimate_field_is_exercised() -> None:
    """The parametrisation must not drift behind the dataclass.

    Without this, adding a field to ``TaskCostEstimate`` and forgetting to hash
    it would leave the suite green - the same silence this file exists to end.
    """
    covered = {name for name, _ in ESTIMATE_MUTATIONS}
    declared = {f.name for f in dataclasses.fields(TaskCostEstimate)}

    assert declared == covered, f"unexercised estimate fields: {declared - covered}"


LIFECYCLE_FIELDS = [
    ("status", PlanStatus.APPROVED),
    ("created_at", 1.0),
    ("decided_at", 2.0),
    ("decision_reason", "looks fine"),
    ("rendering_hash", "sha256:tampered"),
]


@pytest.mark.parametrize(("field", "value"), LIFECYCLE_FIELDS, ids=[f for f, _ in LIFECYCLE_FIELDS])
def test_lifecycle_fields_stay_out_of_the_digest(field: str, value: object) -> None:
    """The digest has to survive the plan being approved.

    Approval writes ``status``, ``decided_at``, ``decision_reason`` and
    ``rendering_hash`` onto the plan; if any of them fed the hash, the act of
    deciding would invalidate the digest the decision just bound to.
    """
    baseline = _plan()
    mutated = _plan(**{field: value})

    assert _digest(mutated) == _digest(baseline), f"lifecycle field {field} leaked into the digest"


def test_every_plan_field_is_either_hashed_or_lifecycle() -> None:
    """Every ``TaskPlan`` field is claimed by exactly one of the two suites.

    A new field must be added either to ``PLAN_MUTATIONS`` (hashed) or to
    ``LIFECYCLE_FIELDS`` (excluded) - forgetting both leaves it silently
    unpinned, which is the gap this file exists to close.
    """
    # task_estimates is hashed too - its per-field sweep is the ESTIMATE suite.
    hashed = {name for name, _ in PLAN_MUTATIONS} | {"task_estimates"}
    lifecycle = {name for name, _ in LIFECYCLE_FIELDS}
    declared = {f.name for f in dataclasses.fields(TaskPlan)}

    assert not (hashed & lifecycle), f"claimed by both: {hashed & lifecycle}"
    assert declared == hashed | lifecycle, f"unclaimed plan fields: {declared - hashed - lifecycle}"


def test_cost_change_below_rounding_precision_is_ignored() -> None:
    """``estimated_cost_usd`` is rounded to 6dp deliberately.

    Asserting the digest moved here would pin a bug, not a property: sub-micro-
    dollar noise must not invalidate an approval.
    """
    baseline = _plan()
    nudged = _plan(
        task_estimates=[
            dataclasses.replace(baseline.task_estimates[0], estimated_cost_usd=0.25 + 1e-9),
            baseline.task_estimates[1],
        ]
    )

    assert _digest(nudged) == _digest(baseline)


def test_cost_change_at_rounding_precision_is_caught() -> None:
    """The other side of the same boundary: 1e-6 is inside the hashed value."""
    baseline = _plan()
    nudged = _plan(
        task_estimates=[
            dataclasses.replace(baseline.task_estimates[0], estimated_cost_usd=0.25 + 1e-6),
            baseline.task_estimates[1],
        ]
    )

    assert _digest(nudged) != _digest(baseline)


def test_reordering_risk_reasons_does_not_move_the_digest() -> None:
    """``risk_reasons`` is sorted before hashing, so order is not content."""
    baseline = _plan()
    reordered = _plan(
        task_estimates=[
            dataclasses.replace(
                baseline.task_estimates[0],
                risk_reasons=list(reversed(baseline.task_estimates[0].risk_reasons)),
            ),
            baseline.task_estimates[1],
        ]
    )

    assert _digest(reordered) == _digest(baseline)


def test_reordering_tasks_does_not_move_the_digest() -> None:
    """Order-invariance is the documented contract; pinned alongside the rest."""
    baseline = _plan()
    reordered = _plan(task_estimates=list(reversed(baseline.task_estimates)))

    assert _digest(reordered) == _digest(baseline)


def test_journal_head_moves_the_digest() -> None:
    """The optional binding must not be silently droppable either."""
    plan = _plan()

    assert compute_plan_rendering(plan, journal_head="sha256:abc").rendering_hash != _digest(plan)


def test_golden_digest_of_a_frozen_plan() -> None:
    """An accidental payload or format change should be loud, not inferred.

    If this value changes, the hashed payload changed. That is sometimes
    correct - but it invalidates every stored approval digest, so it must be a
    decision someone made, not a side effect they discover later.
    """
    assert _digest(_plan()) == "1a5f0fe29fd0b0cec6b52cd895070786870df0fe442839f4ee8609291ea904e9"
