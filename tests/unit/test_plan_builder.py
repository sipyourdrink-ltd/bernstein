"""Unit tests for PlanBuilder.render_to_markdown()."""

from __future__ import annotations

import time

import pytest

from bernstein.core.models import (
    Complexity,
    PlanStatus,
    Scope,
    Task,
    TaskCostEstimate,
    TaskPlan,
    TaskStatus,
)
from bernstein.core.plan_builder import PlanBuilder, _fmt_cost, _fmt_minutes, _topological_order


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_plan(
    goal: str = "Build a widget",
    task_estimates: list[TaskCostEstimate] | None = None,
    total_cost: float = 0.42,
    total_minutes: int = 90,
    high_risk: list[str] | None = None,
    status: PlanStatus = PlanStatus.PENDING,
) -> TaskPlan:
    if task_estimates is None:
        task_estimates = []
    return TaskPlan(
        id="abc123",
        goal=goal,
        task_estimates=task_estimates,
        total_estimated_cost_usd=total_cost,
        total_estimated_minutes=total_minutes,
        high_risk_tasks=high_risk or [],
        status=status,
        created_at=1_000_000.0,
    )


def _make_estimate(
    task_id: str = "t1",
    title: str = "Do stuff",
    role: str = "backend",
    model: str = "sonnet",
    tokens: int = 80_000,
    cost: float = 0.40,
    risk: str = "low",
    risk_reasons: list[str] | None = None,
) -> TaskCostEstimate:
    return TaskCostEstimate(
        task_id=task_id,
        title=title,
        role=role,
        model=model,
        estimated_tokens=tokens,
        estimated_cost_usd=cost,
        risk_level=risk,  # type: ignore[arg-type]
        risk_reasons=risk_reasons or [],
    )


def _make_task(
    task_id: str = "t1",
    title: str = "Do stuff",
    role: str = "backend",
    model: str = "sonnet",
    effort: str | None = "high",
    scope: Scope = Scope.MEDIUM,
    estimated_minutes: int = 45,
    depends_on: list[str] | None = None,
    assigned_agent: str | None = None,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        description="",
        role=role,
        model=model,
        effort=effort,
        scope=scope,
        estimated_minutes=estimated_minutes,
        depends_on=depends_on or [],
        assigned_agent=assigned_agent,
        status=TaskStatus.PLANNED,
        complexity=Complexity.MEDIUM,
    )


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


def test_fmt_cost_small() -> None:
    assert _fmt_cost(0.001) == "$0.0010"


def test_fmt_cost_normal() -> None:
    assert _fmt_cost(1.234) == "$1.23"


def test_fmt_minutes_under_hour() -> None:
    assert _fmt_minutes(45) == "45m"


def test_fmt_minutes_exact_hour() -> None:
    assert _fmt_minutes(60) == "1h"


def test_fmt_minutes_over_hour() -> None:
    assert _fmt_minutes(90) == "1h 30m"


# ---------------------------------------------------------------------------
# Topological order tests
# ---------------------------------------------------------------------------


def test_topological_order_no_deps() -> None:
    tasks = [_make_task("a"), _make_task("b"), _make_task("c")]
    ordered = _topological_order(tasks)
    assert {t.id for t in ordered} == {"a", "b", "c"}


def test_topological_order_linear_chain() -> None:
    t1 = _make_task("t1")
    t2 = _make_task("t2", depends_on=["t1"])
    t3 = _make_task("t3", depends_on=["t2"])
    ordered = _topological_order([t3, t1, t2])  # input order intentionally scrambled
    ids = [t.id for t in ordered]
    assert ids.index("t1") < ids.index("t2")
    assert ids.index("t2") < ids.index("t3")


def test_topological_order_fan_in() -> None:
    t1 = _make_task("t1")
    t2 = _make_task("t2")
    t3 = _make_task("t3", depends_on=["t1", "t2"])
    ordered = _topological_order([t1, t2, t3])
    ids = [t.id for t in ordered]
    assert ids.index("t1") < ids.index("t3")
    assert ids.index("t2") < ids.index("t3")


def test_topological_order_cycle_does_not_crash() -> None:
    t1 = _make_task("t1", depends_on=["t2"])
    t2 = _make_task("t2", depends_on=["t1"])
    # Should not raise; remaining tasks appended at end
    ordered = _topological_order([t1, t2])
    assert len(ordered) == 2


# ---------------------------------------------------------------------------
# PlanBuilder.render_to_markdown() structure tests
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_plan() -> TaskPlan:
    est = _make_estimate(task_id="t1", title="Add widget", role="backend", model="sonnet", cost=0.40, tokens=80_000)
    return _make_plan(
        goal="Build a widget",
        task_estimates=[est],
        total_cost=0.40,
        total_minutes=45,
    )


def test_render_contains_plan_id(simple_plan: TaskPlan) -> None:
    md = PlanBuilder(simple_plan).render_to_markdown()
    assert "abc123" in md


def test_render_contains_goal(simple_plan: TaskPlan) -> None:
    md = PlanBuilder(simple_plan).render_to_markdown()
    assert "Build a widget" in md


def test_render_contains_status(simple_plan: TaskPlan) -> None:
    md = PlanBuilder(simple_plan).render_to_markdown()
    assert "pending" in md.lower()


def test_render_contains_summary_section(simple_plan: TaskPlan) -> None:
    md = PlanBuilder(simple_plan).render_to_markdown()
    assert "## Summary" in md
    assert "Total tasks" in md
    assert "Estimated cost" in md
    assert "Estimated time" in md


