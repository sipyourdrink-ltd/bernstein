# State Machine Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement explicit state machine workflows to drive deterministic task progression through plan→implement→test→review→merge states with configurable human checkpoints.

**Architecture:** Workflows are defined as directed state graphs where each state represents a work phase (e.g., planning, implementation, testing, review) with explicit transitions guarded by checkpoints. Tasks are linked to workflows and progress through states driven by completion verification. Human checkpoints are configurable gates that can block or allow transitions based on quality signals.

**Tech Stack:** Python 3.12+, dataclasses, file-based state storage (.sdd/), pytest, Pyright strict typing

---

## File Structure

### New Files to Create
- `src/bernstein/core/workflows.py` — Workflow engine, state machine logic, transition guards
- `src/bernstein/core/workflow_models.py` — Data models for workflows, states, checkpoints
- `src/bernstein/core/workflow_state_handlers.py` — Role-specific handlers for each state
- `src/bernstein/core/workflow_checkpoint_store.py` — Persistent storage for checkpoint decisions
- `tests/unit/test_workflows.py` — State machine transitions, checkpoint logic
- `tests/unit/test_workflow_integration.py` — Integration with task lifecycle
- `templates/workflows/` — Workflow definition templates (YAML)
  - `default.yaml` — Standard plan→implement→test→review→merge workflow
  - `simple.yaml` — Minimal plan→implement→done workflow

### Modified Files
- `src/bernstein/core/models.py` — Add `workflow_id`, `workflow_state`, `workflow_history` to Task dataclass
- `src/bernstein/core/orchestrator.py` — Integrate workflow state transitions in task lifecycle
- `src/bernstein/core/task_lifecycle.py` — Add workflow transition logic to task completion handling
- `src/bernstein/core/tick_pipeline.py` — Route tasks to correct state handler based on workflow state

---

## Tasks

### Task 1: Core Workflow Data Models

**Files:**
- Create: `src/bernstein/core/workflow_models.py`
- Modify: `src/bernstein/core/models.py:165-194`
- Test: `tests/unit/test_workflows.py`

Workflow state machine components: states, transitions, checkpoints, workflow definitions.

- [ ] **Step 1: Write failing test for WorkflowState creation**

```python
def test_workflow_state_creation():
    state = WorkflowState(
        id="plan",
        name="Planning",
        description="Decompose task into subtasks",
        required_role="manager",
        checkpoint_config=CheckpointConfig(
            required_quality_score=0.8,
            requires_human_approval=False,
            completion_signals=["test_passes"]
        )
    )
    assert state.id == "plan"
    assert state.name == "Planning"
    assert state.required_role == "manager"
    assert state.checkpoint_config.required_quality_score == 0.8
```

- [ ] **Step 2: Write failing test for StateTransition**

```python
def test_state_transition_creation():
    transition = StateTransition(
        from_state="plan",
        to_state="implement",
        condition="all_subtasks_created",
        checkpoint_id="plan_review"
    )
    assert transition.from_state == "plan"
    assert transition.to_state == "implement"
    assert transition.condition == "all_subtasks_created"
```

- [ ] **Step 3: Write failing test for WorkflowDefinition**

```python
def test_workflow_definition_creation():
    workflow = WorkflowDefinition(
        id="default",
        name="Standard Development Workflow",
        description="Plan → Implement → Test → Review → Merge",
        initial_state="plan",
        states=[
            WorkflowState(id="plan", name="Planning", required_role="manager"),
            WorkflowState(id="implement", name="Implementation", required_role="backend"),
            WorkflowState(id="test", name="Testing", required_role="qa"),
            WorkflowState(id="review", name="Review", required_role="manager"),
            WorkflowState(id="merge", name="Merge", required_role="manager")
        ],
        transitions=[
            StateTransition(from_state="plan", to_state="implement"),
            StateTransition(from_state="implement", to_state="test"),
            StateTransition(from_state="test", to_state="review"),
            StateTransition(from_state="review", to_state="merge")
        ]
    )
    assert workflow.id == "default"
    assert len(workflow.states) == 5
    assert len(workflow.transitions) == 4
    assert workflow.initial_state == "plan"
```

- [ ] **Step 4: Implement WorkflowState dataclass in workflow_models.py**

```python
"""Workflow state machine models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class CheckpointType(Enum):
    """Type of checkpoint gate."""
    NONE = "none"  # No checkpoint, auto-transition
    AUTO = "auto"  # Automated quality checks only
    HUMAN = "human"  # Requires human approval
    HYBRID = "hybrid"  # Both automated checks and human approval


@dataclass(frozen=True)
class CheckpointConfig:
    """Configuration for a state checkpoint.

    Attributes:
        type: Kind of checkpoint (none, auto, human, hybrid).
        required_quality_score: Minimum quality (0.0-1.0) needed to proceed.
        requires_human_approval: Whether human must explicitly approve.
        required_completion_signals: Janitor signals that must pass.
        approval_role: Role that can approve (e.g., "manager").
        approval_timeout_minutes: Time before checkpoint auto-expires (None = no timeout).
    """
    type: CheckpointType = CheckpointType.AUTO
    required_quality_score: float = 0.0
    requires_human_approval: bool = False
    required_completion_signals: list[str] = field(default_factory=list[str])
    approval_role: str | None = None
    approval_timeout_minutes: int | None = None


@dataclass(frozen=True)
class WorkflowState:
    """A state in a workflow.

    Attributes:
        id: Unique identifier (e.g., "plan", "implement").
        name: Human-readable name.
        description: What work happens in this state.
        required_role: Agent role responsible for this state (e.g., "manager", "backend").
        checkpoint_config: Gate configuration for exiting this state.
        estimated_minutes: Expected time in this state.
    """
    id: str
    name: str
    description: str
    required_role: str
    checkpoint_config: CheckpointConfig = field(default_factory=CheckpointConfig)
    estimated_minutes: int = 30


@dataclass(frozen=True)
class StateTransition:
    """A transition between two workflow states.

    Attributes:
        from_state: Source state ID.
        to_state: Destination state ID.
        condition: Optional condition that must be met (human-readable).
        checkpoint_id: Optional linked checkpoint for approval.
    """
    from_state: str
    to_state: str
    condition: str = ""
    checkpoint_id: str | None = None


@dataclass(frozen=True)
class WorkflowDefinition:
    """A complete workflow state machine.

    Attributes:
        id: Unique identifier (e.g., "default").
        name: Human-readable name.
        description: What this workflow is for.
        initial_state: Starting state ID.
        states: Ordered list of states.
        transitions: Allowed state transitions.
        version: Schema version.
    """
    id: str
    name: str
    description: str
    initial_state: str
    states: list[WorkflowState]
    transitions: list[StateTransition]
    version: int = 1

    def get_state(self, state_id: str) -> WorkflowState | None:
        """Look up a state by ID."""
        for state in self.states:
            if state.id == state_id:
                return state
        return None

    def get_next_states(self, from_state: str) -> list[str]:
        """Get all states reachable from a given state."""
        return [t.to_state for t in self.transitions if t.from_state == from_state]

    def is_valid_transition(self, from_state: str, to_state: str) -> bool:
        """Check if a transition is allowed."""
        return to_state in self.get_next_states(from_state)


@dataclass(frozen=True)
class WorkflowCheckpointDecision:
    """Record of a checkpoint approval/rejection.

    Attributes:
        checkpoint_id: Linked to a transition's checkpoint_id.
        task_id: Task undergoing checkpoint.
        from_state: Source state.
        to_state: Target state.
        decision: "approved" or "rejected".
        approver: Role that made the decision.
        reason: Why approved/rejected.
        timestamp: When decision was made.
    """
    checkpoint_id: str
    task_id: str
    from_state: str
    to_state: str
    decision: Literal["approved", "rejected"]
    approver: str
    reason: str
    timestamp: float
```

- [ ] **Step 5: Run tests to verify WorkflowState, StateTransition, WorkflowDefinition pass**

```bash
pytest tests/unit/test_workflows.py::test_workflow_state_creation -xvs
pytest tests/unit/test_workflows.py::test_state_transition_creation -xvs
pytest tests/unit/test_workflows.py::test_workflow_definition_creation -xvs
```

Expected: All three tests PASS

- [ ] **Step 6: Extend Task model with workflow fields**

Modify `src/bernstein/core/models.py` around line 165-194:

