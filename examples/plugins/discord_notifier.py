"""Example plugin: Discord notifier.

Posts task failure, completion, and evolution-proposal alerts to a Discord
channel via a Webhook URL.  Uses Discord's embed format for color-coded,
structured messages.

Usage — add to bernstein.yaml:

    plugins:
      - examples.plugins.discord_notifier:DiscordNotifier

Set the webhook URL via environment variable:

    export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/CHANNEL_ID/TOKEN

Or pass it explicitly at construction time (useful in scripts):

    from examples.plugins.discord_notifier import DiscordNotifier
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/...")
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from typing import Any

from bernstein.plugins import hookimpl

log = logging.getLogger(__name__)


class DiscordNotifier:
    """Posts task failure and completion alerts to Discord.

    Hook calls are fire-and-forget: the HTTP request is dispatched on a
    daemon thread so it never blocks the orchestrator loop.
    """

    def __init__(self, webhook_url: str | None = None) -> None:
        self._webhook_url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL", "")
        if not self._webhook_url:
            log.warning(
                "DiscordNotifier: no webhook URL configured — "
                "set DISCORD_WEBHOOK_URL or pass webhook_url= at construction time"
            )

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    @hookimpl
    def on_task_failed(self, task_id: str, role: str, error: str) -> None:
        """Alert on task failure with a red embed."""
        self._post(
            embeds=[
                {
                    "title": f"\u274c Task Failed: {task_id}",
                    "description": f"**Role:** `{role}`\n```{error[:800]}```",
                    "color": 0xED4245,  # Discord red
                }
            ]
        )

    @hookimpl
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        """Notify on task completion with a green embed."""
        self._post(
            embeds=[
                {
                    "title": f"\u2705 Task Completed: {task_id}",
                    "description": f"**Role:** `{role}`\n{result_summary[:400]}",
                    "color": 0x57F287,  # Discord green
                }
            ]
        )

    @hookimpl
    def on_evolve_proposal(self, proposal_id: str, title: str, verdict: str) -> None:
        """Notify when an evolution proposal is accepted or rejected."""
        color = 0x57F287 if verdict == "accepted" else 0xED4245
        self._post(
            embeds=[
                {
                    "title": f"Evolution Proposal {verdict.title()}: {title}",
                    "description": f"Proposal ID: `{proposal_id}`",
                    "color": color,
                }
            ]
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _post(self, embeds: list[dict[str, Any]]) -> None:
        """Dispatch a Discord webhook call on a background daemon thread."""
        if not self._webhook_url:
            return
        url = self._webhook_url
        payload: dict[str, Any] = {"embeds": embeds}

        def _send() -> None:
            try:
                data = json.dumps(payload).encode()
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5):
                    pass
            except Exception as exc:
                log.warning("DiscordNotifier: failed to post webhook: %s", exc)

        t = threading.Thread(target=_send, daemon=True)
        t.start()
