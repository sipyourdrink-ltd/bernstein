"""Webhook notification system for Bernstein run events.

Supports Slack Block Kit, Discord embeds, Telegram bot messages, PagerDuty
incidents, and generic JSON webhooks. Each ``NotificationTarget`` subscribes
to a list of event names; ``NotificationManager`` dispatches only to
interested targets. All errors are swallowed so that notification failures
never crash a run.

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
``budget.exhausted``
    Cost budget fully exhausted — orchestration must stop.
``approval.needed``
    A task is blocked waiting for human review.
``incident.critical``
    Critical incident detected (high failure rate, agent crash loop, etc.).
"""

from __future__ import annotations

import logging
import smtplib
import subprocess
from contextlib import suppress
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from shutil import which
from sys import platform
from typing import TYPE_CHECKING, Any, Literal

import httpx

if TYPE_CHECKING:
    from bernstein.core.models import SmtpConfig

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
    "budget.exhausted",
    "approval.needed",
    "incident.critical",
]

# PagerDuty severity mapping per event
_PD_SEVERITY: dict[str, str] = {
    "run.started": "info",
    "task.completed": "info",
    "task.failed": "warning",
    "run.completed": "info",
    "budget.warning": "warning",
    "budget.exhausted": "critical",
    "approval.needed": "warning",
    "incident.critical": "critical",
}

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
    "budget.exhausted": _RED,
    "approval.needed": _ORANGE,
    "incident.critical": _RED,
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotificationTarget:
    """A single notification destination.

    Attributes:
        type: Formatter to use (``slack``, ``discord``, ``telegram``, ``webhook``, ``desktop``, ``pagerduty``).
        url: Webhook URL or Telegram API base URL.
        events: List of event names this target cares about.
        token: Telegram bot token (required when ``type == "telegram"``).
        chat_id: Telegram chat/channel ID (required when ``type == "telegram"``).
        routing_key: PagerDuty integration / events API routing key.
    """

    type: Literal["slack", "discord", "telegram", "webhook", "email", "desktop", "pagerduty"]
    url: str
    events: list[str] = field(default_factory=list[str])
    token: str | None = None
    chat_id: str | None = None
    routing_key: str | None = None


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


def format_pagerduty(payload: NotificationPayload, routing_key: str) -> dict[str, Any]:
    """Format a payload as a PagerDuty Events API v2 incident.

    Args:
        payload: The notification payload.
        routing_key: PagerDuty integration key.

    Returns:
        Dict suitable for POST-ing to ``https://events.pagerduty.com/v2/enqueue``.
    """
    return {
        "routing_key": routing_key,
        "event_action": "trigger",
        "dedup_key": f"bernstein:{payload.event}",
        "payload": {
            "summary": f"{payload.title}: {payload.body}",
            "source": "bernstein",
            "severity": _PD_SEVERITY.get(payload.event, "info"),
            "component": "orchestrator",
            "custom_details": payload.metadata,
        },
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
        smtp_config: Optional SMTP configuration for email targets.
    """

    def __init__(
        self,
        targets: list[NotificationTarget],
        smtp_config: SmtpConfig | None = None,
    ) -> None:
        self._targets = targets
        self._smtp_config = smtp_config

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

            elif target.type == "email" and self._smtp_config:
                if not self._smtp_config.to_addresses:
                    return

                msg = MIMEMultipart()
                msg["From"] = self._smtp_config.from_address
                msg["To"] = ", ".join(self._smtp_config.to_addresses)
                msg["Subject"] = payload.title

                body = f"Event: {payload.event}\n\n{payload.body}\n"
                for k, v in payload.metadata.items():
                    body += f"\n{k}: {v}"

                msg.attach(MIMEText(body, "plain"))

                with smtplib.SMTP(self._smtp_config.host, self._smtp_config.port) as server:
                    with suppress(smtplib.SMTPNotSupportedError):
                        server.starttls()
                    if self._smtp_config.username and self._smtp_config.password:
                        server.login(self._smtp_config.username, self._smtp_config.password)
                    server.send_message(msg)
                    logger.info("Email notification sent: event=%s", payload.event)

            elif target.type == "desktop":
                self._send_desktop_notification(payload)

            elif target.type == "pagerduty":
                if not target.routing_key:
                    logger.warning(
                        "PagerDuty notification skipped: no routing_key configured for event=%s",
                        payload.event,
                    )
                    return
                body = format_pagerduty(payload, target.routing_key)
                resp = httpx.post(
                    "https://events.pagerduty.com/v2/enqueue",
                    json=body,
                    timeout=10.0,
                )
                if resp.status_code < 300:
                    logger.info("PagerDuty incident created: event=%s", payload.event)
                else:
                    logger.warning(
                        "PagerDuty returned %d: event=%s body=%s",
                        resp.status_code,
                        payload.event,
                        resp.text[:200],
                    )

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

    def _send_desktop_notification(self, payload: NotificationPayload) -> None:
        """Send a local OS notification when a supported notifier is available."""
        body = payload.body
        if payload.metadata:
            metadata_lines = [f"{key}: {value}" for key, value in payload.metadata.items()]
            body = f"{body}\n" + "\n".join(metadata_lines) if body else "\n".join(metadata_lines)

        if platform == "darwin":
            notifier = which("terminal-notifier")
            if notifier is None:
                logger.debug("Desktop notification skipped: terminal-notifier not installed")
                return
            subprocess.run(
                [notifier, "-title", payload.title, "-message", body or payload.event, "-group", "bernstein"],
                check=False,
                capture_output=True,
                text=True,
            )
            logger.info("Desktop notification sent via terminal-notifier: event=%s", payload.event)
            return

        notifier = which("notify-send")
        if notifier is None:
            logger.debug("Desktop notification skipped: notify-send not installed")
            return
        subprocess.run(
            [notifier, payload.title, body or payload.event],
            check=False,
            capture_output=True,
            text=True,
        )
        logger.info("Desktop notification sent via notify-send: event=%s", payload.event)