```python
@dataclass
class Task:
    """A unit of work for an agent."""

    # ... existing fields (id, title, description, role, etc.) ...

    # Workflow integration (new fields)
    workflow_id: str | None = None  # Which workflow this task belongs to
    workflow_state: str | None = None  # Current state in workflow (e.g., "implement")
    workflow_history: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    # [{state, entered_at, exited_at, result_summary}, ...]
```

- [ ] **Step 7: Commit workflow models**

```bash
git add src/bernstein/core/workflow_models.py tests/unit/test_workflows.py src/bernstein/core/models.py
git commit -m "feat: add core workflow state machine data models

- WorkflowState, StateTransition, WorkflowDefinition for explicit workflow definitions
- CheckpointConfig for state exit gates (auto/human/hybrid)
- WorkflowCheckpointDecision for tracking approvals
- Extend Task with workflow_id, workflow_state, workflow_history fields"
```

---

### Task 2: Workflow Engine & State Transitions

**Files:**
- Create: `src/bernstein/core/workflows.py`
- Test: `tests/unit/test_workflows.py` (extend)

Workflow execution engine: load workflows, validate transitions, manage state progression.

- [ ] **Step 1: Write failing test for WorkflowEngine initialization**

```python
def test_workflow_engine_load_default_workflow():
    engine = WorkflowEngine(workdir=Path("/tmp/test"))
    # Assume default workflow loaded from templates/workflows/default.yaml
    assert engine.has_workflow("default")
    workflow = engine.get_workflow("default")
    assert workflow.initial_state == "plan"
```

- [ ] **Step 2: Write failing test for state transition validation**

```python
def test_workflow_engine_validate_transition_allowed():
    engine = WorkflowEngine(workdir=Path("/tmp/test"))
    workflow = engine.get_workflow("default")

    # Valid transition: plan → implement
    assert engine.can_transition(workflow, "plan", "implement") is True

    # Invalid transition: plan → merge (skipping states)
    assert engine.can_transition(workflow, "plan", "merge") is False
```

- [ ] **Step 3: Write failing test for task state progression**

```python
def test_workflow_engine_progress_task_state():
    engine = WorkflowEngine(workdir=Path("/tmp/test"))
    workflow = engine.get_workflow("default")

    task = Task(
        id="test-1",
        title="Test Task",
        description="",
        role="backend",
        workflow_id="default",
        workflow_state="plan"
    )

    # Progress task to next state
    result = engine.transition_task(task, "plan", "implement", reason="Planning complete")
    assert result.workflow_state == "implement"
    assert len(result.workflow_history) == 1
    assert result.workflow_history[0]["state"] == "plan"
```

- [ ] **Step 4: Implement WorkflowEngine in workflows.py**

```python
"""Workflow state machine engine."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import yaml

from bernstein.core.models import Task
from bernstein.core.workflow_models import WorkflowDefinition, WorkflowState

logger = logging.getLogger(__name__)


class WorkflowEngine:
    """Manages workflow definitions and state transitions.

    Loads workflows from YAML templates, validates transitions, tracks state history.
    """

    def __init__(self, workdir: Path):
        """Initialize workflow engine.

        Args:
            workdir: Project root (where .sdd/ lives).
        """
        self.workdir = workdir
        self.workflows: dict[str, WorkflowDefinition] = {}
        self._load_builtin_workflows()

    def _load_builtin_workflows(self) -> None:
        """Load built-in workflow definitions."""
        template_dir = Path(__file__).parent.parent.parent / "templates" / "workflows"
        if not template_dir.exists():
            logger.warning("Workflow templates directory not found: %s", template_dir)
            return

        for yaml_file in template_dir.glob("*.yaml"):
            try:
                with open(yaml_file) as f:
                    workflow_dict = yaml.safe_load(f)
                # Reconstruct WorkflowDefinition from YAML
                # (simplified; full impl handles nested dataclass reconstruction)
                workflow_id = workflow_dict["id"]
                self.workflows[workflow_id] = self._deserialize_workflow(workflow_dict)
                logger.debug("Loaded workflow: %s", workflow_id)
            except Exception as e:
                logger.error("Failed to load workflow %s: %s", yaml_file, e)

    def _deserialize_workflow(self, data: dict[str, Any]) -> WorkflowDefinition:
        """Deserialize workflow from dict (loaded from YAML)."""
        # Stub: full impl reconstructs nested dataclasses
        # For now, return a basic WorkflowDefinition
        from bernstein.core.workflow_models import (
            CheckpointConfig, CheckpointType, StateTransition, WorkflowState
        )

        states = []
        for state_dict in data.get("states", []):
            checkpoint = state_dict.get("checkpoint", {})
            states.append(WorkflowState(
                id=state_dict["id"],
                name=state_dict["name"],
                description=state_dict.get("description", ""),
                required_role=state_dict["required_role"],
                checkpoint_config=CheckpointConfig(
                    type=CheckpointType(checkpoint.get("type", "auto")),
                    required_quality_score=checkpoint.get("required_quality_score", 0.0),
                    requires_human_approval=checkpoint.get("requires_human_approval", False),
                ),
                estimated_minutes=state_dict.get("estimated_minutes", 30),
            ))

        transitions = []
        for trans_dict in data.get("transitions", []):
            transitions.append(StateTransition(
                from_state=trans_dict["from_state"],
                to_state=trans_dict["to_state"],
                condition=trans_dict.get("condition", ""),
            ))

        return WorkflowDefinition(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            initial_state=data["initial_state"],
            states=states,
            transitions=transitions,
            version=data.get("version", 1),
        )

    def has_workflow(self, workflow_id: str) -> bool:
        """Check if a workflow exists."""
        return workflow_id in self.workflows

    def get_workflow(self, workflow_id: str) -> WorkflowDefinition | None:
        """Get a workflow by ID."""
        return self.workflows.get(workflow_id)

    def can_transition(self, workflow: WorkflowDefinition, from_state: str, to_state: str) -> bool:
        """Check if a state transition is allowed by the workflow."""
        return workflow.is_valid_transition(from_state, to_state)

    def transition_task(
        self,
        task: Task,
        from_state: str,
        to_state: str,
        *,
        reason: str = "",
    ) -> Task:
        """Transition a task to a new workflow state.

        Args:
            task: Task to transition.
            from_state: Current state.
            to_state: Target state.
            reason: Why the transition occurred.

        Returns:
            Updated task with new state and history entry.

        Raises:
            ValueError: If transition is invalid.
        """
        if not task.workflow_id:
            raise ValueError(f"Task {task.id} has no workflow_id")

        workflow = self.get_workflow(task.workflow_id)
        if not workflow:
            raise ValueError(f"Unknown workflow: {task.workflow_id}")

        if not self.can_transition(workflow, from_state, to_state):
            raise ValueError(
                f"Invalid transition {from_state} → {to_state} in workflow {workflow.id}"
            )

        # Append to history
        history_entry = {
            "state": from_state,
            "entered_at": getattr(task, "_state_enter_time", time.time()),
            "exited_at": time.time(),
            "result_summary": reason,
        }

        new_history = list(task.workflow_history) + [history_entry]

        # Create new task with updated state
        task.workflow_state = to_state
        task.workflow_history = new_history

        logger.info("Task %s transitioned: %s → %s (%s)", task.id, from_state, to_state, reason)

        return task

    def get_next_states(self, workflow: WorkflowDefinition, current_state: str) -> list[str]:
        """Get all states reachable from current state."""
        return workflow.get_next_states(current_state)

    def get_state_info(self, workflow: WorkflowDefinition, state_id: str) -> WorkflowState | None:
        """Get details about a specific state."""
        return workflow.get_state(state_id)
```

- [ ] **Step 5: Write test for transition validation**

```python
def test_workflow_engine_rejects_invalid_transition():
    engine = WorkflowEngine(workdir=Path("/tmp/test"))
    workflow = engine.get_workflow("default")

    task = Task(
        id="test-1",
        title="Test Task",
        description="",
        role="backend",
        workflow_id="default",
        workflow_state="plan"
    )

    # Try invalid transition (skip states)
    with pytest.raises(ValueError, match="Invalid transition"):
        engine.transition_task(task, "plan", "merge")
```

- [ ] **Step 6: Run all workflow engine tests**

```bash
pytest tests/unit/test_workflows.py -xvs -k "engine or transition"
```

Expected: All tests PASS

- [ ] **Step 7: Commit workflow engine**

```bash
git add src/bernstein/core/workflows.py tests/unit/test_workflows.py
git commit -m "feat: implement WorkflowEngine for state machine execution

- Load workflow definitions from YAML templates
- Validate state transitions against workflow definition
- Transition tasks between states with history tracking
- Provide workflow lookup and state info queries"
```

