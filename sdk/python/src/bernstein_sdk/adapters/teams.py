"""Microsoft Teams adapter — send Bernstein task events via Incoming Webhooks.

Teams supports Incoming Webhooks with an Adaptive Cards payload.

Setup::

    export TEAMS_WEBHOOK_URL=https://your-org.webhook.office.com/webhookb2/...

Usage::

    from bernstein_sdk.adapters.teams import TeamsAdapter

    adapter = TeamsAdapter.from_env()
    adapter.notify_task_completed(
        task_id="abc123",
        title="Add rate limiting",
        role="backend",
        result_summary="Implemented token bucket in middleware.py",
    )
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from typing import Any

log = logging.getLogger(__name__)


class TeamsAdapter:
    """Send Bernstein task events to Microsoft Teams via Incoming Webhooks.

    All HTTP calls are dispatched on daemon threads (fire-and-forget) so they
    never block the orchestrator.

    Args:
        webhook_url: Teams Incoming Webhook URL.
    """

    def __init__(self, webhook_url: str = "") -> None:
        self._webhook_url = webhook_url
        if not webhook_url:
            log.warning(
                "TeamsAdapter: no webhook URL configured — "
                "set TEAMS_WEBHOOK_URL or pass webhook_url= at construction time"
            )

    @classmethod
    def from_env(cls) -> TeamsAdapter:
        """Construct from the ``TEAMS_WEBHOOK_URL`` environment variable."""
        return cls(webhook_url=os.getenv("TEAMS_WEBHOOK_URL", ""))

    # ------------------------------------------------------------------
    # Notification helpers
    # ------------------------------------------------------------------

    def notify_task_completed(
        self,
        task_id: str,
        title: str,
        role: str,
        result_summary: str = "",
    ) -> None:
        """Post a task-completed Adaptive Card."""
        card = _completed_card(
            task_id=task_id, title=title, role=role, summary=result_summary
        )
        self._post_async(card)

    def notify_task_failed(
        self,
        task_id: str,
        title: str,
        role: str,
        error: str = "",
    ) -> None:
        """Post a task-failed Adaptive Card."""
        card = _failed_card(task_id=task_id, title=title, role=role, error=error)
        self._post_async(card)

    def notify_task_created(
        self,
        task_id: str,
        title: str,
        role: str,
        priority: int = 2,
    ) -> None:
        """Post a task-created Adaptive Card."""
        card = _created_card(task_id=task_id, title=title, role=role, priority=priority)
        self._post_async(card)

    def post_message(self, text: str) -> None:
        """Post a plain-text message card."""
        card = _text_card(text)
        self._post_async(card)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _post_async(self, payload: dict[str, Any]) -> None:
        if not self._webhook_url:
            return
        url = self._webhook_url

        def _send() -> None:
            try:
                data = json.dumps(payload).encode()
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=8):
                    pass  # Response body not needed
            except Exception as exc:
                log.warning("TeamsAdapter: failed to post webhook: %s", exc)

        t = threading.Thread(target=_send, daemon=True)
        t.start()


# ---------------------------------------------------------------------------
# Adaptive Card / MessageCard builders
#
# Teams supports both the legacy "MessageCard" format and newer Adaptive Cards.
# We use Adaptive Cards (schema 1.4) for rich formatting.
# ---------------------------------------------------------------------------


def _completed_card(
    task_id: str, title: str, role: str, summary: str
) -> dict[str, Any]:
    facts = [
        {"title": "Task ID", "value": task_id},
        {"title": "Role", "value": role},
    ]
    if summary:
        facts.append({"title": "Result", "value": summary[:400]})
    return _adaptive_card(
        title="✅ Task Completed",
        subtitle=title,
        color="Good",
        facts=facts,
    )


def _failed_card(task_id: str, title: str, role: str, error: str) -> dict[str, Any]:
    facts = [
        {"title": "Task ID", "value": task_id},
        {"title": "Role", "value": role},
    ]
    if error:
        facts.append({"title": "Error", "value": error[:400]})
    return _adaptive_card(
        title="❌ Task Failed",
        subtitle=title,
        color="Attention",
        facts=facts,
    )


def _created_card(task_id: str, title: str, role: str, priority: int) -> dict[str, Any]:
    priority_label = {1: "Critical", 2: "Normal", 3: "Low"}.get(priority, "Normal")
    return _adaptive_card(
        title="🆕 New Task",
        subtitle=title,
        color="Accent",
        facts=[
            {"title": "Task ID", "value": task_id},
            {"title": "Role", "value": role},
            {"title": "Priority", "value": priority_label},
        ],
    )


def _text_card(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [{"type": "TextBlock", "text": text, "wrap": True}],
                },
            }
        ],
    }


def _adaptive_card(
    title: str,
    subtitle: str,
    color: str,
    facts: list[dict[str, str]],
) -> dict[str, Any]:
    """Build an Adaptive Card message payload for Teams Incoming Webhooks."""
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "size": "Large",
                            "weight": "Bolder",
                            "text": title,
                            "color": color,
                        },
                        {
                            "type": "TextBlock",
                            "text": subtitle,
                            "wrap": True,
                        },
                        {
                            "type": "FactSet",
                            "facts": facts,
                        },
                    ],
                },
            }
        ],
    }
