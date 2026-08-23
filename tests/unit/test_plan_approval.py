"""Unit tests for plan approval and persistence."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path

from bernstein.core.models import Complexity, PlanStatus, Scope, Task
from bernstein.core.plan_approval import PlanStore, _classify_risk, create_plan


def _task(
    task_id: str,
    *,
    title: str,
    description: str = "",
    role: str = "backend",
    scope: Scope = Scope.MEDIUM,
    complexity: Complexity = Complexity.MEDIUM,
    estimated_minutes: int = 30,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        description=description,
        role=role,
        scope=scope,
        complexity=complexity,
        estimated_minutes=estimated_minutes,
    )


def test_classify_risk_uses_keywords_role_and_scope() -> None:
    task = _task(
        "T-risk-1",
        title="Add auth migration for production database",
        role="security",
        scope=Scope.LARGE,
        complexity=Complexity.HIGH,
    )

    risk_level, reasons = _classify_risk(task)

    assert risk_level == "critical"
    assert any("Contains high-risk keywords" in reason for reason in reasons)
    assert any("High-risk role" in reason for reason in reasons)
    assert any(reason == "High complexity task" for reason in reasons)


def test_create_plan_aggregates_cost_time_and_high_risk_tasks() -> None:
    low = _task("T-plan-1", title="Refine docs", estimated_minutes=15, scope=Scope.SMALL, complexity=Complexity.LOW)
    high = _task(
        "T-plan-2",
        title="Rotate secrets",
        description="Update production credentials",
        role="security",
        estimated_minutes=90,
        scope=Scope.LARGE,
        complexity=Complexity.HIGH,
    )

    plan = create_plan("Improve release safety", [low, high])

    assert plan.goal == "Improve release safety"
    assert len(plan.task_estimates) == 2
    assert plan.total_estimated_minutes == 105
    assert plan.total_estimated_cost_usd > 0.0
    assert plan.high_risk_tasks == ["T-plan-2"]


def test_plan_store_round_trips_and_records_approval(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    store = PlanStore(sdd_dir)
    plan = create_plan("Ship a change", [_task("T-store-1", title="Implement feature")])

    store.save_plan(plan)
    reloaded = PlanStore(sdd_dir).get_plan(plan.id)
    approved = store.approve_plan(plan.id, "reviewed")

    assert reloaded is not None
    assert reloaded.id == plan.id
    assert approved is not None
    assert approved.status == PlanStatus.APPROVED
    assert approved.decision_reason == "reviewed"


def test_classify_risk_returns_low_for_safe_task() -> None:
    task = _task("T-risk-2", title="Update docs", description="Refresh examples")

    risk_level, reasons = _classify_risk(task)

    assert risk_level == "low"
    assert reasons == []


def test_plan_store_reject_flow_updates_status(tmp_path: Path) -> None:
    store = PlanStore(tmp_path / ".sdd")
    plan = create_plan("Do maintenance", [_task("T-store-2", title="Cleanup imports")])
    store.save_plan(plan)
    rejected = store.reject_plan(plan.id, "out of scope")

    assert rejected is not None
    assert rejected.status == PlanStatus.REJECTED
    assert rejected.decision_reason == "out of scope"


def test_plan_approval_panel_shows_configured_model_and_prices_free_route() -> None:
    """Issue #4214: Plan-approval panel shows configured model and prices free route at $0.00."""
    import io

    from bernstein.core.plan_approval import configure_plan_models
    from rich.console import Console

    from bernstein.cli.plan.plan_display import render_plan

    configure_plan_models(None, default_model="openrouter/cohere/north-mini-code:free")
    task = _task("T-free-1", title="Fix one-liner bug", role="manager")
    plan = create_plan("Fix bug", [task])

    assert plan.task_estimates[0].model == "openrouter/cohere/north-mini-code:free"
    assert plan.total_estimated_cost_usd == 0.0

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100)
    render_plan(plan, [task], console=console)
    output = buf.getvalue()

    assert "openrouter/cohere/north-mini-code:free" in output
    assert "$0.00" in output


def test_plan_approval_panel_shows_explicit_model_override() -> None:
    """Issue #4214: Plan-approval panel renders the specified --model override."""
    import io

    from bernstein.core.plan_approval import configure_plan_models
    from rich.console import Console

    from bernstein.cli.plan.plan_display import render_plan

    configure_plan_models(None, default_model="opus")
    task = _task("T-opus-1", title="Complex refactor", role="manager")
    plan = create_plan("Refactor", [task])

    assert plan.task_estimates[0].model == "opus"

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100)
    render_plan(plan, [task], console=console)
    output = buf.getvalue()

    assert "opus" in output


def test_plan_approval_panel_and_post_approval_line_agree_for_free_route() -> None:
    """Issue #4214: Panel cost estimate ($0.00) agrees with estimate_run_cost ($0.00-$0.00)."""
    from bernstein.core.plan_approval import configure_plan_models

    from bernstein.core.cost import estimate_run_cost

    model_name = "openrouter/cohere/north-mini-code:free"
    configure_plan_models(None, default_model=model_name)
    task = _task("T-agree-1", title="Quick fix", role="manager")
    plan = create_plan("Quick fix", [task])

    low_usd, high_usd = estimate_run_cost(1, model_name)

    assert plan.total_estimated_cost_usd == 0.0
    assert low_usd == 0.0
    assert high_usd == 0.0