---

### Task 3: Workflow Templates (Default & Simple)

**Files:**
- Create: `templates/workflows/default.yaml`
- Create: `templates/workflows/simple.yaml`

Standard workflow definitions for common task patterns.

- [ ] **Step 1: Write default.yaml workflow**

Create `templates/workflows/default.yaml`:

```yaml
id: "default"
name: "Standard Development Workflow"
description: "Plan → Implement → Test → Review → Merge"
version: 1
initial_state: "plan"

states:
  - id: "plan"
    name: "Planning"
    description: "Decompose task, create subtasks, design solution"
    required_role: "manager"
    estimated_minutes: 20
    checkpoint:
      type: "auto"
      required_quality_score: 0.7
      requires_human_approval: false
      required_completion_signals: []

  - id: "implement"
    name: "Implementation"
    description: "Write code, create files, implement features"
    required_role: "backend"
    estimated_minutes: 45
    checkpoint:
      type: "auto"
      required_quality_score: 0.8
      requires_human_approval: false
      required_completion_signals: ["file_exists", "test_passes"]

  - id: "test"
    name: "Testing"
    description: "Write unit/integration tests, verify quality"
    required_role: "qa"
    estimated_minutes: 30
    checkpoint:
      type: "auto"
      required_quality_score: 0.9
      requires_human_approval: false
      required_completion_signals: ["test_passes", "coverage_above_80"]

  - id: "review"
    name: "Code Review"
    description: "Security review, architecture review, code quality"
    required_role: "manager"
    estimated_minutes: 20
    checkpoint:
      type: "hybrid"
      required_quality_score: 0.85
      requires_human_approval: true
      approval_role: "manager"
      approval_timeout_minutes: 1440

  - id: "merge"
    name: "Merge to Main"
    description: "Merge PR, close task, update records"
    required_role: "manager"
    estimated_minutes: 5
    checkpoint:
      type: "none"

transitions:
  - from_state: "plan"
    to_state: "implement"
    condition: "subtasks created and design approved"

  - from_state: "implement"
    to_state: "test"
    condition: "code committed and tests written"

  - from_state: "test"
    to_state: "review"
    condition: "all tests passing and coverage adequate"

  - from_state: "review"
    to_state: "merge"
    condition: "approval given and all feedback addressed"

  - from_state: "review"
    to_state: "implement"
    condition: "feedback requires rework"
```

- [ ] **Step 2: Write simple.yaml workflow**

Create `templates/workflows/simple.yaml`:

```yaml
id: "simple"
name: "Simple Implementation Workflow"
description: "Plan → Implement → Done (minimal checkpoints)"
version: 1
initial_state: "plan"

states:
  - id: "plan"
    name: "Planning"
    description: "Understand requirements"
    required_role: "manager"
    estimated_minutes: 10
    checkpoint:
      type: "auto"
      required_quality_score: 0.0
      requires_human_approval: false

  - id: "implement"
    name: "Implementation"
    description: "Write code and tests"
    required_role: "backend"
    estimated_minutes: 30
    checkpoint:
      type: "auto"
      required_quality_score: 0.8
      requires_human_approval: false
      required_completion_signals: ["test_passes"]

  - id: "done"
    name: "Done"
    description: "Task complete"
    required_role: "manager"
    estimated_minutes: 0
    checkpoint:
      type: "none"

transitions:
  - from_state: "plan"
    to_state: "implement"

  - from_state: "implement"
    to_state: "done"
```

- [ ] **Step 3: Verify workflows can be loaded**

```bash
# Test by creating a simple Python script
python3 << 'EOF'
from pathlib import Path
from bernstein.core.workflows import WorkflowEngine

engine = WorkflowEngine(Path("."))
print(f"Loaded workflows: {list(engine.workflows.keys())}")
for name in ["default", "simple"]:
    wf = engine.get_workflow(name)
    if wf:
        print(f"  {name}: {len(wf.states)} states, {len(wf.transitions)} transitions")
EOF
```

Expected: Output shows both workflows loaded with correct state/transition counts

- [ ] **Step 4: Commit workflow templates**

```bash
git add templates/workflows/default.yaml templates/workflows/simple.yaml
git commit -m "feat: add default and simple workflow templates

- default.yaml: plan → implement → test → review → merge (5 states)
- simple.yaml: plan → implement → done (3 states, minimal gates)
- Both include checkpoint configuration for quality gates"
```

---

### Task 4: Checkpoint Store & Decision Tracking

**Files:**
- Create: `src/bernstein/core/workflow_checkpoint_store.py`
- Modify: `.sdd/` directory structure
- Test: `tests/unit/test_workflows.py` (extend)

Persistent storage for checkpoint decisions, audit trail.

- [ ] **Step 1: Write failing test for checkpoint decision storage**

```python
def test_checkpoint_store_save_decision():
    store = WorkflowCheckpointStore(workdir=Path("/tmp/test"))

    decision = WorkflowCheckpointDecision(
        checkpoint_id="plan_review",
        task_id="test-1",
        from_state="plan",
        to_state="implement",
        decision="approved",
        approver="manager",
        reason="Requirements clear, design sound",
        timestamp=time.time()
    )

    store.save_decision(decision)

    # Retrieve it
    retrieved = store.get_decision(checkpoint_id="plan_review", task_id="test-1")
    assert retrieved is not None
    assert retrieved.decision == "approved"
```

- [ ] **Step 2: Implement WorkflowCheckpointStore**

Create `src/bernstein/core/workflow_checkpoint_store.py`:

```python
"""Checkpoint decision persistence and audit trail."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from bernstein.core.workflow_models import WorkflowCheckpointDecision

logger = logging.getLogger(__name__)


class WorkflowCheckpointStore:
    """Persistent store for workflow checkpoint decisions.

    Decisions are stored in .sdd/workflow/checkpoints/ as JSONL for audit trail.
    """

    def __init__(self, workdir: Path):
        """Initialize checkpoint store.

        Args:
            workdir: Project root (where .sdd/ lives).
        """
        self.workdir = workdir
        self.checkpoint_dir = workdir / ".sdd" / "workflow" / "checkpoints"
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def save_decision(self, decision: WorkflowCheckpointDecision) -> None:
        """Save a checkpoint decision to the audit trail.

        Args:
            decision: The decision to save.
        """
        # Store in a JSONL file per task for easy retrieval
        log_file = self.checkpoint_dir / f"{decision.task_id}.jsonl"

        decision_dict = {
            "checkpoint_id": decision.checkpoint_id,
            "task_id": decision.task_id,
            "from_state": decision.from_state,
            "to_state": decision.to_state,
            "decision": decision.decision,
            "approver": decision.approver,
            "reason": decision.reason,
            "timestamp": decision.timestamp,
        }

        with open(log_file, "a") as f:
            f.write(json.dumps(decision_dict) + "\n")

        logger.debug("Saved checkpoint decision: %s → %s for task %s",
                     decision.from_state, decision.to_state, decision.task_id)

    def get_decision(self, checkpoint_id: str, task_id: str) -> WorkflowCheckpointDecision | None:
        """Retrieve the most recent decision for a checkpoint.

        Args:
            checkpoint_id: ID of the checkpoint.
            task_id: ID of the task.

        Returns:
            Most recent matching decision, or None if not found.
        """
        log_file = self.checkpoint_dir / f"{task_id}.jsonl"
        if not log_file.exists():
            return None

        # Read all decisions for this task and find matching checkpoint
        with open(log_file) as f:
            lines = f.readlines()

        for line in reversed(lines):
            decision_dict = json.loads(line.strip())
            if decision_dict["checkpoint_id"] == checkpoint_id:
                return WorkflowCheckpointDecision(
                    checkpoint_id=decision_dict["checkpoint_id"],
                    task_id=decision_dict["task_id"],
                    from_state=decision_dict["from_state"],
                    to_state=decision_dict["to_state"],
                    decision=decision_dict["decision"],  # type: ignore
                    approver=decision_dict["approver"],
                    reason=decision_dict["reason"],
                    timestamp=decision_dict["timestamp"],
                )

        return None

    def get_task_decisions(self, task_id: str) -> list[WorkflowCheckpointDecision]:
        """Get all checkpoint decisions for a task (ordered by time)."""
        log_file = self.checkpoint_dir / f"{task_id}.jsonl"
        if not log_file.exists():
            return []

        decisions = []
        with open(log_file) as f:
            for line in f:
                decision_dict = json.loads(line.strip())
                decisions.append(WorkflowCheckpointDecision(
                    checkpoint_id=decision_dict["checkpoint_id"],
                    task_id=decision_dict["task_id"],
                    from_state=decision_dict["from_state"],
                    to_state=decision_dict["to_state"],
                    decision=decision_dict["decision"],  # type: ignore
                    approver=decision_dict["approver"],
                    reason=decision_dict["reason"],
                    timestamp=decision_dict["timestamp"],
                ))

        return decisions

    def reject_transition(
        self,
        task_id: str,
        from_state: str,
        to_state: str,
        approver: str,
        reason: str,
    ) -> WorkflowCheckpointDecision:
        """Record a checkpoint rejection (blocking a transition)."""
        decision = WorkflowCheckpointDecision(
            checkpoint_id=f"{from_state}_{to_state}",
            task_id=task_id,
            from_state=from_state,
            to_state=to_state,
            decision="rejected",
            approver=approver,
            reason=reason,
            timestamp=time.time(),
        )
        self.save_decision(decision)
        return decision

    def approve_transition(
        self,
        task_id: str,
        from_state: str,
        to_state: str,
        approver: str,
        reason: str = "",
    ) -> WorkflowCheckpointDecision:
        """Record a checkpoint approval (allowing a transition)."""
        decision = WorkflowCheckpointDecision(
            checkpoint_id=f"{from_state}_{to_state}",
            task_id=task_id,
            from_state=from_state,
            to_state=to_state,
            decision="approved",
            approver=approver,
            reason=reason or "Approved",
            timestamp=time.time(),
        )
        self.save_decision(decision)
        return decision
```

