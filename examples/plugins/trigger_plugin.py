"""Example plugin: custom trigger source (Jira webhook).

Demonstrates how to add a new event source that feeds tasks into Bernstein.
Trigger sources normalize raw webhook payloads into TriggerEvent objects,
which the orchestrator turns into tasks.

Usage — wire up in your WSGI/ASGI app:

    from examples.plugins.trigger_plugin import JiraTriggerSource

    jira = JiraTriggerSource(project_key="MYPROJ")
    events = jira.parse(request.json)

Or use it as a Bernstein lifecycle plugin to inject tasks on issue transitions:

    plugins:
      - examples.plugins.trigger_plugin:JiraTriggerSource

Set credentials via environment variables:
    JIRA_URL=https://mycompany.atlassian.net
    JIRA_TOKEN=<personal-access-token>
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from bernstein.plugins import hookimpl

log = logging.getLogger(__name__)


@dataclass
class JiraEvent:
    """Normalized Jira issue event."""

    issue_key: str
    summary: str
    status: str
    priority: str
    assignee: str | None
    url: str
    raw_payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=lambda: datetime.now(UTC).timestamp())


class JiraTriggerSource:
    """Converts Jira webhook payloads into Bernstein tasks.

    Register in bernstein.yaml under ``plugins`` to have Jira issue transitions
    automatically create Bernstein tasks:

        plugins:
          - examples.plugins.trigger_plugin:JiraTriggerSource

    Supported events: ``jira:issue_created``, ``jira:issue_updated``
    Tasks are only created when an issue moves to the configured trigger status
    (default: "In Progress").
    """

    def __init__(
        self,
        project_key: str = "",
        trigger_status: str = "In Progress",
        default_role: str = "backend",
    ) -> None:
        self._project_key = project_key or os.getenv("JIRA_PROJECT_KEY", "")
        self._trigger_status = trigger_status
        self._default_role = default_role
        self._jira_url = os.getenv("JIRA_URL", "")

    # ------------------------------------------------------------------
    # Lifecycle plugin hook: fires when orchestrator is fully started
    # ------------------------------------------------------------------

    @hookimpl
    def on_task_created(self, task_id: str, role: str, title: str) -> None:
        """Log when Bernstein creates a task (informational hook example)."""
        log.debug("JiraTriggerSource: task created task_id=%s title=%r", task_id, title)

    # ------------------------------------------------------------------
    # Webhook parsing (call from your web framework's route handler)
    # ------------------------------------------------------------------

    def parse(self, payload: dict[str, Any]) -> JiraEvent | None:
        """Parse a raw Jira webhook payload into a JiraEvent.

        Returns None for events that should not trigger a task.

        Args:
            payload: Raw JSON body from the Jira webhook.

        Returns:
            JiraEvent if the event should create a task, else None.
        """
        event_type = payload.get("webhookEvent", "")
        if event_type not in ("jira:issue_created", "jira:issue_updated"):
            return None

        issue = payload.get("issue", {})
        fields = issue.get("fields", {})
        status = (fields.get("status") or {}).get("name", "")

        if status != self._trigger_status:
            return None

        issue_key = issue.get("key", "")
        if self._project_key and not issue_key.startswith(f"{self._project_key}-"):
            return None

        assignee_obj = fields.get("assignee") or {}
        return JiraEvent(
            issue_key=issue_key,
            summary=fields.get("summary", ""),
            status=status,
            priority=(fields.get("priority") or {}).get("name", "medium"),
            assignee=assignee_obj.get("displayName"),
            url=f"{self._jira_url}/browse/{issue_key}" if self._jira_url else "",
            raw_payload=payload,
        )

    def to_task_params(self, event: JiraEvent) -> dict[str, Any]:
        """Convert a JiraEvent into keyword arguments for the task server POST /tasks.

        Args:
            event: Parsed Jira event.

        Returns:
            Dict suitable for posting to ``http://127.0.0.1:8052/tasks``.
        """
        return {
            "title": f"[{event.issue_key}] {event.summary}",
            "description": (
                f"Jira issue {event.issue_key} moved to '{event.status}'.\nPriority: {event.priority}\nURL: {event.url}"
            ),
            "role": self._default_role,
            "priority": _jira_priority_to_int(event.priority),
        }


def _jira_priority_to_int(priority: str) -> int:
    mapping = {"Highest": 1, "High": 2, "Medium": 3, "Low": 4, "Lowest": 5}
    return mapping.get(priority, 3)
