"""Typed models for the Bernstein SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    """Bernstein task lifecycle states."""

    OPEN = "open"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


class TaskScope(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class TaskComplexity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class TaskCreate:
    """Parameters for creating a new task.

    Args:
        title: Short, imperative description (e.g. ``"Fix login regression"``).
        role: Agent role to assign (``"backend"``, ``"qa"``, ``"security"`` …).
        description: Full task brief shown to the agent.
        priority: 1 = critical, 2 = normal, 3 = nice-to-have.
        scope: Rough size estimate for routing and scheduling.
        complexity: Reasoning complexity hint for model selection.
        estimated_minutes: Expected wall-clock minutes to complete.
        depends_on: List of task IDs that must finish first.
        external_ref: Opaque string linking back to the source issue
            (e.g. ``"jira:PROJ-42"`` or ``"linear:ISS-99"``).
        metadata: Arbitrary key-value pairs stored on the task.
    """

    title: str
    role: str = "backend"
    description: str = ""
    priority: int = 2
    scope: TaskScope = TaskScope.MEDIUM
    complexity: TaskComplexity = TaskComplexity.MEDIUM
    estimated_minutes: int = 30
    depends_on: list[str] = field(default_factory=list)
    external_ref: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_api_payload(self) -> dict[str, Any]:
        """Serialize to the Bernstein REST API payload format."""
        payload: dict[str, Any] = {
            "title": self.title,
            "role": self.role,
            "description": self.description,
            "priority": self.priority,
            "scope": self.scope.value,
            "complexity": self.complexity.value,
            "estimated_minutes": self.estimated_minutes,
        }
        if self.depends_on:
            payload["depends_on"] = self.depends_on
        if self.external_ref:
            payload["external_ref"] = self.external_ref
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


@dataclass
class TaskUpdate:
    """Partial update for an existing task.

    Only non-``None`` fields are sent to the server.
    """

    status: TaskStatus | None = None
    result_summary: str | None = None
    error: str | None = None

    def to_api_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.status is not None:
            payload["status"] = self.status.value
        if self.result_summary is not None:
            payload["result_summary"] = self.result_summary
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass
class TaskResponse:
    """A task as returned by the Bernstein task server.

    Args:
        id: Unique 12-hex-char task identifier.
        title: Human-readable title.
        role: Assigned agent role.
        status: Current lifecycle state.
        priority: 1–3 (1 = critical).
        scope: Size estimate.
        complexity: Reasoning complexity.
        description: Full task brief.
        assigned_agent: Session ID of the agent currently working this task.
        result_summary: Set when the task reaches ``done``.
        external_ref: Opaque back-reference to the originating issue.
        metadata: Arbitrary key-value pairs.
        created_at: Unix timestamp of task creation.
    """

    id: str
    title: str
    role: str
    status: TaskStatus
    priority: int
    scope: str
    complexity: str
    description: str = ""
    assigned_agent: str | None = None
    result_summary: str | None = None
    external_ref: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "TaskResponse":
        """Deserialize from a raw API response dict."""
        return cls(
            id=data["id"],
            title=data["title"],
            role=data.get("role", "backend"),
            status=TaskStatus(data.get("status", "open")),
            priority=data.get("priority", 2),
            scope=data.get("scope", "medium"),
            complexity=data.get("complexity", "medium"),
            description=data.get("description", ""),
            assigned_agent=data.get("assigned_agent"),
            result_summary=data.get("result_summary"),
            external_ref=data.get("external_ref", ""),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", 0.0),
        )


@dataclass
class StatusSummary:
    """Aggregate statistics from ``GET /status``."""

    total: int
    open: int
    claimed: int
    done: int
    failed: int
    agents: int
    cost_usd: float

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "StatusSummary":
        return cls(
            total=data.get("total", 0),
            open=data.get("open", 0),
            claimed=data.get("claimed", 0),
            done=data.get("done", 0),
            failed=data.get("failed", 0),
            agents=data.get("agents", 0),
            cost_usd=float(data.get("cost_usd", 0.0)),
        )
