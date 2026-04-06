"""TEST-018: Test data generators for realistic task payloads.

Factory functions that produce realistic Task, TaskCreate, and
related objects for use in tests throughout the suite.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest

from bernstein.core.models import (
    AgentSession,
    Complexity,
    CompletionSignal,
    RiskAssessment,
    RollbackPlan,
    Scope,
    Task,
    TaskStatus,
    TaskType,
    UpgradeProposalDetails,
)

# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def make_task(
    *,
    id: str | None = None,
    title: str = "Test task",
    description: str = "A test task for unit testing",
    role: str = "backend",
    priority: int = 2,
    scope: Scope = Scope.MEDIUM,
    complexity: Complexity = Complexity.MEDIUM,
    status: TaskStatus = TaskStatus.OPEN,
    task_type: TaskType = TaskType.STANDARD,
    depends_on: list[str] | None = None,
    owned_files: list[str] | None = None,
    tenant_id: str = "default",
    assigned_agent: str | None = None,
    parent_task_id: str | None = None,
    completion_signals: list[CompletionSignal] | None = None,
    batch_eligible: bool | None = None,
    created_at: float | None = None,
) -> Task:
    """Create a realistic Task with sensible defaults.

    All parameters are optional; override only what matters for your test.
    """
    return Task(
        id=id or f"task-{uuid.uuid4().hex[:8]}",
        title=title,
        description=description,
        role=role,
        priority=priority,
        scope=scope,
        complexity=complexity,
        status=status,
        task_type=task_type,
        depends_on=depends_on or [],
        owned_files=owned_files or [],
        tenant_id=tenant_id,
        assigned_agent=assigned_agent,
        parent_task_id=parent_task_id,
        completion_signals=completion_signals or [],
        batch_eligible=batch_eligible,
        created_at=created_at or time.time(),
    )


def make_task_create_dict(
    *,
    title: str = "Create-test task",
    description: str = "Created via factory",
    role: str = "backend",
    priority: int = 2,
    scope: str = "medium",
    complexity: str = "medium",
    depends_on: list[str] | None = None,
    owned_files: list[str] | None = None,
    task_type: str = "standard",
    tenant_id: str = "default",
    model: str | None = None,
    effort: str | None = None,
    batch_eligible: bool = False,
) -> dict[str, Any]:
    """Create a dict suitable for TaskCreate.model_validate() or POST /tasks."""
    return {
        "title": title,
        "description": description,
        "role": role,
        "priority": priority,
        "scope": scope,
        "complexity": complexity,
        "depends_on": depends_on or [],
        "owned_files": owned_files or [],
        "task_type": task_type,
        "tenant_id": tenant_id,
        "model": model,
        "effort": effort,
        "batch_eligible": batch_eligible,
    }


def make_upgrade_proposal(
    *,
    title: str = "Upgrade logging",
    risk_level: str = "medium",
    breaking: bool = False,
) -> Task:
    """Create a task with upgrade proposal details."""
    return Task(
        id=f"upgrade-{uuid.uuid4().hex[:8]}",
        title=title,
        description="Upgrade proposal task",
        role="backend",
        task_type=TaskType.UPGRADE_PROPOSAL,
        upgrade_details=UpgradeProposalDetails(
            current_state="Uses print() for logging",
            proposed_change="Switch to structured logging with JSON output",
            benefits=["Better observability", "Easier log parsing"],
            risk_assessment=RiskAssessment(
                level=risk_level,  # type: ignore[arg-type]
                breaking_changes=breaking,
                affected_components=["core.orchestrator", "core.spawner"],
                mitigation="Feature flag for gradual rollout",
            ),
            rollback_plan=RollbackPlan(
                steps=["Revert commit", "Restart servers"],
                estimated_rollback_minutes=15,
            ),
            cost_estimate_usd=0.5,
            performance_impact="Negligible",
        ),
    )


def make_task_batch(
    n: int = 5,
    *,
    role: str = "backend",
    status: TaskStatus = TaskStatus.OPEN,
) -> list[Task]:
    """Create a batch of N tasks with sequential titles."""
    return [
        make_task(
            title=f"Batch task {i + 1}/{n}",
            description=f"Task {i + 1} of {n} in batch",
            role=role,
            status=status,
            priority=(i % 3) + 1,
        )
        for i in range(n)
    ]


def make_completion_signals() -> list[CompletionSignal]:
    """Create a set of typical completion signals."""
    return [
        CompletionSignal(type="path_exists", value="src/new_feature.py"),
        CompletionSignal(type="test_passes", value="pytest tests/unit/test_new_feature.py -x"),
        CompletionSignal(type="file_contains", value="src/new_feature.py::class NewFeature"),
    ]


def make_task_from_dict_raw(
    *,
    status: str = "open",
    role: str = "backend",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a raw dict suitable for Task.from_dict()."""
    base: dict[str, Any] = {
        "id": f"raw-{uuid.uuid4().hex[:8]}",
        "title": "Raw task",
        "description": "Created from raw dict",
        "role": role,
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
        "status": status,
        "depends_on": [],
        "owned_files": [],
        "assigned_agent": None,
        "result_summary": None,
        "tenant_id": "default",
        "task_type": "standard",
    }
    if extra:
        base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Tests that the factories themselves work correctly
