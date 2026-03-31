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

    rejected = store.reject_plan(plan.id, "unsafe right now")

    assert rejected is not None
    assert rejected.status == PlanStatus.REJECTED
    assert rejected.decision_reason == "unsafe right now"