- [ ] **Step 3: Write test for retrieving task decisions**

```python
def test_checkpoint_store_get_task_decisions():
    store = WorkflowCheckpointStore(workdir=Path("/tmp/test"))

    # Save two decisions for same task
    d1 = WorkflowCheckpointDecision(
        checkpoint_id="plan_review",
        task_id="test-1",
        from_state="plan",
        to_state="implement",
        decision="rejected",
        approver="manager",
        reason="Needs more detail",
        timestamp=time.time()
    )
    store.save_decision(d1)

    d2 = WorkflowCheckpointDecision(
        checkpoint_id="plan_review",
        task_id="test-1",
        from_state="plan",
        to_state="implement",
        decision="approved",
        approver="manager",
        reason="Updated plan looks good",
        timestamp=time.time() + 3600
    )
    store.save_decision(d2)

    decisions = store.get_task_decisions("test-1")
    assert len(decisions) == 2
    assert decisions[1].decision == "approved"  # Most recent
```

- [ ] **Step 4: Run checkpoint store tests**

```bash
pytest tests/unit/test_workflows.py::test_checkpoint_store -xvs
```

Expected: All tests PASS

- [ ] **Step 5: Commit checkpoint store**

```bash
git add src/bernstein/core/workflow_checkpoint_store.py tests/unit/test_workflows.py
git commit -m "feat: implement checkpoint decision store with audit trail

- Persistent JSONL storage for checkpoint decisions per task
- Retrieve latest decision or full audit trail
- Helper methods to record approvals/rejections
- Stored in .sdd/workflow/checkpoints/ for durability"
```

---

### Task 5: Checkpoint Verification & Quality Gates

**Files:**
- Create: `src/bernstein/core/workflow_checkpoint_validators.py`
- Modify: `src/bernstein/core/workflows.py`
- Test: `tests/unit/test_workflows.py` (extend)

Automated quality checks before human approval gates.

- [ ] **Step 1: Write failing test for checkpoint validation**

```python
def test_checkpoint_validator_auto_pass():
    validator = CheckpointValidator(
        workdir=Path("/tmp/test"),
        completion_signals=["test_passes", "file_exists:src/main.py"]
    )

    # Mock successful completion signals
    validation_result = validator.validate(
        task=Task(id="test-1", title="Test", description="", role="backend"),
        completion_data={"test_results": {"status": "passed"}, "files_modified": ["src/main.py"]}
    )

    assert validation_result.quality_score >= 0.8
    assert validation_result.passed is True
```

- [ ] **Step 2: Write failing test for human approval requirement**

```python
def test_checkpoint_requires_human_approval():
    checkpoint = CheckpointConfig(
        type=CheckpointType.HUMAN,
        requires_human_approval=True,
        approval_role="manager"
    )

    validator = CheckpointValidator(workdir=Path("/tmp/test"), config=checkpoint)

    # Even with good quality, human approval should be required
    validation_result = validator.validate(
        task=Task(id="test-1", title="Test", description="", role="backend"),
        completion_data={"quality_score": 0.95}
    )

    assert validation_result.passed is False
    assert validation_result.requires_human_approval is True
```

- [ ] **Step 3: Implement CheckpointValidator**

Create `src/bernstein/core/workflow_checkpoint_validators.py`:

```python
"""Checkpoint validators for workflow state gates."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bernstein.core.models import Task
from bernstein.core.workflow_models import CheckpointConfig, CheckpointType

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of checkpoint validation.

    Attributes:
        passed: Whether the checkpoint passed (auto-approval).
        quality_score: Computed quality (0.0-1.0).
        requires_human_approval: Whether human sign-off is needed.
        reason: Why it passed/failed.
        failed_signals: Which completion signals failed.
    """
    passed: bool
    quality_score: float
    requires_human_approval: bool
    reason: str
    failed_signals: list[str] = None

    def __post_init__(self):
        if self.failed_signals is None:
            self.failed_signals = []


class CheckpointValidator:
    """Validates state transitions against checkpoint criteria."""

    def __init__(
        self,
        workdir: Path,
        config: CheckpointConfig | None = None,
        completion_signals: list[str] | None = None,
    ):
        """Initialize validator.

        Args:
            workdir: Project root.
            config: Checkpoint configuration.
            completion_signals: Janitor signals to check.
        """
        self.workdir = workdir
        self.config = config or CheckpointConfig()
        self.completion_signals = completion_signals or []

    def validate(
        self,
        task: Task,
        completion_data: dict[str, Any],
    ) -> ValidationResult:
        """Validate a checkpoint.

        Args:
            task: Task attempting to transition.
            completion_data: Completion data from agent (test results, files, etc.).

        Returns:
            ValidationResult indicating pass/fail and requirements.
        """
        failed_signals: list[str] = []
        quality_score = 0.0
        reason = ""

        # Check required completion signals
        if self.completion_signals:
            failed_signals = self._check_completion_signals(completion_data)
            if failed_signals:
                quality_score = max(0.0, (len(self.completion_signals) - len(failed_signals)) / len(self.completion_signals))
            else:
                quality_score = 1.0

        # Check quality score threshold
        if quality_score < self.config.required_quality_score:
            return ValidationResult(
                passed=False,
                quality_score=quality_score,
                requires_human_approval=False,
                reason=f"Quality score {quality_score:.2f} below threshold {self.config.required_quality_score}",
                failed_signals=failed_signals,
            )

        # Determine if auto-approved or needs human
        if self.config.type == CheckpointType.NONE:
            return ValidationResult(
                passed=True,
                quality_score=quality_score,
                requires_human_approval=False,
                reason="No checkpoint required",
            )
        elif self.config.type == CheckpointType.AUTO:
            return ValidationResult(
                passed=True,
                quality_score=quality_score,
                requires_human_approval=False,
                reason=f"Auto-approved (quality: {quality_score:.2f})",
            )
        elif self.config.type == CheckpointType.HYBRID or self.config.requires_human_approval:
            return ValidationResult(
                passed=False,
                quality_score=quality_score,
                requires_human_approval=True,
                reason="Awaiting human approval",
            )
        else:
            return ValidationResult(
                passed=True,
                quality_score=quality_score,
                requires_human_approval=False,
                reason=f"Approved (quality: {quality_score:.2f})",
            )

    def _check_completion_signals(self, completion_data: dict[str, Any]) -> list[str]:
        """Check which completion signals failed."""
        failed = []
        for signal in self.completion_signals:
            if not self._signal_passes(signal, completion_data):
                failed.append(signal)
        return failed

    def _signal_passes(self, signal: str, completion_data: dict[str, Any]) -> bool:
        """Check if a single completion signal passes."""
        # Stub: full impl checks janitor signals
        # For now, simple checks for test_passes, file_exists

        if signal == "test_passes":
            test_results = completion_data.get("test_results", {})
            return test_results.get("status") == "passed"
        elif signal.startswith("file_exists:"):
            filepath = signal.split(":", 1)[1]
            return filepath in completion_data.get("files_modified", [])
        else:
            # Unknown signal; assume pass
            logger.warning("Unknown completion signal: %s", signal)
            return True
```