# ---------------------------------------------------------------------------


class TestMakeTask:
    """Verify make_task produces valid Task objects."""

    def test_default_task(self) -> None:
        t = make_task()
        assert t.id.startswith("task-")
        assert t.status == TaskStatus.OPEN
        assert t.role == "backend"
        assert t.priority == 2

    def test_custom_fields(self) -> None:
        t = make_task(role="qa", priority=1, status=TaskStatus.CLAIMED)
        assert t.role == "qa"
        assert t.priority == 1
        assert t.status == TaskStatus.CLAIMED

    def test_unique_ids(self) -> None:
        ids = {make_task().id for _ in range(20)}
        assert len(ids) == 20

    def test_with_dependencies(self) -> None:
        t = make_task(depends_on=["task-a", "task-b"])
        assert t.depends_on == ["task-a", "task-b"]

    def test_with_completion_signals(self) -> None:
        signals = make_completion_signals()
        t = make_task(completion_signals=signals)
        assert len(t.completion_signals) == 3
        assert t.completion_signals[0].type == "path_exists"


class TestMakeTaskCreateDict:
    """Verify make_task_create_dict produces valid dicts for TaskCreate."""

    def test_validates_with_pydantic(self) -> None:
        from bernstein.core.server import TaskCreate

        data = make_task_create_dict()
        tc = TaskCreate.model_validate(data)
        assert tc.title == "Create-test task"

    def test_custom_fields(self) -> None:
        data = make_task_create_dict(role="security", priority=1, model="opus")
        assert data["role"] == "security"
        assert data["priority"] == 1
        assert data["model"] == "opus"


class TestMakeUpgradeProposal:
    """Verify make_upgrade_proposal produces valid upgrade tasks."""

    def test_has_upgrade_details(self) -> None:
        t = make_upgrade_proposal()
        assert t.task_type == TaskType.UPGRADE_PROPOSAL
        assert t.upgrade_details is not None
        assert t.upgrade_details.risk_assessment.level == "medium"

    def test_breaking_flag(self) -> None:
        t = make_upgrade_proposal(breaking=True)
        assert t.upgrade_details is not None
        assert t.upgrade_details.risk_assessment.breaking_changes is True


class TestMakeTaskBatch:
    """Verify make_task_batch produces correct batches."""

    def test_correct_count(self) -> None:
        batch = make_task_batch(10)
        assert len(batch) == 10

    def test_unique_ids(self) -> None:
        batch = make_task_batch(10)
        ids = {t.id for t in batch}
        assert len(ids) == 10

    def test_priority_distribution(self) -> None:
        batch = make_task_batch(6)
        priorities = [t.priority for t in batch]
        # Priorities cycle: 1, 2, 3, 1, 2, 3
        assert priorities == [1, 2, 3, 1, 2, 3]

    def test_custom_role(self) -> None:
        batch = make_task_batch(3, role="qa")
        assert all(t.role == "qa" for t in batch)

    def test_custom_status(self) -> None:
        batch = make_task_batch(3, status=TaskStatus.CLAIMED)
        assert all(t.status == TaskStatus.CLAIMED for t in batch)


class TestMakeTaskFromDictRaw:
    """Verify make_task_from_dict_raw produces valid raw dicts."""

    def test_task_from_dict(self) -> None:
        raw = make_task_from_dict_raw()
        task = Task.from_dict(raw)
        assert task.status == TaskStatus.OPEN

    def test_extra_fields(self) -> None:
        raw = make_task_from_dict_raw(extra={"model": "opus", "effort": "max"})
        task = Task.from_dict(raw)
        assert task.model == "opus"
        assert task.effort == "max"

    def test_various_statuses(self) -> None:
        for s in ["open", "claimed", "done", "failed"]:
            raw = make_task_from_dict_raw(status=s)
            task = Task.from_dict(raw)
            assert task.status.value == s
