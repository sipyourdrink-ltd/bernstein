"""Slack adapter — send Bernstein task notifications to Slack channels.

Sends Block Kit messages to Slack via Incoming Webhooks or the Web API.

Setup
-----
Option A — Incoming Webhook (simplest)::

    export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...

Option B — Bot token (supports specifying channels dynamically)::

    export SLACK_BOT_TOKEN=xoxb-...

Usage::

    from bernstein_sdk.adapters.slack import SlackAdapter, TaskEvent

    adapter = SlackAdapter.from_env()
    adapter.notify_task_completed(
        task_id="abc123",
        title="Fix login regression",
        role="backend",
        result_summary="Patched null-check in auth.py",
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

_SLACK_API_BASE = "https://slack.com/api"


class SlackAdapter:
    """Send Bernstein task events to Slack.

    Supports both Incoming Webhooks (simpler) and the Slack Web API (more
    control).  All HTTP calls are dispatched on daemon threads so they never
    block the caller.

    Args:
        webhook_url: Slack Incoming Webhook URL.  If set, notifications are
            sent here regardless of *bot_token* / *channel*.
        bot_token: Slack bot OAuth token (``xoxb-...``).  Required if
            *webhook_url* is not set.
        channel: Default Slack channel ID or name (e.g. ``"#dev-notifications"``).
        mention_on_failure: Slack user/group ID to ``@mention`` when a task
            fails (e.g. ``"<!here>"`` or ``"<@U12345>"``).
    """

    def __init__(
        self,
        webhook_url: str = "",
        bot_token: str = "",
        channel: str = "",
        mention_on_failure: str = "",
    ) -> None:
        self._webhook_url = webhook_url
        self._bot_token = bot_token
        self._channel = channel
        self._mention_on_failure = mention_on_failure

    @classmethod
    def from_env(cls) -> SlackAdapter:
        """Construct from environment variables.

        Reads ``SLACK_WEBHOOK_URL`` and/or ``SLACK_BOT_TOKEN`` /
        ``SLACK_CHANNEL``.
        """
        return cls(
            webhook_url=os.getenv("SLACK_WEBHOOK_URL", ""),
            bot_token=os.getenv("SLACK_BOT_TOKEN", ""),
            channel=os.getenv("SLACK_CHANNEL", ""),
            mention_on_failure=os.getenv("SLACK_MENTION_ON_FAILURE", ""),
        )

    # ------------------------------------------------------------------
    # Public notification helpers
    # ------------------------------------------------------------------

    def notify_task_completed(
        self,
        task_id: str,
        title: str,
        role: str,
        result_summary: str = "",
        channel: str = "",
    ) -> None:
        """Post a task-completed notification."""
        blocks = _task_completed_blocks(
            task_id=task_id, title=title, role=role, summary=result_summary
        )
        self._post_async(blocks=blocks, channel=channel or self._channel)

    def notify_task_failed(
        self,
        task_id: str,
        title: str,
        role: str,
        error: str = "",
        channel: str = "",
    ) -> None:
        """Post a task-failed notification (optionally @mentioning a user)."""
        mention = self._mention_on_failure
        blocks = _task_failed_blocks(
            task_id=task_id, title=title, role=role, error=error, mention=mention
        )
        self._post_async(blocks=blocks, channel=channel or self._channel)

    def notify_task_created(
        self,
        task_id: str,
        title: str,
        role: str,
        priority: int = 2,
        channel: str = "",
    ) -> None:
        """Post a task-created notification."""
        blocks = _task_created_blocks(
            task_id=task_id, title=title, role=role, priority=priority
        )
        self._post_async(blocks=blocks, channel=channel or self._channel)

    def post_message(
        self, text: str, blocks: list[dict[str, Any]] | None = None, channel: str = ""
    ) -> None:
        """Post an arbitrary text or Block Kit message."""
        self._post_async(
            text=text, blocks=blocks or [], channel=channel or self._channel
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _post_async(
        self,
        blocks: list[dict[str, Any]],
        text: str = "",
        channel: str = "",
    ) -> None:
        """Dispatch a Slack message on a daemon thread (fire-and-forget)."""
        if not self._webhook_url and not self._bot_token:
            log.warning("SlackAdapter: no webhook URL or bot token configured")
            return
        target_channel = channel or self._channel

        def _send() -> None:
            try:
                if self._webhook_url:
                    _post_webhook(
                        self._webhook_url,
                        blocks=blocks,
                        text=text or _blocks_fallback(blocks),
                    )
                elif self._bot_token and target_channel:
                    _post_web_api(
                        token=self._bot_token,
                        channel=target_channel,
                        blocks=blocks,
                        text=text or _blocks_fallback(blocks),
                    )
                else:
                    log.warning(
                        "SlackAdapter: no channel configured for bot token delivery"
                    )
            except Exception as exc:
                log.warning("SlackAdapter: failed to post message: %s", exc)

        t = threading.Thread(target=_send, daemon=True)
        t.start()


# ---------------------------------------------------------------------------
# Block Kit builders
# ---------------------------------------------------------------------------


def _task_completed_blocks(
    task_id: str, title: str, role: str, summary: str
) -> list[dict[str, Any]]:
    text = f":white_check_mark: *Task completed* — `{task_id}`"
    fields = [
        {"type": "mrkdwn", "text": f"*Title:*\n{title}"},
        {"type": "mrkdwn", "text": f"*Role:*\n`{role}`"},
    ]
    if summary:
        fields.append({"type": "mrkdwn", "text": f"*Result:*\n{summary[:300]}"})
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "section", "fields": fields},
    ]


def _task_failed_blocks(
    task_id: str, title: str, role: str, error: str, mention: str
) -> list[dict[str, Any]]:
    header = f":x: *Task failed* — `{task_id}`"
    if mention:
        header = f"{mention} {header}"
    fields = [
        {"type": "mrkdwn", "text": f"*Title:*\n{title}"},
        {"type": "mrkdwn", "text": f"*Role:*\n`{role}`"},
    ]
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "section", "fields": fields},
    ]
    if error:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Error:*\n```{error[:500]}```"},
            }
        )
    return blocks


def _task_created_blocks(
    task_id: str, title: str, role: str, priority: int
) -> list[dict[str, Any]]:
    priority_emoji = {
        1: ":rotating_light:",
        2: ":blue_circle:",
        3: ":white_circle:",
    }.get(priority, ":blue_circle:")
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{priority_emoji} *New task* — `{task_id}`",
            },
            "fields": [
                {"type": "mrkdwn", "text": f"*Title:*\n{title}"},
                {"type": "mrkdwn", "text": f"*Role:*\n`{role}`"},
            ],
        }
    ]


def _blocks_fallback(blocks: list[dict[str, Any]]) -> str:
    """Extract a plain-text fallback from a Block Kit message."""
    for block in blocks:
        text_obj = block.get("text")
        if isinstance(text_obj, dict):
            text = text_obj.get("text", "")
            if text:
                return text
    return "Bernstein notification"


# ---------------------------------------------------------------------------
# HTTP helpers (no extra dependencies)
# ---------------------------------------------------------------------------


def _post_webhook(url: str, blocks: list[dict[str, Any]], text: str) -> None:
    payload = {"blocks": blocks, "text": text}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=5):
        pass  # Response body not needed


def _post_web_api(
    token: str, channel: str, blocks: list[dict[str, Any]], text: str
) -> None:
    payload = {"channel": channel, "blocks": blocks, "text": text}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{_SLACK_API_BASE}/chat.postMessage",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = json.loads(resp.read())
        if not body.get("ok"):
            raise RuntimeError(f"Slack Web API error: {body.get('error')}")
