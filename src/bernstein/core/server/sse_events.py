"""Server-Sent Events (SSE) support for real-time task/agent updates.

Defines event types, a wire-format serialiser, and factory classmethods
for all Bernstein SSE event categories.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SSEEventType(StrEnum):
    """SSE event type identifiers."""

    TASK_CREATED = "task_created"
    TASK_CLAIMED = "task_claimed"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_RETRIED = "task_retried"
    AGENT_SPAWNED = "agent_spawned"
    AGENT_EXITED = "agent_exited"
    GATE_RESULT = "gate_result"
    COST_UPDATE = "cost_update"
    MERGE_STARTED = "merge_started"
    MERGE_COMPLETED = "merge_completed"
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    HEARTBEAT = "heartbeat"


@dataclass
class SSEEvent:
    """A single SSE event ready for wire serialisation.

    Args:
        event: The event type identifier.
        data: Arbitrary JSON-serialisable payload.
        id: Optional event ID for Last-Event-ID reconnection.
        timestamp: Epoch timestamp (defaults to now).
    """

    event: SSEEventType
    data: dict[str, Any] = field(default_factory=dict)
    id: str | None = None
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_sse(self) -> str:
        """Serialise to SSE wire format.

        Returns:
            SSE-formatted string with event, data, and optional id fields,
            terminated by a blank line.
        """
        lines: list[str] = []
        if self.id is not None:
            lines.append(f"id: {self.id}")
        lines.append(f"event: {self.event.value}")
        payload = dict(self.data)
        payload["timestamp"] = self.timestamp
        lines.append(f"data: {json.dumps(payload)}")
        lines.append("")  # trailing blank line
        return "\n".join(lines) + "\n"

    # -- factory classmethods ------------------------------------------------

    @classmethod
    def task_created(cls, task_id: str, title: str = "", **extra: Any) -> SSEEvent:
        """Create a task_created event.

        Args:
            task_id: Task identifier.
            title: Task title.
            **extra: Additional payload fields.

        Returns:
            SSEEvent instance.
        """
        return cls(
            event=SSEEventType.TASK_CREATED,
            data={"task_id": task_id, "title": title, **extra},
        )

    @classmethod
    def task_completed(cls, task_id: str, **extra: Any) -> SSEEvent:
        """Create a task_completed event.

        Args:
            task_id: Task identifier.
            **extra: Additional payload fields.

        Returns:
            SSEEvent instance.
        """
        return cls(
            event=SSEEventType.TASK_COMPLETED,
            data={"task_id": task_id, **extra},
        )

    @classmethod
    def task_failed(cls, task_id: str, reason: str = "", **extra: Any) -> SSEEvent:
        """Create a task_failed event.

        Args:
            task_id: Task identifier.
            reason: Failure reason.
            **extra: Additional payload fields.

        Returns:
            SSEEvent instance.
        """
        return cls(
            event=SSEEventType.TASK_FAILED,
            data={"task_id": task_id, "reason": reason, **extra},
        )

    @classmethod
    def agent_spawned(cls, agent_id: str, role: str = "", **extra: Any) -> SSEEvent:
        """Create an agent_spawned event.

        Args:
            agent_id: Agent identifier.
            role: Agent role.
            **extra: Additional payload fields.

        Returns:
            SSEEvent instance.
        """
        return cls(
            event=SSEEventType.AGENT_SPAWNED,
            data={"agent_id": agent_id, "role": role, **extra},
        )

    @classmethod
    def gate_result(cls, gate_name: str, passed: bool, **extra: Any) -> SSEEvent:
        """Create a gate_result event.

        Args:
            gate_name: Quality gate name.
            passed: Whether the gate passed.
            **extra: Additional payload fields.

        Returns:
            SSEEvent instance.
        """
        return cls(
            event=SSEEventType.GATE_RESULT,
            data={"gate_name": gate_name, "passed": passed, **extra},
        )

    @classmethod
    def cost_update(cls, total_usd: float, **extra: Any) -> SSEEvent:
        """Create a cost_update event.

        Args:
            total_usd: Current total cost in USD.
            **extra: Additional payload fields.

        Returns:
            SSEEvent instance.
        """
        return cls(
            event=SSEEventType.COST_UPDATE,
            data={"total_usd": total_usd, **extra},
        )

    @classmethod
    def merge_completed(cls, branch: str, result: str = "success", **extra: Any) -> SSEEvent:
        """Create a merge_completed event.

        Args:
            branch: Branch that was merged.
            result: Merge result.
            **extra: Additional payload fields.

        Returns:
            SSEEvent instance.
        """
        return cls(
            event=SSEEventType.MERGE_COMPLETED,
            data={"branch": branch, "result": result, **extra},
        )

    @classmethod
    def run_completed(cls, run_id: str, **extra: Any) -> SSEEvent:
        """Create a run_completed event.

        Args:
            run_id: Run identifier.
            **extra: Additional payload fields.

        Returns:
            SSEEvent instance.
        """
        return cls(
            event=SSEEventType.RUN_COMPLETED,
            data={"run_id": run_id, **extra},
        )
