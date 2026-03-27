"""Webhook notification system for Bernstein run events.

Supports Slack Block Kit, Discord embeds, Telegram bot messages, and
generic JSON webhooks. Each ``NotificationTarget`` subscribes to a list
of event names; ``NotificationManager`` dispatches only to interested
targets. All errors are swallowed so that notification failures never
crash a run.

Events
------
``run.started``
    Agents are about to be spawned.
``task.completed``
    An individual task finished successfully.
``task.failed``
    An individual task failed or was rejected by the janitor.
``run.completed``
    All tasks are done; includes cost and duration summary.
``budget.warning``
    Cumulative spend is approaching the configured budget cap.
``approval.needed``
    A task is blocked waiting for human review.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

NotificationEvent = Literal[
    "run.started",
    "task.completed",
    "task.failed",
    "run.completed",
    "budget.warning",
    "approval.needed",
]

# Discord / Slack color codes per event
_RED = 0xFF0000
_GREEN = 0x00FF00
_BLUE = 0x0088FF
_ORANGE = 0xFFAA00

_EVENT_COLOR: dict[str, int] = {
    "run.started": _BLUE,
    "task.completed": _GREEN,
    "task.failed": _RED,
    "run.completed": _BLUE,
    "budget.warning": _ORANGE,
    "approval.needed": _ORANGE,
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotificationTarget:
    """A single notification destination.

    Attributes:
        type: Formatter to use (``slack``, ``discord``, ``telegram``, ``webhook``).
        url: Webhook URL or Telegram API base URL.
        events: List of event names this target cares about.
        token: Telegram bot token (required when ``type == "telegram"``).
        chat_id: Telegram chat/channel ID (required when ``type == "telegram"``).
    """

    type: Literal["slack", "discord", "telegram", "webhook"]
    url: str
    events: list[str] = field(default_factory=list[str])
    token: str | None = None
    chat_id: str | None = None


@dataclass(frozen=True)
class NotificationPayload:
    """Structured data for a notification event.

    Attributes:
        event: The event name (e.g. ``"run.completed"``).
        title: Short human-readable title.
        body: Longer description / summary text.
        metadata: Arbitrary key-value pairs (cost, task counts, etc.).
    """

    event: str
    title: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def format_slack(payload: NotificationPayload) -> dict[str, Any]:
    """Format a payload as a Slack Block Kit message.

    Args:
        payload: The notification payload.

    Returns:
        Dict suitable for ``POST``-ing to a Slack incoming webhook.
    """
    color = _EVENT_COLOR.get(payload.event, _BLUE)
    color_hex = f"#{color:06X}"

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": payload.title},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Event:* `{payload.event}`\n{payload.body}",
            },
        },
    ]

    if payload.metadata:
        meta_lines = [f"*{k}:* {v}" for k, v in payload.metadata.items()]
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(meta_lines)},
            }
        )

    return {
        "blocks": blocks,
        "attachments": [{"color": color_hex, "blocks": blocks}],
    }


def format_discord(payload: NotificationPayload) -> dict[str, Any]:
    """Format a payload as a Discord webhook embed.

    Args:
        payload: The notification payload.

    Returns:
        Dict suitable for ``POST``-ing to a Discord incoming webhook.
    """
    color = _EVENT_COLOR.get(payload.event, _BLUE)

    fields: list[dict[str, Any]] = [
        {"name": "Event", "value": f"`{payload.event}`", "inline": True},
    ]
    for k, v in payload.metadata.items():
        fields.append({"name": str(k), "value": str(v), "inline": True})

    embed: dict[str, Any] = {
        "title": payload.title,
        "description": payload.body,
        "color": color,
        "fields": fields,
    }

    return {"embeds": [embed]}


def format_telegram(payload: NotificationPayload) -> str:
    """Format a payload as a Telegram Markdown message.

    Args:
        payload: The notification payload.

    Returns:
        Markdown-formatted string for the Telegram Bot API.
    """
    lines: list[str] = [
        f"*{payload.title}*",
        f"Event: `{payload.event}`",
        "",
    ]
    if payload.body:
        lines.append(payload.body)
    for k, v in payload.metadata.items():
        lines.append(f"*{k}:* {v}")
    return "\n".join(lines)


def format_webhook(payload: NotificationPayload) -> dict[str, Any]:
    """Format a payload as a generic JSON webhook body.

    Args:
        payload: The notification payload.

    Returns:
        JSON-serialisable dict with ``event``, ``title``, ``body``, and
        ``metadata`` keys.
    """
    return {
        "event": payload.event,
        "title": payload.title,
        "body": payload.body,
        "metadata": payload.metadata,
    }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class NotificationManager:
    """Dispatch notifications to configured targets.

    Errors are always swallowed — notification failures must never crash
    the orchestrator run.

    Args:
        targets: List of notification destinations to dispatch to.
    """

    def __init__(self, targets: list[NotificationTarget]) -> None:
        self._targets = targets

    def notify(self, event: str, payload: NotificationPayload) -> None:
        """Send notifications to all targets subscribed to ``event``.

        Args:
            event: The event name (e.g. ``"run.completed"``).
            payload: Structured notification data.
        """
        for target in self._targets:
            if event in target.events:
                self._send(target, payload)

    def _send(self, target: NotificationTarget, payload: NotificationPayload) -> None:
        """Dispatch to the right formatter and POST to the endpoint.

        Args:
            target: Destination configuration.
            payload: Notification data.
        """
        try:
            if target.type == "slack":
                body = format_slack(payload)
                httpx.post(target.url, json=body, timeout=10.0)
                logger.info("Slack notification sent: event=%s", payload.event)

            elif target.type == "discord":
                body = format_discord(payload)
                httpx.post(target.url, json=body, timeout=10.0)
                logger.info("Discord notification sent: event=%s", payload.event)

            elif target.type == "telegram":
                text = format_telegram(payload)
                api_url = f"{target.url.rstrip('/')}/bot{target.token}/sendMessage"
                httpx.post(
                    api_url,
                    json={
                        "chat_id": target.chat_id,
                        "text": text,
                        "parse_mode": "Markdown",
                    },
                    timeout=10.0,
                )
                logger.info("Telegram notification sent: event=%s", payload.event)

            else:  # generic webhook
                body = format_webhook(payload)
                httpx.post(target.url, json=body, timeout=10.0)
                logger.info("Webhook notification sent: event=%s url=%s", payload.event, target.url)

        except Exception:
            logger.exception(
                "Notification failed (swallowed): type=%s event=%s url=%s",
                target.type,
                payload.event,
                target.url,
            )
