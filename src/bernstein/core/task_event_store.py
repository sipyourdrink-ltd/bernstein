"""Append-only event-sourced task transition store.

Each task gets a separate JSONL file under ``.sdd/events/{task_id}.jsonl``.
Events are immutable, append-only records of every lifecycle transition.

Usage::

    from pathlib import Path
    from bernstein.core.task_event_store import (
        TaskEventKind, TaskEventStore, record_transition,
    )

    store = TaskEventStore(Path(".sdd/events"))
    evt = record_transition(store, "task-1", TaskEventKind.CREATED, "orchestrator")
    print(store.current_state("task-1"))  # "CREATED"
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class TaskEventKind(StrEnum):
    """Canonical lifecycle events for a task."""

    CREATED = "CREATED"
    CLAIMED = "CLAIMED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    VERIFIED = "VERIFIED"
    MERGED = "MERGED"
    CLOSED = "CLOSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    UNBLOCKED = "UNBLOCKED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class TaskEvent:
    """An immutable record of a single task lifecycle transition.

    Attributes:
        task_id: Identifier of the task this event belongs to.
        kind: The type of lifecycle event.
        timestamp: ISO 8601 timestamp of when the event occurred.
        actor: Who caused this event (agent id or ``"orchestrator"``).
        metadata: Optional extra context for the event.
    """

    task_id: str
    kind: TaskEventKind
    timestamp: str
    actor: str
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, Any]:
        """Serialize this event to a plain dictionary."""
        return {
            "task_id": self.task_id,
            "kind": str(self.kind),
            "timestamp": self.timestamp,
            "actor": self.actor,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskEvent:
        """Deserialize a dictionary back into a ``TaskEvent``.

        Args:
            data: Dictionary with ``task_id``, ``kind``, ``timestamp``,
                ``actor``, and optional ``metadata`` keys.

        Returns:
            A new frozen ``TaskEvent`` instance.
        """
        return cls(
            task_id=data["task_id"],
            kind=TaskEventKind(data["kind"]),
            timestamp=data["timestamp"],
            actor=data["actor"],
            metadata=data.get("metadata", {}),
        )


class TaskEventStore:
    """File-backed, append-only event store for task transitions.

    Each task's events live in a separate JSONL file so that reads/writes
    for one task never contend with another.

    Args:
        store_path: Directory where ``{task_id}.jsonl`` files are stored.
    """

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._path.mkdir(parents=True, exist_ok=True)

    def _task_file(self, task_id: str) -> Path:
        """Return the JSONL path for a given task."""
        return self._path / f"{task_id}.jsonl"

    def append(self, event: TaskEvent) -> None:
        """Append an event to the task's JSONL file.

        Args:
            event: The event to persist.
        """
        path = self._task_file(event.task_id)
        line = json.dumps(event.to_dict(), separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def events_for(self, task_id: str) -> list[TaskEvent]:
        """Read all events for a task in chronological order.

        Args:
            task_id: The task to look up.

        Returns:
            List of events ordered oldest-first.  Empty list if the task
            has no recorded events.
        """
        path = self._task_file(task_id)
        if not path.exists():
            return []
        events: list[TaskEvent] = []
        with path.open(encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                    events.append(TaskEvent.from_dict(data))
                except (json.JSONDecodeError, KeyError, ValueError):
                    logger.warning(
                        "Skipping corrupt event line %d in %s",
                        lineno,
                        path,
                    )
        return events

    def current_state(self, task_id: str) -> str | None:
        """Derive the current status of a task from its latest event.

        Args:
            task_id: The task to query.

        Returns:
            The ``TaskEventKind`` value of the most recent event, or
            ``None`` if no events exist.
        """
        events = self.events_for(task_id)
        if not events:
            return None
        return str(events[-1].kind)

    def all_task_ids(self) -> list[str]:
        """List every task that has at least one recorded event.

        Returns:
            Sorted list of task ID strings.
        """
        ids: list[str] = []
        for p in self._path.iterdir():
            if p.suffix == ".jsonl" and p.is_file():
                ids.append(p.stem)
        ids.sort()
        return ids


def record_transition(
    store: TaskEventStore,
    task_id: str,
    kind: TaskEventKind,
    actor: str,
    **metadata: Any,
) -> TaskEvent:
    """Create and persist a task event in one call.

    Args:
        store: The event store to write to.
        task_id: Task this event belongs to.
        kind: The lifecycle transition.
        actor: Who triggered the transition.
        **metadata: Arbitrary key-value pairs stored alongside the event.

    Returns:
        The newly created (and already persisted) ``TaskEvent``.
    """
    event = TaskEvent(
        task_id=task_id,
        kind=kind,
        timestamp=datetime.now(UTC).isoformat(),
        actor=actor,
        metadata=metadata if metadata else {},
    )
    store.append(event)
    return event