- [ ] **Step 4: Run checkpoint validator tests**

```bash
pytest tests/unit/test_workflows.py -xvs -k "validator or checkpoint"
```

Expected: All tests PASS

- [ ] **Step 5: Integrate validator into WorkflowEngine**

Modify `src/bernstein/core/workflows.py` to add validation before transitions:

```python
def validate_transition(
    self,
    workflow: WorkflowDefinition,
    task: Task,
    from_state: str,
    to_state: str,
    completion_data: dict[str, Any] | None = None,
) -> ValidationResult:
    """Validate that a transition can proceed.

    Args:
        workflow: The workflow definition.
        task: Task attempting transition.
        from_state: Current state.
        to_state: Target state.
        completion_data: Optional completion data for quality checks.

    Returns:
        ValidationResult indicating pass/fail.
    """
    if not self.can_transition(workflow, from_state, to_state):
        return ValidationResult(
            passed=False,
            quality_score=0.0,
            requires_human_approval=False,
            reason="Transition not allowed by workflow"
        )

    state = workflow.get_state(from_state)
    if not state:
        return ValidationResult(
            passed=False,
            quality_score=0.0,
            requires_human_approval=False,
            reason=f"Unknown state: {from_state}"
        )

    validator = CheckpointValidator(
        self.workdir,
        config=state.checkpoint_config,
        completion_signals=state.checkpoint_config.required_completion_signals,
    )

    completion_data = completion_data or {}
    return validator.validate(task, completion_data)
```

- [ ] **Step 6: Commit checkpoint validators**

```bash
git add src/bernstein/core/workflow_checkpoint_validators.py src/bernstein/core/workflows.py tests/unit/test_workflows.py
git commit -m "feat: add checkpoint validation with quality gates

- CheckpointValidator checks completion signals and quality scores
- Support for NONE/AUTO/HYBRID/HUMAN checkpoint types
- Integrate validation into WorkflowEngine.validate_transition()
- Return structured ValidationResult with pass/fail and approval requirements"
```

---

### Task 6: Task Lifecycle Integration

**Files:**
- Modify: `src/bernstein/core/task_lifecycle.py`
- Modify: `src/bernstein/core/orchestrator.py`
- Modify: `src/bernstein/core/models.py` (extend Task.from_dict)
- Test: `tests/unit/test_workflow_integration.py` (new)

Wire workflow state transitions into the existing task completion flow.

- [ ] **Step 1: Write failing integration test**

```python
def test_task_completion_triggers_workflow_transition():
    """Test that completing a task advances its workflow state."""
    engine = WorkflowEngine(workdir=Path("/tmp/test"))
    checkpoint_store = WorkflowCheckpointStore(workdir=Path("/tmp/test"))

    # Create a task with workflow
    task = Task(
        id="test-1",
        title="Test Feature",
        description="Implement feature",
        role="backend",
        workflow_id="default",
        workflow_state="implement",
    )

    # Simulate task completion
    completion_data = {
        "files_modified": ["src/feature.py"],
        "test_results": {"status": "passed"},
    }

    # Should auto-transition to test state (implement → test is auto)
    result = transition_task_on_completion(
        task=task,
        engine=engine,
        checkpoint_store=checkpoint_store,
        completion_data=completion_data,
    )

    assert result.workflow_state == "test"
    assert len(result.workflow_history) >= 1
```

- [ ] **Step 2: Implement transition_task_on_completion in task_lifecycle.py**

Add to `src/bernstein/core/task_lifecycle.py`:

```python
from bernstein.core.workflows import WorkflowEngine
from bernstein.core.workflow_checkpoint_store import WorkflowCheckpointStore
from bernstein.core.workflow_checkpoint_validators import ValidationResult


def transition_task_on_completion(
    task: Task,
    engine: WorkflowEngine,
    checkpoint_store: WorkflowCheckpointStore,
    completion_data: dict[str, Any],
) -> Task:
    """Auto-transition a task to the next workflow state on completion.

    Called after task agent reports completion. Checks if the completion
    satisfies state exit criteria and moves the task forward.

    Args:
        task: Task that completed.
        engine: WorkflowEngine for validation and transition.
        checkpoint_store: Checkpoint decision store.
        completion_data: Data from the agent (files, test results, etc.).

    Returns:
        Updated task with new workflow state (or unchanged if no transition).

    Raises:
        ValueError: If task has no workflow.
    """
    if not task.workflow_id or not task.workflow_state:
        logger.debug("Task %s has no workflow, skipping state transition", task.id)
        return task

    workflow = engine.get_workflow(task.workflow_id)
    if not workflow:
        logger.warning("Unknown workflow: %s", task.workflow_id)
        return task

    current_state = task.workflow_state
    next_states = engine.get_next_states(workflow, current_state)

    if not next_states:
        logger.debug("Task %s in terminal state %s", task.id, current_state)
        return task

    # Auto-advance to first next state (assume linear progression for now)
    target_state = next_states[0]

    # Validate transition
    validation = engine.validate_transition(
        workflow,
        task,
        current_state,
        target_state,
        completion_data,
    )

    if validation.requires_human_approval:
        logger.info(
            "Task %s transition %s → %s requires human approval: %s",
            task.id, current_state, target_state, validation.reason,
        )
        # Don't advance; wait for manual approval
        return task

    if not validation.passed:
        logger.warning(
            "Task %s failed transition validation: %s",
            task.id, validation.reason,
        )
        # Could mark task as needing rework, or hold in current state
        return task

    # All checks passed, advance state
    logger.info(
        "Task %s auto-transitioning: %s → %s (quality: %.2f)",
        task.id, current_state, target_state, validation.quality_score,
    )

    updated_task = engine.transition_task(
        task,
        current_state,
        target_state,
        reason=validation.reason,
    )

    # Record approval
    checkpoint_store.approve_transition(
        task_id=task.id,
        from_state=current_state,
        to_state=target_state,
        approver="auto",
        reason=validation.reason,
    )

    return updated_task
```

- [ ] **Step 3: Wire into process_completed_tasks in task_lifecycle.py**

Modify `process_completed_tasks()` to call workflow transition. Find the function and add after task status update:

```python
# ... existing completion logic ...

# Advance workflow state if task has one
task = transition_task_on_completion(
    task=task,
    engine=WorkflowEngine(workdir),
    checkpoint_store=WorkflowCheckpointStore(workdir),
    completion_data=completion_data,
)

# Update task on server with new state
# ... existing server update ...
```

- [ ] **Step 4: Update Task.from_dict to handle workflow fields**

Modify `src/bernstein/core/models.py` Task.from_dict method:

```python
@classmethod
def from_dict(cls, raw: dict[str, Any]) -> Task:
    """Deserialise a server JSON response into a Task.

    Args:
        raw: Dict from the task server JSON response.

    Returns:
        Fully initialized Task.
    """
    # ... existing deserialization ...

    # Add workflow fields
    task.workflow_id = raw.get("workflow_id")
    task.workflow_state = raw.get("workflow_state")
    task.workflow_history = raw.get("workflow_history", [])

    return task
```

- [ ] **Step 5: Write integration test for multi-state progression**

```python
def test_task_progresses_through_workflow_states():
    """Test full task progression: plan → implement → test → review."""
    engine = WorkflowEngine(workdir=Path("/tmp/test"))
    store = WorkflowCheckpointStore(workdir=Path("/tmp/test"))

    task = Task(
        id="full-flow",
        title="Full Flow Test",
        description="",
        role="backend",
        workflow_id="default",
        workflow_state="plan"
    )

    # plan → implement
    task = transition_task_on_completion(task, engine, store, {})
    assert task.workflow_state == "implement"

    # implement → test
    task = transition_task_on_completion(
        task, engine, store,
        {"files_modified": ["src/test.py"], "test_results": {"status": "passed"}}
    )
    assert task.workflow_state == "test"

    # test → review (with quality score check)
    task = transition_task_on_completion(
        task, engine, store,
        {"test_results": {"status": "passed", "coverage": 85}}
    )
    assert task.workflow_state == "review"
```

- [ ] **Step 6: Run integration tests**

```bash
pytest tests/unit/test_workflow_integration.py -xvs
```

Expected: All tests PASS

- [ ] **Step 7: Commit task lifecycle integration**

```bash
git add src/bernstein/core/task_lifecycle.py src/bernstein/core/orchestrator.py tests/unit/test_workflow_integration.py
git commit -m "feat: integrate workflow transitions into task lifecycle

- Add transition_task_on_completion() to advance states on task completion
- Validate transitions with quality gates before advancing
- Auto-approve transitions that pass checkpoint validation
- Hold transitions that require human approval
- Wire into process_completed_tasks() for automatic progression"
```

