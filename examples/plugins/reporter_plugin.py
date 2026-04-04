"""Example plugin: custom reporter.

Demonstrates how to emit run reports in a custom format by hooking into
Bernstein's lifecycle events. This example posts a Slack summary when
the orchestrator finishes a batch of tasks.

Usage — add to bernstein.yaml:

    plugins:
      - examples.plugins.reporter_plugin:SlackReporter

Set credentials via environment variable:
    SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime

from bernstein.plugins import hookimpl

log = logging.getLogger(__name__)


@dataclass
class _RunSummary:
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=lambda: datetime.now(UTC).timestamp())


class SlackReporter:
    """Posts a Slack summary when tasks complete or fail.

    Register in bernstein.yaml under ``plugins`` to get Slack notifications:

        plugins:
          - examples.plugins.reporter_plugin:SlackReporter

    The webhook URL is read from ``SLACK_WEBHOOK_URL``. If unset, the reporter
    is a no-op and logs at DEBUG level instead.
    """

    def __init__(self, webhook_url: str | None = None) -> None:
        self._webhook_url = webhook_url or os.getenv("SLACK_WEBHOOK_URL", "")
        self._summary = _RunSummary()

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    @hookimpl
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        """Record a completed task."""
        self._summary.completed.append(f"{task_id} ({role})")

    @hookimpl
    def on_task_failed(self, task_id: str, role: str, error: str) -> None:
        """Record a failed task and post an immediate alert."""
        self._summary.failed.append(f"{task_id} ({role}): {error[:100]}")
        self._post(
            f":x: Task *{task_id}* failed ({role})\n```{error[:300]}```",
            urgent=True,
        )

    @hookimpl
    def on_stop(self, session_id: str, reason: str, signal: str) -> None:
        """Post a run summary when the orchestrator stops."""
        elapsed = datetime.now(UTC).timestamp() - self._summary.started_at
        lines = [
            f":white_check_mark: *Bernstein run finished* (session `{session_id}`) — {elapsed:.0f}s",
            f"Completed: {len(self._summary.completed)}  Failed: {len(self._summary.failed)}",
        ]
        if self._summary.failed:
            lines.append("Failed tasks:\n" + "\n".join(f"  • {t}" for t in self._summary.failed))
        self._post("\n".join(lines))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _post(self, text: str, *, urgent: bool = False) -> None:
        """Send a Slack message via incoming webhook."""
        if not self._webhook_url:
            log.debug("SlackReporter: no webhook configured, skipping: %s", text[:100])
            return
        payload = json.dumps({"text": text}).encode()
        try:
            req = urllib.request.Request(
                self._webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status != 200:
                    log.warning("SlackReporter: unexpected status %s", resp.status)
        except OSError as exc:
            log.warning("SlackReporter: failed to post message: %s", exc)
