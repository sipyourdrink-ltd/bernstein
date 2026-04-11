"""Task search, filter parsing, and input widget for the TUI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from textual.widgets import Input

if TYPE_CHECKING:
    from textual.events import Key


class SupportsTaskSearch(Protocol):
    """Protocol for task rows that can be searched/filtered."""

    task_id: str
    title: str
    role: str
    status: str
    priority: int
    model: str
    assigned_agent: str
    blocked_reason: str


@dataclass(frozen=True)
class TaskSearchQuery:
    """Parsed search query for task filtering."""

    text_terms: tuple[str, ...] = ()
    status: str | None = None
    role: str | None = None
    agent: str | None = None
    priority: int | None = None
    raw_filters: dict[str, str] = field(default_factory=dict[str, str])


_FILTER_PREFIXES: tuple[str, ...] = ("status", "role", "agent", "priority", "p")


def parse_task_search(query: str) -> TaskSearchQuery:
    """Parse a free-form task search query into structured filters."""

    raw_filters: dict[str, str] = {}
    text_terms: list[str] = []

    for token in query.split():
        key, sep, value = token.partition(":")
        normalized_key = key.lower().strip()
        normalized_value = value.lower().strip()
        if sep and normalized_key in _FILTER_PREFIXES and normalized_value:
            raw_filters[normalized_key] = normalized_value
            continue
        text_terms.append(token.lower())

    priority_value: int | None = None
    raw_priority = raw_filters.get("priority") or raw_filters.get("p")
    if raw_priority is not None:
        try:
            priority_value = int(raw_priority)
        except ValueError:
            priority_value = None

    return TaskSearchQuery(
        text_terms=tuple(text_terms),
        status=raw_filters.get("status"),
        role=raw_filters.get("role"),
        agent=raw_filters.get("agent"),
        priority=priority_value,
        raw_filters=raw_filters,
    )


def matches_task_search(task: SupportsTaskSearch, parsed: TaskSearchQuery) -> bool:
    """Return ``True`` when *task* matches the parsed task search query."""

    if parsed.status is not None and task.status.lower() != parsed.status:
        return False
    if parsed.role is not None and task.role.lower() != parsed.role:
        return False
    if parsed.agent is not None and parsed.agent not in task.assigned_agent.lower():
        return False
    if parsed.priority is not None and task.priority != parsed.priority:
        return False

    haystack = " ".join(
        [
            task.task_id.lower(),
            task.title.lower(),
            task.role.lower(),
            task.status.lower(),
            task.model.lower(),
            task.assigned_agent.lower(),
            task.blocked_reason.lower(),
        ]
    )
    return all(term in haystack for term in parsed.text_terms)


class TaskSearchInput(Input):
    """Search input widget for filtering tasks.

    Press '/' to focus, Escape to clear.
    Filters task table in real-time as user types.
    """

    DEFAULT_CSS = """
    TaskSearchInput {
        dock: top;
        width: 100%;
        margin: 0 1 1 1;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(
            *args,
            placeholder="Search tasks or use status:, role:, priority:, agent:",
            **kwargs,
        )

    def on_key(self, event: Key) -> None:
        """Handle key events."""
        if event.key == "escape":
            self.value = ""
            self.blur()
            event.stop()