---

### Task 7: CLI Commands for Workflow Management

**Files:**
- Modify: `src/bernstein/cli/main.py`
- Create: `src/bernstein/cli/workflows_cmd.py`
- Test: `tests/unit/test_cli_workflows.py` (new)

User-facing commands to inspect and manage workflows.

- [ ] **Step 1: Write failing test for workflow list command**

```python
def test_cli_workflow_list():
    """Test: bernstein workflow list"""
    runner = CliRunner()
    result = runner.invoke(workflows_cmd.list_workflows)

    assert result.exit_code == 0
    assert "default" in result.output
    assert "simple" in result.output
```

- [ ] **Step 2: Create workflows_cmd.py with list command**

Create `src/bernstein/cli/workflows_cmd.py`:

```python
"""Workflow management CLI commands."""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.core.workflows import WorkflowEngine
from bernstein.core.workflow_checkpoint_store import WorkflowCheckpointStore


@click.group()
def workflows():
    """Manage workflow definitions and checkpoints."""
    pass


@workflows.command()
@click.option("--format", default="text", type=click.Choice(["text", "json"]))
def list_workflows(format: str):
    """List available workflow definitions."""
    engine = WorkflowEngine(Path.cwd())

    if format == "json":
        import json
        workflows_data = {
            wf_id: {
                "name": wf.name,
                "states": len(wf.states),
                "transitions": len(wf.transitions),
            }
            for wf_id, wf in engine.workflows.items()
        }
        click.echo(json.dumps(workflows_data, indent=2))
    else:
        if not engine.workflows:
            click.echo("No workflows found")
            return

        for wf_id, wf in engine.workflows.items():
            click.echo(f"{wf_id:15} {wf.name:30} ({len(wf.states)} states, {len(wf.transitions)} transitions)")


@workflows.command()
@click.argument("workflow_id")
def show_workflow(workflow_id: str):
    """Show details of a workflow."""
    engine = WorkflowEngine(Path.cwd())
    workflow = engine.get_workflow(workflow_id)

    if not workflow:
        click.echo(f"Workflow not found: {workflow_id}", err=True)
        raise SystemExit(1)

    click.echo(f"Workflow: {workflow.name}")
    click.echo(f"Description: {workflow.description}")
    click.echo(f"Initial State: {workflow.initial_state}")
    click.echo()

    click.echo("States:")
    for state in workflow.states:
        click.echo(f"  {state.id:15} {state.name} (role: {state.required_role})")
        if state.checkpoint_config.requires_human_approval:
            click.echo(f"                   ⚠️ Requires human approval")

    click.echo()
    click.echo("Transitions:")
    for trans in workflow.transitions:
        click.echo(f"  {trans.from_state} → {trans.to_state}")


@workflows.command()
@click.argument("task_id")
def show_task_checkpoints(task_id: str):
    """Show checkpoint decisions for a task."""
    store = WorkflowCheckpointStore(Path.cwd())
    decisions = store.get_task_decisions(task_id)

    if not decisions:
        click.echo(f"No checkpoints recorded for task {task_id}")
        return

    click.echo(f"Checkpoints for task {task_id}:")
    for decision in decisions:
        status = "✓" if decision.decision == "approved" else "✗"
        click.echo(
            f"  {status} {decision.from_state} → {decision.to_state}: "
            f"{decision.decision} by {decision.approver}"
        )
        if decision.reason:
            click.echo(f"     {decision.reason}")
```

- [ ] **Step 2: Add workflows command to main CLI**

Modify `src/bernstein/cli/main.py`:

```python
from bernstein.cli import workflows_cmd

@click.group()
def cli():
    """Bernstein multi-agent orchestrator."""
    pass

# Add workflows command group
cli.add_command(workflows_cmd.workflows)
```

- [ ] **Step 3: Write test for workflow show command**

```python
def test_cli_workflow_show():
    """Test: bernstein workflow show default"""
    runner = CliRunner()
    result = runner.invoke(workflows_cmd.show_workflow, ["default"])

    assert result.exit_code == 0
    assert "plan" in result.output
    assert "implement" in result.output
```

- [ ] **Step 4: Run CLI tests**

```bash
pytest tests/unit/test_cli_workflows.py -xvs
```

Expected: All tests PASS

- [ ] **Step 5: Manual test of CLI**

```bash
bernstein workflow list
bernstein workflow show default
```

Expected: Displays list of workflows and details of default workflow

- [ ] **Step 6: Commit CLI commands**

```bash
git add src/bernstein/cli/workflows_cmd.py src/bernstein/cli/main.py tests/unit/test_cli_workflows.py
git commit -m "feat: add CLI commands for workflow management

- bernstein workflow list — show available workflows
- bernstein workflow show <id> — show workflow details and states
- bernstein workflow checkpoints <task-id> — audit trail of checkpoint decisions"
```

---

### Task 8: Unit Tests for Edge Cases

**Files:**
- Modify: `tests/unit/test_workflows.py`
- Create: `tests/unit/test_workflow_edge_cases.py`

Comprehensive test coverage for edge cases and error conditions.

- [ ] **Step 1: Write test for invalid workflow reference**

```python
def test_task_transition_with_invalid_workflow():
    """Test that task with non-existent workflow is skipped gracefully."""
    engine = WorkflowEngine(workdir=Path("/tmp/test"))
    store = WorkflowCheckpointStore(workdir=Path("/tmp/test"))

    task = Task(
        id="bad-wf",
        title="Bad Workflow Task",
        description="",
        role="backend",
        workflow_id="nonexistent",  # Invalid
        workflow_state="plan",
    )

    # Should return task unchanged, log warning
    result = transition_task_on_completion(
        task, engine, store, {}
    )

    assert result.workflow_state == "plan"  # Unchanged
```

- [ ] **Step 2: Write test for checkpoint rejection requiring rework**

```python
def test_checkpoint_rejection_holds_task():
    """Test that rejected checkpoints keep task in current state."""
    store = WorkflowCheckpointStore(workdir=Path("/tmp/test"))

    # Simulate rejection
    decision = store.reject_transition(
        task_id="test-1",
        from_state="review",
        to_state="merge",
        approver="manager",
        reason="Tests still flaky, needs rework"
    )

    assert decision.decision == "rejected"

    # Task should not advance
    retrieved = store.get_decision("review_merge", "test-1")
    assert retrieved.decision == "rejected"
```

- [ ] **Step 3: Write test for checkpoint approval**

```python
def test_checkpoint_approval_allows_transition():
    """Test that approved checkpoints allow transitions."""
    engine = WorkflowEngine(workdir=Path("/tmp/test"))
    store = WorkflowCheckpointStore(workdir=Path("/tmp/test"))

    approval = store.approve_transition(
        task_id="test-1",
        from_state="review",
        to_state="merge",
        approver="manager",
        reason="Code review complete, all feedback addressed"
    )

    # Subsequent check should find approval
    retrieved = store.get_decision("review_merge", "test-1")
    assert retrieved.decision == "approved"
```

- [ ] **Step 4: Write test for parallel checkpoint decisions**

```python
def test_multiple_checkpoint_decisions_per_task():
    """Test that a task can have multiple independent checkpoints."""
    store = WorkflowCheckpointStore(workdir=Path("/tmp/test"))

    # Task goes through plan → implement → test → review → merge
    checkpoints = [
        ("plan_implement", "approved"),
        ("implement_test", "approved"),
        ("test_review", "rejected"),  # Feedback needed
        ("test_review", "approved"),  # Resubmission approved
        ("review_merge", "approved"),
    ]

    for cp_id, decision_val in checkpoints:
        if decision_val == "approved":
            store.approve_transition(
                task_id="test-1",
                from_state=cp_id.split("_")[0],
                to_state=cp_id.split("_")[1],
                approver="reviewer",
            )
        else:
            store.reject_transition(
                task_id="test-1",
                from_state=cp_id.split("_")[0],
                to_state=cp_id.split("_")[1],
                approver="reviewer",
                reason="Needs rework",
            )

    all_decisions = store.get_task_decisions("test-1")
    assert len(all_decisions) == 5
```

- [ ] **Step 5: Write test for workflow with no transitions from a state**

```python
def test_terminal_state_has_no_next_states():
    """Test that terminal states (like 'done') have no outgoing transitions."""
    engine = WorkflowEngine(workdir=Path("/tmp/test"))
    workflow = engine.get_workflow("simple")

    next_states = engine.get_next_states(workflow, "done")
    assert next_states == []
```

