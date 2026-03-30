"""Tests for the governed workflow module."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bernstein.core.models import Task, TaskStatus, WorkflowPhaseEvent
from bernstein.core.workflow import (
    GOVERNED_DEFAULT,
    WORKFLOW_REGISTRY,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowPhase,
    load_workflow,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    task_id: str,
    role: str = "backend",
    status: TaskStatus = TaskStatus.OPEN,
) -> Task:
    """Create a minimal task for testing."""
    return Task(
        id=task_id,
        title=f"Task {task_id}",
        description="test",
        role=role,
        status=status,
    )


# ---------------------------------------------------------------------------
# WorkflowPhase
# ---------------------------------------------------------------------------


class TestWorkflowPhase:
    def test_defaults(self) -> None:
        phase = WorkflowPhase(name="test")
        assert phase.name == "test"
        assert phase.allowed_roles == frozenset()
        assert TaskStatus.DONE in phase.completion_statuses
        assert TaskStatus.CANCELLED in phase.completion_statuses
        assert phase.requires_approval is False

    def test_with_roles(self) -> None:
        phase = WorkflowPhase(name="verify", allowed_roles=frozenset({"qa", "security"}))
        assert "qa" in phase.allowed_roles
        assert "backend" not in phase.allowed_roles


# ---------------------------------------------------------------------------
# WorkflowDefinition
# ---------------------------------------------------------------------------


class TestWorkflowDefinition:
    def test_governed_default_phases(self) -> None:
        names = GOVERNED_DEFAULT.phase_names()
        assert names == ["plan", "implement", "verify", "review", "merge"]

    def test_definition_hash_deterministic(self) -> None:
        h1 = GOVERNED_DEFAULT.definition_hash()
        h2 = GOVERNED_DEFAULT.definition_hash()
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_definition_hash_changes_on_modification(self) -> None:
        modified = WorkflowDefinition(
            name="governed",
            version="2.0.0",
            phases=GOVERNED_DEFAULT.phases,
        )
        assert modified.definition_hash() != GOVERNED_DEFAULT.definition_hash()

    def test_phase_names(self) -> None:
        defn = WorkflowDefinition(
            name="test",
            phases=(
                WorkflowPhase(name="a"),
                WorkflowPhase(name="b"),
            ),
        )
        assert defn.phase_names() == ["a", "b"]


# ---------------------------------------------------------------------------
# WorkflowExecutor
# ---------------------------------------------------------------------------


class TestWorkflowExecutor:
    @pytest.fixture()
    def sdd_dir(self, tmp_path: Path) -> Path:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        return sdd

    @pytest.fixture()
    def executor(self, sdd_dir: Path) -> WorkflowExecutor:
        return WorkflowExecutor(
            definition=GOVERNED_DEFAULT,
            run_id="test-run-001",
            sdd_dir=sdd_dir,
        )

    def test_initial_state(self, executor: WorkflowExecutor) -> None:
        assert executor.current_phase_name == "plan"
        assert executor.phase_index == 0
        assert executor.is_completed is False
        assert executor.approval_pending is False
        assert len(executor.events) == 1  # initial phase entry event

    def test_definition_hash_persisted(self, executor: WorkflowExecutor, sdd_dir: Path) -> None:
        hash_file = sdd_dir / "runtime" / "workflow" / "definition_hash.txt"
        assert hash_file.exists()
        assert hash_file.read_text() == executor.definition_hash

    def test_definition_json_persisted(self, executor: WorkflowExecutor, sdd_dir: Path) -> None:
        defn_file = sdd_dir / "runtime" / "workflow" / "definition.json"
        assert defn_file.exists()
        data = json.loads(defn_file.read_text())
        assert data["name"] == "governed"
        assert data["hash"] == executor.definition_hash
        assert data["phases"] == ["plan", "implement", "verify", "review", "merge"]

    def test_filter_tasks_plan_phase(self, executor: WorkflowExecutor) -> None:
        tasks = [
            _task("t1", role="manager"),
            _task("t2", role="backend"),
            _task("t3", role="architect"),
        ]
        filtered = executor.filter_tasks_for_current_phase(tasks)
        roles = {t.role for t in filtered}
        assert roles == {"manager", "architect"}

    def test_phase_not_complete_when_tasks_open(self, executor: WorkflowExecutor) -> None:
        tasks = [_task("t1", role="manager", status=TaskStatus.OPEN)]
        assert executor.phase_tasks_complete(tasks) is False

    def test_phase_complete_when_tasks_done(self, executor: WorkflowExecutor) -> None:
        tasks = [_task("t1", role="manager", status=TaskStatus.DONE)]
        assert executor.phase_tasks_complete(tasks) is True

    def test_phase_not_complete_with_no_tasks(self, executor: WorkflowExecutor) -> None:
        assert executor.phase_tasks_complete([]) is False

    def test_try_advance_blocked_until_complete(self, executor: WorkflowExecutor) -> None:
        tasks = [_task("t1", role="manager", status=TaskStatus.OPEN)]
        assert executor.try_advance(tasks) is None
        assert executor.current_phase_name == "plan"

    def test_try_advance_to_implement_needs_approval(self, executor: WorkflowExecutor) -> None:
        # Plan phase tasks complete
        tasks = [_task("t1", role="manager", status=TaskStatus.DONE)]
        # First call triggers phase completion, advances past plan (no approval)
        event = executor.try_advance(tasks)
        assert event is not None
        # Now in implement phase which requires approval
        assert executor.current_phase_name == "implement"

    def test_implement_phase_requires_approval(self, executor: WorkflowExecutor) -> None:
        # Advance past plan
        plan_tasks = [_task("t1", role="manager", status=TaskStatus.DONE)]
        executor.try_advance(plan_tasks)
        assert executor.current_phase_name == "implement"

        # Now all implement tasks done — triggers approval request
        impl_tasks = [
            _task("t1", role="manager", status=TaskStatus.DONE),
            _task("t2", role="backend", status=TaskStatus.DONE),
        ]
        result = executor.try_advance(impl_tasks)
        assert result is None  # blocked on approval
        assert executor.approval_pending is True

    def test_grant_approval_unblocks_advance(self, executor: WorkflowExecutor) -> None:
        # Advance to implement
        plan_tasks = [_task("t1", role="manager", status=TaskStatus.DONE)]
        executor.try_advance(plan_tasks)

        # Complete implement tasks — triggers approval
        impl_tasks = [
            _task("t1", role="manager", status=TaskStatus.DONE),
            _task("t2", role="backend", status=TaskStatus.DONE),
        ]
        executor.try_advance(impl_tasks)
        assert executor.approval_pending is True

        # Grant approval
        executor.grant_approval("tests look good")
        assert executor.approval_pending is False

        # Now try_advance should succeed
        event = executor.try_advance(impl_tasks)
        assert event is not None
        assert executor.current_phase_name == "verify"

    def test_full_workflow_progression(self, sdd_dir: Path) -> None:
        """Drive a complete 5-phase governed workflow to completion."""
        # Use a simpler workflow with no approval requirements
        defn = WorkflowDefinition(
            name="test-simple",
            phases=(
                WorkflowPhase(name="plan", allowed_roles=frozenset({"manager"})),
                WorkflowPhase(name="implement"),
                WorkflowPhase(name="verify", allowed_roles=frozenset({"qa"})),
            ),
        )
        executor = WorkflowExecutor(defn, run_id="test-full", sdd_dir=sdd_dir)

        # Phase 1: plan
        assert executor.current_phase_name == "plan"
        tasks = [_task("t1", role="manager", status=TaskStatus.DONE)]
        event = executor.try_advance(tasks)
        assert event is not None
        assert event.from_phase == "plan"
        assert event.to_phase == "implement"

        # Phase 2: implement
        assert executor.current_phase_name == "implement"
        tasks = [
            _task("t1", role="manager", status=TaskStatus.DONE),
            _task("t2", role="backend", status=TaskStatus.DONE),
        ]
        event = executor.try_advance(tasks)
        assert event is not None
        assert event.to_phase == "verify"

        # Phase 3: verify
        assert executor.current_phase_name == "verify"
        tasks = [
            _task("t1", role="manager", status=TaskStatus.DONE),
            _task("t2", role="backend", status=TaskStatus.DONE),
            _task("t3", role="qa", status=TaskStatus.DONE),
        ]
        event = executor.try_advance(tasks)
        assert event is not None
        assert event.to_phase == "completed"
        assert executor.is_completed is True

    def test_event_log_persisted(self, executor: WorkflowExecutor, sdd_dir: Path) -> None:
        log_path = sdd_dir / "runtime" / "workflow" / "events.jsonl"
        assert log_path.exists()
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) >= 1  # at least the initial event
        first = json.loads(lines[0])
        assert first["from_phase"] == ""
        assert first["to_phase"] == "plan"
        assert first["workflow_hash"] == executor.definition_hash

    def test_to_dict(self, executor: WorkflowExecutor) -> None:
        state = executor.to_dict()
        assert state["workflow_name"] == "governed"
        assert state["current_phase"] == "plan"
        assert state["phase_index"] == 0
        assert state["total_phases"] == 5
        assert state["completed"] is False
        assert state["workflow_hash"] == executor.definition_hash

    def test_completed_executor_returns_all_tasks(self, sdd_dir: Path) -> None:
        """Once workflow is done, filter_tasks_for_current_phase returns all tasks."""
        defn = WorkflowDefinition(
            name="minimal",
            phases=(WorkflowPhase(name="only", allowed_roles=frozenset({"manager"})),),
        )
        executor = WorkflowExecutor(defn, run_id="test-done", sdd_dir=sdd_dir)
        tasks = [_task("t1", role="manager", status=TaskStatus.DONE)]
        executor.try_advance(tasks)
        assert executor.is_completed is True

        # Now all tasks should pass through
        all_tasks = [_task("t2", role="backend"), _task("t3", role="qa")]
        assert len(executor.filter_tasks_for_current_phase(all_tasks)) == 2

    def test_approval_pending_file_written(self, executor: WorkflowExecutor, sdd_dir: Path) -> None:
        # Advance to implement (requires approval)
        plan_tasks = [_task("t1", role="manager", status=TaskStatus.DONE)]
        executor.try_advance(plan_tasks)

        # Complete implement tasks
        impl_tasks = [
            _task("t1", role="manager", status=TaskStatus.DONE),
            _task("t2", role="backend", status=TaskStatus.DONE),
        ]
        executor.try_advance(impl_tasks)
        assert executor.approval_pending is True

        pending_file = sdd_dir / "runtime" / "workflow" / "approval_pending_implement.json"
        assert pending_file.exists()
        data = json.loads(pending_file.read_text())
        assert data["phase"] == "implement"
        assert data["workflow"] == "governed"

    def test_cancelled_tasks_count_as_complete(self, sdd_dir: Path) -> None:
        defn = WorkflowDefinition(
            name="cancel-test",
            phases=(WorkflowPhase(name="only", allowed_roles=frozenset({"qa"})),),
        )
        executor = WorkflowExecutor(defn, run_id="test-cancel", sdd_dir=sdd_dir)
        tasks = [_task("t1", role="qa", status=TaskStatus.CANCELLED)]
        event = executor.try_advance(tasks)
        assert event is not None
        assert executor.is_completed is True


# ---------------------------------------------------------------------------
# WorkflowPhaseEvent model
# ---------------------------------------------------------------------------


class TestWorkflowPhaseEvent:
    def test_frozen(self) -> None:
        event = WorkflowPhaseEvent(
            timestamp=1.0,
            workflow_hash="abc",
            run_id="run-1",
            from_phase="plan",
            to_phase="implement",
        )
        with pytest.raises(AttributeError):
            event.from_phase = "other"  # type: ignore[misc]

    def test_fields(self) -> None:
        event = WorkflowPhaseEvent(
            timestamp=123.456,
            workflow_hash="deadbeef",
            run_id="run-2",
            from_phase="a",
            to_phase="b",
            reason="test",
            tasks_completed=("t1", "t2"),
        )
        assert event.timestamp == 123.456
        assert event.workflow_hash == "deadbeef"
        assert event.tasks_completed == ("t1", "t2")


# ---------------------------------------------------------------------------
# Registry / load_workflow
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_load_governed(self) -> None:
        defn = load_workflow("governed")
        assert defn is not None
        assert defn.name == "governed"

    def test_load_unknown_returns_none(self) -> None:
        assert load_workflow("nonexistent") is None

    def test_governed_in_registry(self) -> None:
        assert "governed" in WORKFLOW_REGISTRY
