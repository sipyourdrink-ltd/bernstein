"""Notification channels for Bernstein events.

Supports Slack webhooks, email (SMTP), and desktop notifications.
"""

from __future__ import annotations

import logging
import smtplib
import subprocess
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, Any, Literal

import httpx

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


NotificationChannel = Literal["slack", "email", "desktop"]
NotificationEvent = Literal["task_complete", "task_failed", "approval_needed", "cost_alert"]


@dataclass
class NotificationConfig:
    """Configuration for notification channels."""

    slack_webhook: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_to: list[str] = field(default_factory=list)
    desktop_enabled: bool = False
    quiet_start: str = "22:00"
    quiet_end: str = "08:00"
    events: list[NotificationEvent] = field(default_factory=list)


@dataclass
class Notification:
    """A notification to be sent."""

    event: NotificationEvent
    title: str
    message: str
    task_id: str | None = None
    cost_usd: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class NotificationSender:
    """Send notifications through configured channels."""

    def __init__(self, config: NotificationConfig, workdir: Path | None = None) -> None:
        self._config = config
        self._workdir = workdir

    def send(self, notification: Notification, channels: list[NotificationChannel] | None = None) -> None:
        """Send notification through specified channels.

        Args:
            notification: Notification to send.
            channels: List of channels to use. If None, uses all configured channels.
        """
        if channels is None:
            channels = []
            if self._config.slack_webhook:
                channels.append("slack")
            if self._config.smtp_host and self._config.smtp_to:
                channels.append("email")
            if self._config.desktop_enabled:
                channels.append("desktop")

        for channel in channels:
            try:
                if channel == "slack":
                    self._send_slack(notification)
                elif channel == "email":
                    self._send_email(notification)
                elif channel == "desktop":
                    self._send_desktop(notification)
            except Exception as exc:
                logger.warning("Failed to send %s notification: %s", channel, exc)

    def _send_slack(self, notification: Notification) -> None:
        """Send notification to Slack webhook."""
        if not self._config.slack_webhook:
            return

        color = {
            "task_complete": "good",
            "task_failed": "danger",
            "approval_needed": "warning",
            "cost_alert": "warning",
        }.get(notification.event, "#439FE0")

        payload = {
            "attachments": [
                {
                    "color": color,
                    "title": notification.title,
                    "text": notification.message,
                    "fields": [
                        {"title": "Event", "value": notification.event, "short": True},
                    ],
                }
            ]
        }

        if notification.task_id:
            payload["attachments"][0]["fields"].append(
                {"title": "Task ID", "value": notification.task_id, "short": True}
            )

        if notification.cost_usd is not None:
            payload["attachments"][0]["fields"].append(
                {"title": "Cost", "value": f"${notification.cost_usd:.2f}", "short": True}
            )

        resp = httpx.post(self._config.slack_webhook, json=payload, timeout=10.0)
        resp.raise_for_status()

    def _send_email(self, notification: Notification) -> None:
        """Send notification via email."""
        if not self._config.smtp_host or not self._config.smtp_to:
            return

        subject = f"[Bernstein] {notification.title}"
        body = f"""
Event: {notification.event}
Task: {notification.task_id or 'N/A'}

{notification.message}

Cost: {f'${notification.cost_usd:.2f}' if notification.cost_usd is not None else 'N/A'}
"""

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = self._config.smtp_from or self._config.smtp_user
        msg["To"] = ", ".join(self._config.smtp_to)

        with smtplib.SMTP(self._config.smtp_host, self._config.smtp_port) as server:
            server.starttls()
            if self._config.smtp_user and self._config.smtp_password:
                server.login(self._config.smtp_user, self._config.smtp_password)
            server.send_message(msg)

    def _send_desktop(self, notification: Notification) -> None:
        """Send desktop notification."""
        try:
            # Try macOS terminal-notifier first
            subprocess.run(
                [
                    "terminal-notifier",
                    "-title", "Bernstein",
                    "-subtitle", notification.event,
                    "-message", notification.message,
                ],
                timeout=5.0,
                check=False,
                capture_output=True,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # Try Linux notify-send
            try:
                subprocess.run(
                    ["notify-send", "Bernstein", f"{notification.event}: {notification.message}"],
                    timeout=5.0,
                    check=False,
                    capture_output=True,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                logger.debug("Desktop notifications not available")

    def is_quiet_hours(self) -> bool:
        """Check if current time is within quiet hours."""
        from datetime import datetime

        now = datetime.now()
        quiet_start = datetime.strptime(self._config.quiet_start, "%H:%M").time()
        quiet_end = datetime.strptime(self._config.quiet_end, "%H:%M").time()

        if quiet_start > quiet_end:
            # Quiet hours span midnight (e.g., 22:00 to 08:00)
            return now.time() >= quiet_start or now.time() <= quiet_end
        else:
            return quiet_start <= now.time() <= quiet_end

    def should_notify(self, event: NotificationEvent) -> bool:
        """Check if notification should be sent for this event."""
        # Check quiet hours
        if self.is_quiet_hours():
            return False

        # Check event filter
        return not (self._config.events and event not in self._config.events)