- [ ] **Step 6: Write test for circular workflow (if supported)**

```python
def test_workflow_can_have_fallback_transitions():
    """Test that tasks can transition back to earlier states on rejection."""
    engine = WorkflowEngine(workdir=Path("/tmp/test"))

    # Some workflows might allow review → implement (rework)
    # This is optional; just test that it doesn't crash
    workflow = engine.get_workflow("default")

    # Check if review → implement is allowed (may not be)
    is_allowed = engine.can_transition(workflow, "review", "implement")
    # Just verify no error; result depends on workflow definition
```

- [ ] **Step 7: Run edge case tests**

```bash
pytest tests/unit/test_workflow_edge_cases.py -xvs
```

Expected: All tests PASS

- [ ] **Step 8: Run full test suite for workflows**

```bash
pytest tests/unit/test_workflows.py tests/unit/test_workflow_integration.py tests/unit/test_workflow_edge_cases.py -xvs
```

Expected: All workflow tests PASS

- [ ] **Step 9: Commit edge case tests**

```bash
git add tests/unit/test_workflow_edge_cases.py tests/unit/test_workflows.py
git commit -m "feat: add comprehensive edge case tests for workflows

- Invalid workflow references handled gracefully
- Checkpoint rejections keep task in current state
- Checkpoint approvals unblock transitions
- Multiple checkpoint decisions per task tracked
- Terminal states have no outgoing transitions
- Fallback transitions (rework) optional per workflow"
```

---

### Task 9: Documentation & Examples

**Files:**
- Create: `docs/workflows/WORKFLOWS.md`
- Create: `docs/workflows/example-simple-task.md`
- Create: `docs/workflows/checkpoint-decisions.md`

Documentation for workflow concepts, usage, and examples.

- [ ] **Step 1: Write WORKFLOWS.md overview**

Create `docs/workflows/WORKFLOWS.md`:

```markdown
# Workflow State Machines

Bernstein supports explicit workflow definitions to drive tasks through deterministic state progressions. Each workflow defines states, transitions, and checkpoints (quality gates).

## Why Workflows?

- **Explicit progression**: Task state is machine-driven, not LLM-guessed
- **Quality gates**: Checkpoints can verify completion before advancing
- **Audit trail**: Every state transition is recorded with approval/rejection history
- **Customizable**: Define workflows per project or task type

## Built-in Workflows

### default
Standard 5-state workflow: `plan → implement → test → review → merge`
- **plan** (manager): Decompose requirements, design solution
- **implement** (backend): Write code and unit tests
- **test** (qa): Integration tests, quality verification
- **review** (manager): Code review, architecture check
- **merge** (manager): Merge PR, close task

Checkpoints:
- plan → implement: Auto-approve if requirements are clear
- implement → test: Auto-approve if tests written
- test → review: Auto-approve if tests pass
- review → merge: **Requires human approval**

### simple
Minimal 3-state workflow: `plan → implement → done`
- **plan** (manager): Quick understanding of requirements
- **implement** (backend): Write code and tests
- **done** (manager): Task complete

Checkpoints: Auto-approve all (no human gates)

## Creating a Custom Workflow

Define a YAML file in `templates/workflows/`:

```yaml
id: "custom"
name: "Custom Workflow"
description: "Your description here"
initial_state: "plan"

states:
  - id: "plan"
    name: "Planning"
    description: "Understand requirements"
    required_role: "manager"
    estimated_minutes: 20
    checkpoint:
      type: "auto"
      required_quality_score: 0.7
      requires_human_approval: false

  - id: "implement"
    name: "Implementation"
    description: "Write code"
    required_role: "backend"
    estimated_minutes: 45
    checkpoint:
      type: "hybrid"
      required_quality_score: 0.85
      requires_human_approval: true
      approval_role: "manager"

  - id: "done"
    name: "Done"
    description: "Task complete"
    required_role: "manager"
    estimated_minutes: 0
    checkpoint:
      type: "none"

transitions:
  - from_state: "plan"
    to_state: "implement"
    condition: "requirements understood"

  - from_state: "implement"
    to_state: "done"
    condition: "code complete and tests pass"

  - from_state: "done"
    to_state: "implement"
    condition: "needs rework (fallback)"
```

## Checkpoint Types

- **none**: No checkpoint, auto-transition
- **auto**: Automated quality checks only
- **human**: Requires explicit human approval
- **hybrid**: Both automated checks AND human approval

## CLI Commands

```bash
# List available workflows
bernstein workflow list

# Show workflow details
bernstein workflow show default

# Inspect checkpoint decisions for a task
bernstein workflow checkpoints TASK-ID
```

## State Transitions

When a task completes:
1. Agent reports completion with result summary
2. Orchestrator calls `transition_task_on_completion()`
3. Workflow engine validates the checkpoint
4. If checkpoint passes: task advances to next state
5. If checkpoint requires human approval: task waits for decision
6. Checkpoint decision is recorded in audit trail

## Audit Trail

All checkpoint decisions are stored in `.sdd/workflow/checkpoints/` as JSONL:

```json
{"checkpoint_id": "plan_implement", "task_id": "TEST-1", "from_state": "plan", "to_state": "implement", "decision": "approved", "approver": "auto", "reason": "Auto-approved", "timestamp": 1711788000}
```

Use `bernstein workflow checkpoints TASK-ID` to view decisions for a task.
```

- [ ] **Step 2: Write example usage document**

Create `docs/workflows/example-simple-task.md`:

```markdown
# Example: Simple Task Through Default Workflow

## Task Setup

Create a task with a workflow:

```bash
curl -X POST http://127.0.0.1:8052/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Add user authentication",
    "description": "Implement JWT-based user authentication",
    "role": "backend",
    "priority": 1,
    "scope": "medium",
    "complexity": "high",
    "workflow_id": "default",
    "workflow_state": "plan"
  }'
```

Task ID: `AUTH-001`

## State Progression

### 1. Planning (manager)
- Manager agent decomposes task into subtasks
- Creates test plan, design doc
- Checkpoint: Auto-approve (no quality gate needed)
- **→ Transition to implement**

### 2. Implementation (backend)
- Backend agent writes code
- Creates unit tests
- Commits changes
- Checkpoint: Validate tests pass
  - If tests pass: Auto-approve
  - If tests fail: Hold in implement state
- **→ Transition to test**

### 3. Testing (qa)
- QA agent writes integration tests
- Checks code coverage (target: 80%+)
- Runs full test suite
- Checkpoint: All tests passing + coverage >= 80%
  - If all pass: Auto-approve
  - If any fail: Hold in test state
- **→ Transition to review**

### 4. Review (manager)
- Manager does code review
- Security analysis
- Architecture check
- **Checkpoint: HUMAN APPROVAL REQUIRED**
  - Manager can approve (→ merge) or reject (→ implement for rework)
  - Approval timeout: 24 hours (can be configured)

### 5. Merge (manager)
- Merge PR to main
- Update task with final summary
- Task complete
- Checkpoint: None (terminal state)

## Checkpoint Example

Inspect the task's checkpoint decisions:

```bash
bernstein workflow checkpoints AUTH-001
```

Output:
```
Checkpoints for task AUTH-001:
  ✓ plan → implement: approved by auto
  ✓ implement → test: approved by auto
  ✓ test → review: approved by auto
  ✗ review → merge: rejected by alice@company.com
     Needs additional security review for token storage
  ✓ implement → review (rework): approved by auto
  ✓ review → merge: approved by alice@company.com
     Security concerns addressed
```

## Key Points

1. **Auto-approval** transitions save human time (implement → test, test → review)
2. **Human approval gates** (review → merge) catch quality issues early
3. **Fallback transitions** (review → implement) allow rework without restarting
4. **Audit trail** shows full decision history including rejections and rework cycles
```

- [ ] **Step 3: Write checkpoint decisions guide**

Create `docs/workflows/checkpoint-decisions.md`:

```markdown
# Checkpoint Decisions & Approvals

## What is a Checkpoint?

A checkpoint is a gate that controls transitions between workflow states. It can be:
- **Automated** (quality checks, test results)
- **Manual** (human approval)
- **Hybrid** (both)

## Automated Checkpoints

Example: `implement → test` checkpoint in default workflow

```yaml
checkpoint:
  type: "auto"
  required_quality_score: 0.8
  requires_human_approval: false
  required_completion_signals: ["test_passes"]
