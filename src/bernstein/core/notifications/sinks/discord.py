"""Discord notification sink (incoming webhook flavour)."""

from __future__ import annotations

import os
from typing import Any

from bernstein.core.notifications.protocol import (
    NotificationEvent,
    NotificationPermanentError,
)
from bernstein.core.notifications.sinks._http import post_json

__all__ = ["DiscordSink"]

_SEVERITY_COLORS: dict[str, int] = {
    "info": 0x3498DB,
    "warning": 0xF1C40F,
    "error": 0xE74C3C,
}


class DiscordSink:
    """Notify a Discord channel via an Incoming Webhook URL.

    Required config keys::

        id: <unique sink id>
        kind: discord
        webhook_url: https://discord.com/api/webhooks/...

    Optional::

        username: <override-bot-name>
        avatar_url: https://...
    """

    kind: str = "discord"

    def __init__(self, config: dict[str, Any]) -> None:
        self.sink_id = str(config["id"])
        webhook_url = _resolve(config.get("webhook_url"))
        if not webhook_url:
            raise NotificationPermanentError(
                f"discord sink {self.sink_id!r} requires 'webhook_url'",
            )
        self._webhook_url = webhook_url
        self._username = _resolve(config.get("username"))
        self._avatar_url = _resolve(config.get("avatar_url"))
        self._timeout = float(config.get("timeout_s", 10.0))

    async def deliver(self, event: NotificationEvent) -> None:
        """Post ``event`` to the configured Discord webhook as an embed."""
        embed: dict[str, Any] = {
            "title": event.title[:256],
            "description": event.body[:4000] if event.body else "",
            "color": _SEVERITY_COLORS.get(event.severity, _SEVERITY_COLORS["info"]),
        }
        payload: dict[str, Any] = {"embeds": [embed]}
        if self._username:
            payload["username"] = self._username
        if self._avatar_url:
            payload["avatar_url"] = self._avatar_url
        await post_json(self._webhook_url, payload, timeout=self._timeout)

    async def close(self) -> None:
        """No-op."""


def _resolve(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1])
    return value
