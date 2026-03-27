"""Example plugin: Slack notifier.

Posts task failure and completion alerts to a Slack channel via an
Incoming Webhook.  Only implements the hooks it cares about — the other
hooks are simply not defined, which is fine.

Usage — add to bernstein.yaml:

    plugins:
      - examples.plugins.slack_notifier:SlackNotifier

Set the webhook URL via an environment variable:

    export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../xxx

Or pass it explicitly when instantiating in a script:

    from examples.plugins.slack_notifier import SlackNotifier
    notifier = SlackNotifier(webhook_url="https://hooks.slack.com/...")
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from bernstein.plugins import hookimpl

log = logging.getLogger(__name__)


class SlackNotifier:
    """Posts task failure and key-event alerts to Slack.

    Hook calls are fire-and-forget: the HTTP request is dispatched on a
    daemon thread so it never blocks the orchestrator loop.
    """

    def __init__(self, webhook_url: str | None = None) -> None:
        self._webhook_url = webhook_url or os.getenv("SLACK_WEBHOOK_URL", "")
        if not self._webhook_url:
            log.warning(
                "SlackNotifier: no webhook URL configured — "
                "set SLACK_WEBHOOK_URL or pass webhook_url= at construction time"
            )

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    @hookimpl
    def on_task_failed(self, task_id: str, role: str, error: str) -> None:
        """Alert on task failure — highest-signal event for on-call."""
        self._post(
            {
                "text": f":red_circle: *Task failed* `{task_id}` (role: `{role}`)\n```{error[:500]}```",
            }
        )

    @hookimpl
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        """Optional: notify on completion (disable if too noisy)."""
        self._post(
            {
                "text": f":white_check_mark: Task `{task_id}` completed by `{role}`: {result_summary[:200]}",
            }
        )

    @hookimpl
    def on_evolve_proposal(self, proposal_id: str, title: str, verdict: str) -> None:
        """Notify when an evolution proposal is accepted or rejected."""
        emoji = ":tada:" if verdict == "accepted" else ":no_entry_sign:"
        self._post(
            {
                "text": f"{emoji} Evolution proposal `{proposal_id}` *{verdict}*: {title}",
            }
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _post(self, payload: dict[str, Any]) -> None:
        """Dispatch a Slack webhook call on a background daemon thread."""
        if not self._webhook_url:
            return
        url = self._webhook_url

        def _send() -> None:
            try:
                import json
                import urllib.request

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
                log.warning("SlackNotifier: failed to post webhook: %s", exc)

        t = threading.Thread(target=_send, daemon=True)
        t.start()
