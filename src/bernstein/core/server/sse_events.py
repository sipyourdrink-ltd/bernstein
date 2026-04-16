"""Structured SSE event types for real-time streaming.

Defines typed events for task lifecycle, agent lifecycle, quality gates,
cost updates, and merge operations. Each event has a structured JSON
payload with consistent schema.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class SSEEventType(StrEnum):
    """Event types for Server-Sent Events stream."""

    TASK_CREATED = "task.created"
    TASK_CLAIMED = "task.claimed"
    TASK_PROGRESS = "task.progress"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    AGENT_SPAWNED = "agent.spawned"
    AGENT_COMPLETED = "agent.completed"
    AGENT_FAILED = "agent.failed"
    GATE_STARTED = "gate.started"
    GATE_PASSED = "gate.passed"
    GATE_FAILED = "gate.failed"
    COST_UPDATE = "cost.update"
    MERGE_COMPLETED = "merge.completed"
    RUN_COMPLETED = "run.completed"


@dataclass
class SSEEvent:
    """A structured SSE event with type and payload."""

    event_type: SSEEventType
    data: dict[str, Any]
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = time.time()

    def to_sse(self) -> str:
        """Format as SSE wire format: event: type\\ndata: json\\n\\n"""
        payload = {"timestamp": self.timestamp, **self.data}
        return f"event: {self.event_type.value}\ndata: {json.dumps(payload)}\n\n"

    @classmethod
    def task_created(cls, task_id: str, goal: str, role: str, complexity: str) -> SSEEvent:
        """Create a task-created event."""
        return cls(
            SSEEventType.TASK_CREATED,
            {
                "task_id": task_id,
                "goal": goal,
                "role": role,
                "complexity": complexity,
            },
        )

    @classmethod
    def task_completed(
        cls,
        task_id: str,
        agent_id: str,
        model: str,
        duration_s: float,
        cost_usd: float,
    ) -> SSEEvent:
        """Create a task-completed event."""
        return cls(
            SSEEventType.TASK_COMPLETED,
            {
                "task_id": task_id,
                "agent_id": agent_id,
                "model": model,
                "duration_s": round(duration_s, 2),
                "cost_usd": round(cost_usd, 4),
            },
        )

    @classmethod
    def task_failed(cls, task_id: str, reason: str, will_retry: bool) -> SSEEvent:
        """Create a task-failed event."""
        return cls(
            SSEEventType.TASK_FAILED,
            {"task_id": task_id, "reason": reason, "will_retry": will_retry},
        )

    @classmethod
    def agent_spawned(cls, agent_id: str, task_id: str, model: str, adapter: str) -> SSEEvent:
        """Create an agent-spawned event."""
        return cls(
            SSEEventType.AGENT_SPAWNED,
            {
                "agent_id": agent_id,
                "task_id": task_id,
                "model": model,
                "adapter": adapter,
            },
        )

    @classmethod
    def gate_result(cls, task_id: str, gate_name: str, passed: bool, details: str = "") -> SSEEvent:
        """Create a quality-gate result event."""
        event_type = SSEEventType.GATE_PASSED if passed else SSEEventType.GATE_FAILED
        return cls(
            event_type,
            {
                "task_id": task_id,
                "gate": gate_name,
                "passed": passed,
                "details": details,
            },
        )

    @classmethod
    def cost_update(cls, total_usd: float, budget_usd: float, budget_pct: float) -> SSEEvent:
        """Create a cost-update event."""
        return cls(
            SSEEventType.COST_UPDATE,
            {
                "total_usd": round(total_usd, 4),
                "budget_usd": round(budget_usd, 2),
                "budget_pct": round(budget_pct, 1),
            },
        )

    @classmethod
    def merge_completed(cls, task_id: str, branch: str, commit_sha: str) -> SSEEvent:
        """Create a merge-completed event."""
        return cls(
            SSEEventType.MERGE_COMPLETED,
            {"task_id": task_id, "branch": branch, "commit_sha": commit_sha},
        )

    @classmethod
    def run_completed(cls, total_tasks: int, passed: int, failed: int, total_cost: float) -> SSEEvent:
        """Create a run-completed event."""
        return cls(
            SSEEventType.RUN_COMPLETED,
            {
                "total_tasks": total_tasks,
                "passed": passed,
                "failed": failed,
                "total_cost_usd": round(total_cost, 4),
            },
        )
