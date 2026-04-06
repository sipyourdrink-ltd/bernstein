"""Complete hook event taxonomy for Bernstein orchestration.

Defines a strongly-typed enum of all hook events that flow through the
system, plus typed payload dataclasses for each event category.  Every
hook emission site should reference ``HookEvent`` members rather than
raw strings so that typos are caught at import time and consumers can
exhaustively match on the enum.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Any

# ---------------------------------------------------------------------------
# Event enum
# ---------------------------------------------------------------------------


@unique
class HookEvent(Enum):
    """Canonical hook event names used across Bernstein.

    Naming convention: ``<domain>.<action>`` in lowercase with dots.
    """

    # -- Task lifecycle --
    TASK_CREATED = "task.created"
    TASK_CLAIMED = "task.claimed"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_RETRIED = "task.retried"

    # -- Agent lifecycle --
    AGENT_SPAWNED = "agent.spawned"
    AGENT_HEARTBEAT = "agent.heartbeat"
    AGENT_COMPLETED = "agent.completed"
    AGENT_KILLED = "agent.killed"
    AGENT_STALLED = "agent.stalled"

    # -- Merge / git operations --
    MERGE_STARTED = "merge.started"
    MERGE_COMPLETED = "merge.completed"
    MERGE_CONFLICT = "merge.conflict"

    # -- Quality gates --
    QUALITY_GATE_PASSED = "quality_gate.passed"
    QUALITY_GATE_FAILED = "quality_gate.failed"

    # -- Budget --
    BUDGET_THRESHOLD = "budget.threshold"
    BUDGET_EXCEEDED = "budget.exceeded"

    # -- Configuration --
    CONFIG_DRIFT = "config.drift"

    # -- Orchestrator lifecycle --
    ORCHESTRATOR_TICK = "orchestrator.tick"
    ORCHESTRATOR_STARTUP = "orchestrator.startup"
    ORCHESTRATOR_SHUTDOWN = "orchestrator.shutdown"

    # -- Plan execution --
    PLAN_LOADED = "plan.loaded"
    PLAN_STAGE_COMPLETED = "plan.stage_completed"

    # -- Permissions --
    PERMISSION_DENIED = "permission.denied"
    PERMISSION_ESCALATED = "permission.escalated"

    # -- Security --
    SECRET_DETECTED = "secret.detected"

    # -- Reliability --
    CIRCUIT_BREAKER_TRIPPED = "circuit_breaker.tripped"

    # -- Cluster --
    CLUSTER_NODE_JOINED = "cluster.node_joined"

    # -- Blocking pre-action events (HOOK-002) --
    PRE_MERGE = "pre_merge"
    PRE_SPAWN = "pre_spawn"
    PRE_APPROVE = "pre_approve"


# Convenience sets for filtering.
TASK_EVENTS: frozenset[HookEvent] = frozenset(
    {
        HookEvent.TASK_CREATED,
        HookEvent.TASK_CLAIMED,
        HookEvent.TASK_COMPLETED,
        HookEvent.TASK_FAILED,
        HookEvent.TASK_RETRIED,
    }
)

AGENT_EVENTS: frozenset[HookEvent] = frozenset(
    {
        HookEvent.AGENT_SPAWNED,
        HookEvent.AGENT_HEARTBEAT,
        HookEvent.AGENT_COMPLETED,
        HookEvent.AGENT_KILLED,
        HookEvent.AGENT_STALLED,
    }
)

MERGE_EVENTS: frozenset[HookEvent] = frozenset(
    {
        HookEvent.MERGE_STARTED,
        HookEvent.MERGE_COMPLETED,
        HookEvent.MERGE_CONFLICT,
    }
)

BLOCKING_EVENTS: frozenset[HookEvent] = frozenset(
    {
        HookEvent.PRE_MERGE,
        HookEvent.PRE_SPAWN,
        HookEvent.PRE_APPROVE,
    }
)


# ---------------------------------------------------------------------------
# Base payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookPayload:
    """Base payload attached to every hook event emission.

    Attributes:
        event: The hook event that triggered this payload.
        timestamp: Unix epoch seconds when the event was created.
        metadata: Arbitrary extra key-value pairs for downstream consumers.
    """

    event: HookEvent
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, Any]:
        """Serialise the payload to a JSON-friendly dict."""
        d: dict[str, Any] = {
            "event": self.event.value,
            "timestamp": self.timestamp,
        }
        if self.metadata:
            d["metadata"] = self.metadata
        return d


# ---------------------------------------------------------------------------
# Domain-specific payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskPayload(HookPayload):
    """Payload for task lifecycle events.

    Attributes:
        task_id: Server-assigned task identifier.
        role: Agent role assigned to the task.
        title: Human-readable task title.
        error: Error detail (only for ``task.failed``).
        retry_count: How many retries have been attempted (only for ``task.retried``).
    """

    task_id: str = ""
    role: str = ""
    title: str = ""
    error: str = ""
    retry_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["task_id"] = self.task_id
        d["role"] = self.role
        d["title"] = self.title
        if self.error:
            d["error"] = self.error
        if self.retry_count:
            d["retry_count"] = self.retry_count
        return d


@dataclass(frozen=True)
class AgentPayload(HookPayload):
    """Payload for agent lifecycle events.

    Attributes:
        session_id: Unique agent session identifier.
        role: Agent role (e.g. ``"backend"``).
        model: Model identifier used for the session.
        reason: Kill/stall reason when applicable.
    """

    session_id: str = ""
    role: str = ""
    model: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["session_id"] = self.session_id
        d["role"] = self.role
        if self.model:
            d["model"] = self.model
        if self.reason:
            d["reason"] = self.reason
        return d


@dataclass(frozen=True)
class MergePayload(HookPayload):
    """Payload for merge / git operation events.

    Attributes:
        branch: The branch being merged.
        target: The target branch.
        conflict_files: List of conflicting files (only for ``merge.conflict``).
    """

    branch: str = ""
    target: str = ""
    conflict_files: list[str] = field(default_factory=list[str])

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["branch"] = self.branch
        d["target"] = self.target
        if self.conflict_files:
            d["conflict_files"] = self.conflict_files
        return d


@dataclass(frozen=True)
class QualityGatePayload(HookPayload):
    """Payload for quality gate events.

    Attributes:
        gate_name: Name of the quality gate (e.g. ``"lint"``, ``"tests"``).
        task_id: Task that triggered the gate.
        details: Gate-specific result details.
    """

    gate_name: str = ""
    task_id: str = ""
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["gate_name"] = self.gate_name
        if self.task_id:
            d["task_id"] = self.task_id
        if self.details:
            d["details"] = self.details
        return d


@dataclass(frozen=True)
class BudgetPayload(HookPayload):
    """Payload for budget threshold / exceeded events.

    Attributes:
        current_spend_usd: Current cumulative spend.
        budget_usd: Configured budget cap.
        percent: Spend as a percentage of the cap.
    """

    current_spend_usd: float = 0.0
    budget_usd: float = 0.0
    percent: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["current_spend_usd"] = self.current_spend_usd
        d["budget_usd"] = self.budget_usd
        d["percent"] = self.percent
        return d


@dataclass(frozen=True)
class ConfigDriftPayload(HookPayload):
    """Payload for configuration drift events.

    Attributes:
        key: Configuration key that drifted.
        expected: Expected value.
        actual: Observed value.
    """

    key: str = ""
    expected: str = ""
    actual: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["key"] = self.key
        d["expected"] = self.expected
        d["actual"] = self.actual
        return d


@dataclass(frozen=True)
class OrchestratorPayload(HookPayload):
    """Payload for orchestrator lifecycle events.

    Attributes:
        tick_number: Current tick count (only for ``orchestrator.tick``).
        active_agents: Number of currently-running agents.
        open_tasks: Number of tasks in open state.
    """

    tick_number: int = 0
    active_agents: int = 0
    open_tasks: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["tick_number"] = self.tick_number
        d["active_agents"] = self.active_agents
        d["open_tasks"] = self.open_tasks
        return d


@dataclass(frozen=True)
class PlanPayload(HookPayload):
    """Payload for plan execution events.

    Attributes:
        plan_path: Filesystem path to the plan YAML.
        stage_name: Name of the completed stage (only for ``plan.stage_completed``).
        total_stages: Total number of stages in the plan.
    """

    plan_path: str = ""
    stage_name: str = ""
    total_stages: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["plan_path"] = self.plan_path
        if self.stage_name:
            d["stage_name"] = self.stage_name
        if self.total_stages:
            d["total_stages"] = self.total_stages
        return d


@dataclass(frozen=True)
class PermissionPayload(HookPayload):
    """Payload for permission events.

    Attributes:
        task_id: Task requesting the permission.
        tool: Tool or action that was denied/escalated.
        reason: Reason for the denial or escalation.
    """

    task_id: str = ""
    tool: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["task_id"] = self.task_id
        d["tool"] = self.tool
        d["reason"] = self.reason
        return d


@dataclass(frozen=True)
class SecretDetectedPayload(HookPayload):
    """Payload for secret detection events.

    Attributes:
        file_path: File where the secret was found.
        secret_type: Category of the detected secret (e.g. ``"api_key"``).
        line_number: Approximate line number.
    """

    file_path: str = ""
    secret_type: str = ""
    line_number: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["file_path"] = self.file_path
        d["secret_type"] = self.secret_type
        if self.line_number:
            d["line_number"] = self.line_number
        return d


@dataclass(frozen=True)
class CircuitBreakerPayload(HookPayload):
    """Payload for circuit breaker events.

    Attributes:
        breaker_name: Name of the circuit breaker.
        failure_count: Number of consecutive failures that triggered the trip.
        cooldown_s: Seconds until the breaker attempts to half-open.
    """

    breaker_name: str = ""
    failure_count: int = 0
    cooldown_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["breaker_name"] = self.breaker_name
        d["failure_count"] = self.failure_count
        d["cooldown_s"] = self.cooldown_s
        return d


@dataclass(frozen=True)
class ClusterPayload(HookPayload):
    """Payload for cluster events.

    Attributes:
        node_id: Identifier of the node that joined.
        node_address: Network address of the joining node.
    """

    node_id: str = ""
    node_address: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["node_id"] = self.node_id
        d["node_address"] = self.node_address
        return d


@dataclass(frozen=True)
class BlockingHookPayload(HookPayload):
    """Payload for blocking pre-action events (HOOK-002).

    Attributes:
        action: The action being gated (e.g. ``"merge"``, ``"spawn"``, ``"approve"``).
        context: Arbitrary context about the action being attempted.
    """

    action: str = ""
    context: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["action"] = self.action
        if self.context:
            d["context"] = self.context
        return d


# ---------------------------------------------------------------------------
# Payload factory
# ---------------------------------------------------------------------------

# Maps each event to its payload class for convenient construction.
EVENT_PAYLOAD_MAP: dict[HookEvent, type[HookPayload]] = {
    HookEvent.TASK_CREATED: TaskPayload,
    HookEvent.TASK_CLAIMED: TaskPayload,
    HookEvent.TASK_COMPLETED: TaskPayload,
    HookEvent.TASK_FAILED: TaskPayload,
    HookEvent.TASK_RETRIED: TaskPayload,
    HookEvent.AGENT_SPAWNED: AgentPayload,
    HookEvent.AGENT_HEARTBEAT: AgentPayload,
    HookEvent.AGENT_COMPLETED: AgentPayload,
    HookEvent.AGENT_KILLED: AgentPayload,
    HookEvent.AGENT_STALLED: AgentPayload,
    HookEvent.MERGE_STARTED: MergePayload,
    HookEvent.MERGE_COMPLETED: MergePayload,
    HookEvent.MERGE_CONFLICT: MergePayload,
    HookEvent.QUALITY_GATE_PASSED: QualityGatePayload,
    HookEvent.QUALITY_GATE_FAILED: QualityGatePayload,
    HookEvent.BUDGET_THRESHOLD: BudgetPayload,
    HookEvent.BUDGET_EXCEEDED: BudgetPayload,
    HookEvent.CONFIG_DRIFT: ConfigDriftPayload,
    HookEvent.ORCHESTRATOR_TICK: OrchestratorPayload,
    HookEvent.ORCHESTRATOR_STARTUP: OrchestratorPayload,
    HookEvent.ORCHESTRATOR_SHUTDOWN: OrchestratorPayload,
    HookEvent.PLAN_LOADED: PlanPayload,
    HookEvent.PLAN_STAGE_COMPLETED: PlanPayload,
    HookEvent.PERMISSION_DENIED: PermissionPayload,
    HookEvent.PERMISSION_ESCALATED: PermissionPayload,
    HookEvent.SECRET_DETECTED: SecretDetectedPayload,
    HookEvent.CIRCUIT_BREAKER_TRIPPED: CircuitBreakerPayload,
    HookEvent.CLUSTER_NODE_JOINED: ClusterPayload,
    HookEvent.PRE_MERGE: BlockingHookPayload,
    HookEvent.PRE_SPAWN: BlockingHookPayload,
    HookEvent.PRE_APPROVE: BlockingHookPayload,
}


def payload_class_for(event: HookEvent) -> type[HookPayload]:
    """Return the payload dataclass for a given event.

    Falls back to the base ``HookPayload`` if no specific mapping exists.

    Args:
        event: The hook event to look up.

    Returns:
        The corresponding payload class.
    """
    return EVENT_PAYLOAD_MAP.get(event, HookPayload)
