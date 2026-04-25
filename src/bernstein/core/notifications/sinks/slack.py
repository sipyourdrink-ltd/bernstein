"""Slack notification sink (incoming webhook flavour).

Targets the simplest Slack surface — an Incoming Webhook URL. Bot
tokens and Block Kit are deliberately out of scope; users who want
either can publish their own pluggy-registered driver. Retrying is
delegated to the shared :mod:`_http` helper which classifies HTTP
errors into transient vs. permanent for the dispatcher.
"""

from __future__ import annotations

import os
from typing import Any

from bernstein.core.notifications.protocol import (
    NotificationEvent,
    NotificationPermanentError,
)
from bernstein.core.notifications.sinks._http import post_json

__all__ = ["SlackSink"]


class SlackSink:
    """Notify a Slack channel via an Incoming Webhook URL.

    Required config keys::

        id: <unique sink id>
        kind: slack
        webhook_url: https://hooks.slack.com/services/...

    Optional::

        username: <override-bot-name>
        icon_emoji: ":robot_face:"
        channel: "#ops"
    """

    kind: str = "slack"

    def __init__(self, config: dict[str, Any]) -> None:
        self.sink_id = str(config["id"])
        webhook_url = _resolve(config.get("webhook_url"))
        if not webhook_url:
            raise NotificationPermanentError(
                f"slack sink {self.sink_id!r} requires 'webhook_url'",
            )
        self._webhook_url = webhook_url
        self._username = _resolve(config.get("username"))
        self._icon_emoji = _resolve(config.get("icon_emoji"))
        self._channel = _resolve(config.get("channel"))
        self._timeout = float(config.get("timeout_s", 10.0))

    async def deliver(self, event: NotificationEvent) -> None:
        """Post ``event`` to the configured Slack webhook."""
        text = f"*{event.title}*"
        if event.body:
            text = f"{text}\n{event.body}"
        payload: dict[str, Any] = {"text": text}
        if self._username:
            payload["username"] = self._username
        if self._icon_emoji:
            payload["icon_emoji"] = self._icon_emoji
        if self._channel:
            payload["channel"] = self._channel
        await post_json(self._webhook_url, payload, timeout=self._timeout)

    async def close(self) -> None:
        """No-op: HTTP client is per-request."""


def _resolve(value: Any) -> str | None:
    """Resolve ``${ENV_VAR}`` substitutions on string config values."""
    if not isinstance(value, str):
        return None
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1])
    return value
