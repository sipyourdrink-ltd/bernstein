"""Governed Workflow Mode — deterministic phase-based orchestration.

When governed mode is active, a run progresses through an ordered sequence
of phases (e.g. plan -> implement -> verify -> review -> merge).  Each phase
has entry guards (preconditions), allowed task roles, and optional human
approval checkpoints.

The workflow definition is hashed (SHA-256) so compliance auditors can verify
"this run used workflow version X".  The event stream (LifecycleEvents +
WorkflowPhaseEvents) is sufficient to deterministically replay the decision
trace of any governed run.

Usage:
    defn = GOVERNED_DEFAULT
    executor = WorkflowExecutor(defn, run_id="20240315-143022", sdd_dir=Path(".sdd"))
    # On each orchestrator tick:
    allowed = executor.allowed_task_ids(all_tasks)
    # When phase completion is detected:
    executor.try_advance(all_tasks)
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.lifecycle import _emit
from bernstein.core.models import (
    LifecycleEvent,
    Task,
    TaskStatus,
    WorkflowPhaseEvent,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowPhase:
    """A single phase in a governed workflow.

    Attributes:
        name: Short identifier (e.g. "plan", "implement").
        allowed_roles: Task roles permitted in this phase. Empty = all roles.
        completion_statuses: Task statuses that count as "phase done" for a
            task (default: DONE, CANCELLED).
        requires_approval: When True, the phase blocks on human approval
            before advancing (reuses plan_approval infrastructure).
        entry_guard_description: Human-readable description of what must be
            true before this phase can start.
    """

    name: str
    allowed_roles: frozenset[str] = frozenset()
    completion_statuses: frozenset[TaskStatus] = frozenset({TaskStatus.DONE, TaskStatus.CANCELLED})
    requires_approval: bool = False
    entry_guard_description: str = ""


@dataclass(frozen=True)
class WorkflowDefinition:
    """An ordered sequence of phases that defines a governed workflow.

    The definition is immutable and hashable so its identity can be
    recorded in run metadata for compliance purposes.

    Attributes:
        name: Human-readable workflow name.
        phases: Ordered tuple of phases.
        version: Semantic version string for the definition.
    """

    name: str
    phases: tuple[WorkflowPhase, ...]
    version: str = "1.0.0"

    def definition_hash(self) -> str:
        """Compute SHA-256 hash of the canonical JSON representation.

        This hash is recorded in run metadata so auditors can verify
        which workflow definition was used for a given run.
        """
        canonical: list[dict[str, object]] = []
        for phase in self.phases:
            canonical.append(
                {
                    "name": phase.name,
                    "allowed_roles": sorted(phase.allowed_roles),
                    "completion_statuses": sorted(s.value for s in phase.completion_statuses),
                    "requires_approval": phase.requires_approval,
                }
            )
        payload = json.dumps(
            {"name": self.name, "version": self.version, "phases": canonical},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def phase_names(self) -> list[str]:
        """Return ordered list of phase names."""
        return [p.name for p in self.phases]


# ---------------------------------------------------------------------------
# Built-in workflow definitions
# ---------------------------------------------------------------------------

GOVERNED_DEFAULT = WorkflowDefinition(
    name="governed",
    version="1.0.0",
    phases=(
        WorkflowPhase(
            name="plan",
            allowed_roles=frozenset({"manager", "architect"}),
            entry_guard_description="Run starts; manager decomposes the goal.",
        ),
        WorkflowPhase(
            name="implement",
            allowed_roles=frozenset(),  # all roles
            requires_approval=True,
            entry_guard_description="Plan approved; implementation begins.",
        ),
        WorkflowPhase(
            name="verify",
            allowed_roles=frozenset({"qa", "security"}),
            entry_guard_description="All implementation tasks completed.",
        ),
        WorkflowPhase(
            name="review",
            allowed_roles=frozenset({"manager", "architect"}),
            requires_approval=True,
            entry_guard_description="Verification passed; final review.",
        ),
        WorkflowPhase(
            name="merge",
            allowed_roles=frozenset({"manager"}),
            entry_guard_description="Review approved; merge to main.",
        ),
    ),
)

# Registry of known workflow definitions.
WORKFLOW_REGISTRY: dict[str, WorkflowDefinition] = {
    "governed": GOVERNED_DEFAULT,
}


# ---------------------------------------------------------------------------
# Workflow executor
# ---------------------------------------------------------------------------


class WorkflowExecutor:
    """Drives a run through a governed workflow's phase sequence.

    The executor tracks the current phase and blocks advancement until
    all tasks in the current phase are complete and guards pass.  It
    emits ``WorkflowPhaseEvent`` on every phase transition and persists
    the event log + definition hash to ``.sdd/runtime/workflow/``.

    Args:
        definition: The workflow definition to execute.
        run_id: Unique identifier for the orchestration run.
        sdd_dir: Path to the ``.sdd`` directory for persistence.
    """

    def __init__(
        self,
        definition: WorkflowDefinition,
        run_id: str,
        sdd_dir: Path,
    ) -> None:
        self._definition = definition
        self._run_id = run_id
        self._phase_index: int = 0
        self._approval_pending: bool = False
        self._approval_granted: bool = False  # True after approval given, reset on phase advance
        self._completed: bool = False
        self._events: list[WorkflowPhaseEvent] = []

        # Persistence directory
        self._workflow_dir = sdd_dir / "runtime" / "workflow"
        self._workflow_dir.mkdir(parents=True, exist_ok=True)

        # Write definition hash on init
        self._hash = definition.definition_hash()
        (self._workflow_dir / "definition_hash.txt").write_text(self._hash)
        (self._workflow_dir / "definition.json").write_text(
            json.dumps(
                {
                    "name": definition.name,
                    "version": definition.version,
                    "hash": self._hash,
                    "phases": [p.name for p in definition.phases],
                },
                indent=2,
            )
        )

        # Emit initial phase entry
        self._emit_phase_event(from_phase="", to_phase=self.current_phase.name, reason="workflow started")

    @property
    def definition(self) -> WorkflowDefinition:
        """The workflow definition being executed."""
        return self._definition

    @property
    def definition_hash(self) -> str:
        """SHA-256 hash of the workflow definition."""
        return self._hash

    @property
    def current_phase(self) -> WorkflowPhase:
        """The currently active phase."""
        return self._definition.phases[self._phase_index]

    @property
    def current_phase_name(self) -> str:
        """Name of the currently active phase."""
        return self.current_phase.name

    @property
    def phase_index(self) -> int:
        """Zero-based index of the current phase."""
        return self._phase_index

    @property
    def is_completed(self) -> bool:
        """True when all phases have been completed."""
        return self._completed

    @property
    def approval_pending(self) -> bool:
        """True when the current phase is waiting for human approval."""
        return self._approval_pending

    @property
    def events(self) -> list[WorkflowPhaseEvent]:
        """All phase events emitted so far."""
        return list(self._events)

    def grant_approval(self, reason: str = "approved") -> None:
        """Grant human approval for the current phase.

        Called by the approval route / CLI when a human approves advancement.
        """
        if self._approval_pending:
            self._approval_pending = False
            self._approval_granted = True
            logger.info("Workflow approval granted for phase %r: %s", self.current_phase_name, reason)

    def filter_tasks_for_current_phase(self, tasks: list[Task]) -> list[Task]:
        """Return tasks that are allowed to execute in the current phase.

        When a phase specifies ``allowed_roles``, only tasks matching those
        roles are included.  When ``allowed_roles`` is empty, all tasks pass.

        Args:
            tasks: All open/ready tasks.

        Returns:
            Subset of tasks allowed in the current phase.
        """
        if self._completed:
            return tasks  # workflow done, no filtering

        phase = self.current_phase
        if not phase.allowed_roles:
            return tasks  # no role restriction

        return [t for t in tasks if t.role in phase.allowed_roles]

    def phase_tasks_complete(self, all_tasks: list[Task]) -> bool:
        """Check whether all tasks relevant to the current phase are complete.

        A phase is considered complete when every task whose role matches the
        phase's ``allowed_roles`` is in a terminal status. If a phase has no
        role restrictions, ALL tasks must be terminal.

        Args:
            all_tasks: Every task in the run (all statuses).

        Returns:
            True if the current phase's tasks are all in terminal statuses.
        """
        if self._completed:
            return True

        phase = self.current_phase
        terminal = phase.completion_statuses

        if phase.allowed_roles:
            phase_tasks = [t for t in all_tasks if t.role in phase.allowed_roles]
        else:
            phase_tasks = list(all_tasks)

        if not phase_tasks:
            return False  # no tasks yet = not complete

        return all(t.status in terminal for t in phase_tasks)

    def try_advance(self, all_tasks: list[Task]) -> WorkflowPhaseEvent | None:
        """Attempt to advance to the next phase.

        Checks phase completion and approval status. If the phase is complete
        and no approval is pending, advances to the next phase and emits a
        ``WorkflowPhaseEvent``.

        Args:
            all_tasks: Every task in the run (all statuses).

        Returns:
            The emitted WorkflowPhaseEvent if a transition occurred, else None.
        """
        if self._completed:
            return None

        if not self.phase_tasks_complete(all_tasks):
            return None

        # Check if approval is needed and hasn't been granted yet
        if self.current_phase.requires_approval and not self._approval_granted:
            if not self._approval_pending:
                self._approval_pending = True
                logger.info(
                    "Workflow phase %r complete — awaiting human approval before advancing",
                    self.current_phase_name,
                )
                self._write_approval_request()
            return None  # still waiting for approval

        # Advance to next phase
        old_phase = self.current_phase_name
        completed_ids = tuple(t.id for t in all_tasks if t.status in self.current_phase.completion_statuses)

        self._phase_index += 1
        self._approval_granted = False  # reset for next phase

        if self._phase_index >= len(self._definition.phases):
            # Workflow complete
            self._completed = True
            event = self._emit_phase_event(
                from_phase=old_phase,
                to_phase="completed",
                reason="all phases completed",
                tasks_completed=completed_ids,
            )
            logger.info("Governed workflow completed (all %d phases done)", len(self._definition.phases))
            return event

        new_phase = self.current_phase_name
        event = self._emit_phase_event(
            from_phase=old_phase,
            to_phase=new_phase,
            reason=f"phase {old_phase!r} tasks completed; advancing to {new_phase!r}",
            tasks_completed=completed_ids,
        )
        logger.info("Workflow advanced: %s -> %s", old_phase, new_phase)
        return event

    def to_dict(self) -> dict[str, object]:
        """Serialize executor state for persistence / dashboard display."""
        return {
            "workflow_name": self._definition.name,
            "workflow_version": self._definition.version,
            "workflow_hash": self._hash,
            "current_phase": self.current_phase_name if not self._completed else "completed",
            "phase_index": self._phase_index,
            "total_phases": len(self._definition.phases),
            "approval_pending": self._approval_pending,
            "completed": self._completed,
            "phase_names": self._definition.phase_names(),
            "events_count": len(self._events),
        }

    # -- Internal helpers ---------------------------------------------------

    def _emit_phase_event(
        self,
        *,
        from_phase: str,
        to_phase: str,
        reason: str,
        tasks_completed: tuple[str, ...] = (),
    ) -> WorkflowPhaseEvent:
        """Create, persist, and emit a WorkflowPhaseEvent."""
        event = WorkflowPhaseEvent(
            timestamp=time.time(),
            workflow_hash=self._hash,
            run_id=self._run_id,
            from_phase=from_phase,
            to_phase=to_phase,
            reason=reason,
            tasks_completed=tasks_completed,
        )
        self._events.append(event)
        self._persist_event(event)

        # Also emit as a LifecycleEvent so existing listeners see it
        lifecycle_event = LifecycleEvent(
            timestamp=event.timestamp,
            entity_type="task",
            entity_id=f"workflow:{self._definition.name}",
            from_status=from_phase or "(start)",
            to_status=to_phase,
            actor="workflow_executor",
            reason=reason,
        )
        _emit(lifecycle_event)

        return event

    def _persist_event(self, event: WorkflowPhaseEvent) -> None:
        """Append a phase event to the workflow event log."""
        log_path = self._workflow_dir / "events.jsonl"
        entry = {
            "ts": event.timestamp,
            "workflow_hash": event.workflow_hash,
            "run_id": event.run_id,
            "from_phase": event.from_phase,
            "to_phase": event.to_phase,
            "reason": event.reason,
            "tasks_completed": list(event.tasks_completed),
        }
        try:
            with log_path.open("a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            logger.warning("Failed to persist workflow event: %s", exc)

    def _write_approval_request(self) -> None:
        """Write an approval-pending file for the current phase."""
        approval_file = self._workflow_dir / f"approval_pending_{self.current_phase_name}.json"
        approval_file.write_text(
            json.dumps(
                {
                    "phase": self.current_phase_name,
                    "workflow": self._definition.name,
                    "workflow_hash": self._hash,
                    "run_id": self._run_id,
                    "requested_at": time.time(),
                    "description": self.current_phase.entry_guard_description,
                },
                indent=2,
            )
        )
        logger.info(
            "Approval request written: %s (POST /workflow/approve or touch approve_%s)",
            approval_file,
            self.current_phase_name,
        )


def load_workflow(name: str) -> WorkflowDefinition | None:
    """Look up a workflow definition by name.

    First checks the built-in registry, then searches for a DSL YAML
    file in ``.bernstein/workflows/``.

    Args:
        name: Workflow name (e.g. "governed") or DSL file name.

    Returns:
        The WorkflowDefinition if found, else None.
    """
    if name in WORKFLOW_REGISTRY:
        return WORKFLOW_REGISTRY[name]

    # Try loading from DSL file.
    try:
        from bernstein.core.workflow_dsl import load_workflow_dsl

        dag = load_workflow_dsl(name)
        if dag is not None:
            return dag.definition
    except Exception:
        logger.debug("Failed to load workflow DSL %r", name, exc_info=True)

    return None


def load_workflow_dag(name: str) -> Any:
    """Load a full WorkflowDAG (with conditional edges) by name.

    Returns None if the name resolves to a built-in definition or is
    not found.  Use ``load_workflow()`` for definitions without DAG
    structure.

    Args:
        name: Workflow name or DSL file name.

    Returns:
        WorkflowDAG if found, else None.
    """
    if name in WORKFLOW_REGISTRY:
        return None  # Built-in, no DAG structure.

    try:
        from bernstein.core.workflow_dsl import load_workflow_dsl

        return load_workflow_dsl(name)
    except Exception:
        logger.debug("Failed to load workflow DAG %r", name, exc_info=True)
        return None