def test_render_contains_tasks_section(simple_plan: TaskPlan) -> None:
    md = PlanBuilder(simple_plan).render_to_markdown()
    assert "## Tasks" in md
    assert "Add widget" in md
    assert "backend" in md
    assert "sonnet" in md


def test_render_contains_cost_breakdown(simple_plan: TaskPlan) -> None:
    md = PlanBuilder(simple_plan).render_to_markdown()
    assert "## Cost Breakdown" in md
    assert "80,000" in md  # formatted tokens


def test_render_contains_dependency_order(simple_plan: TaskPlan) -> None:
    md = PlanBuilder(simple_plan).render_to_markdown()
    assert "## Dependency Order" in md


def test_render_contains_agent_assignments(simple_plan: TaskPlan) -> None:
    md = PlanBuilder(simple_plan).render_to_markdown()
    assert "## Agent Assignments" in md
    assert "backend" in md


def test_render_contains_footer_commands(simple_plan: TaskPlan) -> None:
    md = PlanBuilder(simple_plan).render_to_markdown()
    assert "bernstein plans approve abc123" in md
    assert "bernstein plans reject abc123" in md


def test_render_ends_with_newline(simple_plan: TaskPlan) -> None:
    md = PlanBuilder(simple_plan).render_to_markdown()
    assert md.endswith("\n")


# ---------------------------------------------------------------------------
# With Task objects provided
# ---------------------------------------------------------------------------


def test_render_with_tasks_shows_effort() -> None:
    est = _make_estimate(task_id="t1")
    plan = _make_plan(task_estimates=[est])
    task = _make_task(task_id="t1", effort="max")
    md = PlanBuilder(plan, tasks=[task]).render_to_markdown()
    assert "max" in md


def test_render_with_tasks_shows_scope() -> None:
    est = _make_estimate(task_id="t1")
    plan = _make_plan(task_estimates=[est])
    task = _make_task(task_id="t1", scope=Scope.LARGE)
    md = PlanBuilder(plan, tasks=[task]).render_to_markdown()
    assert "large" in md


def test_render_with_tasks_shows_depends_on() -> None:
    est1 = _make_estimate(task_id="t1", title="First")
    est2 = _make_estimate(task_id="t2", title="Second")
    plan = _make_plan(task_estimates=[est1, est2])
    task1 = _make_task(task_id="t1", title="First")
    task2 = _make_task(task_id="t2", title="Second", depends_on=["t1"])
    md = PlanBuilder(plan, tasks=[task1, task2]).render_to_markdown()
    assert "t1" in md
    # Dependency section should note t2 depends on t1
    dep_section = md[md.index("## Dependency Order") :]
    assert "t1" in dep_section
    assert "t2" in dep_section


def test_render_dependency_order_respects_topology() -> None:
    est1 = _make_estimate(task_id="t1", title="First")
    est2 = _make_estimate(task_id="t2", title="Second")
    plan = _make_plan(task_estimates=[est1, est2])
    task1 = _make_task(task_id="t1")
    task2 = _make_task(task_id="t2", depends_on=["t1"])
    md = PlanBuilder(plan, tasks=[task1, task2]).render_to_markdown()
    dep_section = md[md.index("## Dependency Order") :]
    assert dep_section.index("`t1`") < dep_section.index("`t2`")


def test_render_agent_assignment_shows_assigned_agent() -> None:
    est = _make_estimate(task_id="t1", role="qa")
    plan = _make_plan(task_estimates=[est])
    task = _make_task(task_id="t1", role="qa", assigned_agent="claude-code-v1")
    md = PlanBuilder(plan, tasks=[task]).render_to_markdown()
    assert "claude-code-v1" in md


def test_render_agent_assignment_unassigned_fallback() -> None:
    est = _make_estimate(task_id="t1", role="security")
    plan = _make_plan(task_estimates=[est])
    task = _make_task(task_id="t1", role="security", assigned_agent=None)
    md = PlanBuilder(plan, tasks=[task]).render_to_markdown()
    assert "unassigned" in md


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_render_empty_plan() -> None:
    plan = _make_plan(task_estimates=[], total_cost=0.0, total_minutes=0)
    md = PlanBuilder(plan).render_to_markdown()
    assert "## Summary" in md
    assert "No tasks" in md


def test_render_approved_plan_shows_approved_status() -> None:
    plan = _make_plan(status=PlanStatus.APPROVED)
    md = PlanBuilder(plan).render_to_markdown()
    assert "approved" in md.lower()


def test_render_decision_reason_included() -> None:
    plan = _make_plan()
    plan.decision_reason = "Looks good to me"
    md = PlanBuilder(plan).render_to_markdown()
    assert "Looks good to me" in md


def test_render_high_risk_task_shows_risk_icon() -> None:
    est = _make_estimate(task_id="t1", risk="critical", risk_reasons=["Contains auth keyword"])
    plan = _make_plan(task_estimates=[est], high_risk=["t1"])
    md = PlanBuilder(plan).render_to_markdown()
    assert "critical" in md
    assert "Contains auth keyword" in md


def test_render_multiple_tasks_cost_total() -> None:
    est1 = _make_estimate(task_id="t1", cost=0.10, tokens=10_000)
    est2 = _make_estimate(task_id="t2", cost=0.20, tokens=20_000)
    plan = _make_plan(task_estimates=[est1, est2], total_cost=0.30, total_minutes=60)
    md = PlanBuilder(plan).render_to_markdown()
    assert "$0.30" in md
    assert "30,000" in md  # 10k + 20k total tokens in breakdown