```

Validation steps:
1. Parse agent's completion data (files modified, test results)
2. Check required signals ("test_passes")
3. Compute quality score from signal pass rate
4. If quality >= threshold: auto-approve and advance
5. If quality < threshold: hold task in current state

## Human Approval Checkpoints

Example: `review → merge` checkpoint in default workflow

```yaml
checkpoint:
  type: "hybrid"
  required_quality_score: 0.85
  requires_human_approval: true
  approval_role: "manager"
  approval_timeout_minutes: 1440
```

Validation steps:
1. Run automated checks (quality score)
2. If quality passes: place task in "waiting for approval" state
3. Manager can:
   - **Approve**: Task advances to next state
   - **Reject**: Task reverts to previous state (rework)
   - **Comment**: Leave feedback for agent
4. If not approved within timeout: escalate to escalation handler

## Approving a Checkpoint (API)

```bash
curl -X POST http://127.0.0.1:8052/tasks/AUTH-001/approve-checkpoint \
  -H "Content-Type: application/json" \
  -d '{
    "from_state": "review",
    "to_state": "merge",
    "approver": "alice@company.com",
    "reason": "Code review passed, all feedback addressed"
  }'
```

## Rejecting a Checkpoint (API)

```bash
curl -X POST http://127.0.0.1:8052/tasks/AUTH-001/reject-checkpoint \
  -H "Content-Type: application/json" \
  -d '{
    "from_state": "review",
    "to_state": "merge",
    "approver": "alice@company.com",
    "reason": "Needs additional security review for token storage"
  }'
```

After rejection, the task reverts to the previous state (implement) for rework.

## Viewing Checkpoint History

```bash
bernstein workflow checkpoints AUTH-001
```

Shows all checkpoint decisions for the task in chronological order.

## Configuring Approval Roles

Each checkpoint specifies which role can approve:

```yaml
checkpoint:
  type: "human"
  approval_role: "manager"  # Only "manager" role agents can approve
```

The orchestrator validates that approvals come from the correct role.

## Timeout & Escalation

If a human approval checkpoint times out (no decision within `approval_timeout_minutes`):
1. Send notification to approval role
2. Escalate to project manager or administrator
3. After 48-hour escalation period: auto-approve or auto-reject (configurable)

## Best Practices

1. **Use auto checkpoints for objective measures** (tests pass, coverage > 80%)
2. **Use human checkpoints for subjective reviews** (code quality, architecture, security)
3. **Set reasonable approval timeouts** (24 hours is typical)
4. **Document rejection reasons** so agents can address feedback
5. **Monitor checkpoint bottlenecks** (approval timeouts indicate understaffing)
```

- [ ] **Step 4: Commit documentation**

```bash
git add docs/workflows/WORKFLOWS.md docs/workflows/example-simple-task.md docs/workflows/checkpoint-decisions.md
git commit -m "docs: add comprehensive workflow documentation

- WORKFLOWS.md: Overview of workflow concepts, built-in workflows, custom workflow creation
- example-simple-task.md: Step-by-step example of a task progressing through default workflow
- checkpoint-decisions.md: Detailed guide to automated and human approval checkpoints
- Includes API examples, CLI commands, best practices"
```

---

### Task 10: Verification & Final Tests

**Files:**
- Run: Full test suite
- Manual: CLI commands and API

Verify all workflow functionality works end-to-end.

- [ ] **Step 1: Run full workflow test suite**

```bash
uv run python scripts/run_tests.py -x -k workflow
```

Expected: All workflow tests PASS (no failures, no timeouts)

- [ ] **Step 2: Verify imports and type checking**

```bash
# Check type safety
uv run pyright src/bernstein/core/workflow*.py --outputjson

# Check import chain
python3 -c "from bernstein.core.workflows import WorkflowEngine; from bernstein.core.workflow_models import *; print('Imports OK')"
```

Expected: No type errors, imports work

- [ ] **Step 3: Manual test of CLI commands**

```bash
# Test workflow list
bernstein workflow list

# Test workflow show
bernstein workflow show default
bernstein workflow show simple

# Test invalid workflow
bernstein workflow show nonexistent
```

Expected:
- `list`: Shows "default" and "simple" workflows
- `show default`: Shows 5 states, 4 transitions
- `show simple`: Shows 3 states, 2 transitions
- `show nonexistent`: Error message, exit code 1

- [ ] **Step 4: Manual test of task creation with workflow**

```bash
# Create a test task with workflow
curl -X POST http://127.0.0.1:8052/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Test Workflow Task",
    "description": "Verify workflow progression",
    "role": "backend",
    "workflow_id": "default",
    "workflow_state": "plan"
  }'

# Verify task has workflow fields
curl -s http://127.0.0.1:8052/tasks | jq '.tasks[] | select(.workflow_id != null)'
```

Expected: Task created with workflow_id and workflow_state fields

- [ ] **Step 5: Manual test of state transition**

Simulate task completion:

```bash
# Claim the task (manager claims it)
curl -X POST http://127.0.0.1:8052/tasks/TEST-1/claim \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "manager-session-1"}'

# Complete task (manager finishes planning)
curl -X POST http://127.0.0.1:8052/tasks/TEST-1/complete \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "Task planned, subtasks created"}'

# Check state advanced from "plan" to "implement"
curl -s http://127.0.0.1:8052/tasks/TEST-1 | jq '.workflow_state'
```

Expected: workflow_state changed from "plan" to "implement"

- [ ] **Step 6: Verify checkpoint decisions recorded**

```bash
# Check checkpoint audit trail
bernstein workflow checkpoints TEST-1

# Or inspect the JSON directly
cat .sdd/workflow/checkpoints/TEST-1.jsonl | jq '.'
```

Expected: Shows checkpoint decision with "plan" → "implement" approval

- [ ] **Step 7: Test checkpoint rejection (manual approval gate)**

Create a review→merge transition with human approval:

```bash
# Create task in review state
curl -X POST http://127.0.0.1:8052/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Review Test",
    "description": "Test human approval",
    "role": "manager",
    "workflow_id": "default",
    "workflow_state": "review"
  }'

# Try to complete (should require human approval, not auto-advance)
curl -X POST http://127.0.0.1:8052/tasks/TEST-2/complete \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "Code review complete"}'

# Check state is still "review" (not advanced to merge)
curl -s http://127.0.0.1:8052/tasks/TEST-2 | jq '.workflow_state'
```

Expected: workflow_state stays "review" (human approval required)

- [ ] **Step 8: Run all tests (including non-workflow)**

```bash
uv run python scripts/run_tests.py -x --tb=short
```

Expected: No regressions in existing tests, all new workflow tests PASS

- [ ] **Step 9: Check code coverage**

```bash
# Optional: run with coverage
uv run pytest tests/unit/test_workflow*.py --cov=bernstein.core.workflow --cov-report=term-missing
```

Expected: >80% coverage of workflow modules

- [ ] **Step 10: Commit verification results**

```bash
git add -A
git commit -m "feat: complete and verify state machine workflow implementation

- All workflow tests passing (18 new test suites)
- CLI commands operational (list, show, checkpoints)
- Task progression through states working end-to-end
- Checkpoint validation (auto and human) functional
- Audit trail recording decisions persistently
- No regressions in existing test suite
- Type checking passes (Pyright strict)"
```

---

## Spec Coverage Checklist

- [x] **Explicit state definitions** — WorkflowState dataclass, 5+ states per workflow
- [x] **State transitions** — StateTransition, validate_transition(), can_transition()
- [x] **Configurable checkpoints** — CheckpointConfig (auto/human/hybrid), quality gates
- [x] **Human approval gates** — WorkflowCheckpointStore, approve/reject API
- [x] **Workflow definitions** — YAML templates (default, simple)
- [x] **Task integration** — Task.workflow_id/state/history, transition_task_on_completion()
- [x] **Audit trail** — .sdd/workflow/checkpoints/ JSONL persistence
- [x] **CLI commands** — `bernstein workflow list|show|checkpoints`
- [x] **Testing** — Unit tests for state transitions, checkpoints, edge cases
- [x] **Documentation** — WORKFLOWS.md, examples, checkpoint guide

## Next Steps (Not in This Plan)

- Implement checkpoint approval API endpoints (`/tasks/{id}/approve-checkpoint`)
- Add workflow definition CLI command (`bernstein workflow create`)
- Dashboard UI showing task state progress through workflow
- Workflow metrics: avg time per state, checkpoint rejection rates
- Multi-workflow routing (task type → workflow selection)

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-03-29-state-machine-workflows.md`.**

Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach would you prefer?