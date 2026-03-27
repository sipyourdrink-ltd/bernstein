"""Example plugin: Jira bidirectional sync.

Keeps Jira issues and Bernstein tasks in sync via the plugin hook system.

Features:
- ``on_task_completed`` → transition the linked Jira issue to "Done"
- ``on_task_failed``    → transition the linked Jira issue to "Done" (failed tag)
                          and add a comment with the error
- ``on_task_created``   → (optional) log for auditing

The Jira issue key is expected in ``task.external_ref`` as ``"jira:PROJ-42"``.
If the ref is absent, the plugin is a no-op for that task.

Prerequisites
-------------
Install the Jira extra::

    pip install bernstein-sdk[jira]

Configure in bernstein.yaml::

    plugins:
      - examples.plugins.jira_plugin:JiraPlugin

Set environment variables::

    export JIRA_BASE_URL=https://your-org.atlassian.net
    export JIRA_EMAIL=you@example.com
    export JIRA_API_TOKEN=<token>

Optional custom state mappings::

    # If your project uses non-standard status names:
    from bernstein_sdk.state_map import BernsteinToJira, TaskStatus
    BernsteinToJira.register(TaskStatus.DONE, "Shipped")
    BernsteinToJira.register(TaskStatus.FAILED, "Failed")
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from bernstein.plugins import hookimpl

log = logging.getLogger(__name__)


class JiraPlugin:
    """Synchronize Bernstein task state changes back to Jira issues.

    All Jira API calls run on daemon threads so they never block the
    orchestrator loop.

    Args:
        default_role: Only sync tasks whose role matches this value.
            Pass ``None`` (default) to sync all tasks.
    """

    def __init__(self, default_role: str | None = None) -> None:
        self._role_filter = default_role
        self._adapter: Any | None = None  # lazy-initialized

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    @hookimpl
    def on_task_created(self, task_id: str, role: str, title: str) -> None:
        """Log that a task was created (no Jira action needed at creation)."""
        log.debug("JiraPlugin: task %s created (role=%s, title=%r)", task_id, role, title)

    @hookimpl
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        """Transition the linked Jira issue to Done."""
        if self._role_filter and role != self._role_filter:
            return
        self._sync_async(task_id=task_id, conclusion="done", detail=result_summary)

    @hookimpl
    def on_task_failed(self, task_id: str, role: str, error: str) -> None:
        """Transition the linked Jira issue and add a failure comment."""
        if self._role_filter and role != self._role_filter:
            return
        self._sync_async(task_id=task_id, conclusion="failed", detail=error)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _sync_async(self, task_id: str, conclusion: str, detail: str) -> None:
        """Dispatch the Jira sync on a daemon thread."""
        t = threading.Thread(
            target=self._do_sync,
            kwargs={"task_id": task_id, "conclusion": conclusion, "detail": detail},
            daemon=True,
        )
        t.start()

    def _do_sync(self, task_id: str, conclusion: str, detail: str) -> None:
        """Perform the actual Jira API calls."""
        try:
            adapter = self._get_adapter()
        except Exception as exc:
            log.warning("JiraPlugin: could not initialize adapter: %s", exc)
            return

        # Fetch the task to get external_ref
        try:
            from bernstein_sdk import BernsteinClient

            with BernsteinClient() as client:
                task = client.get_task(task_id)
        except Exception as exc:
            log.warning("JiraPlugin: could not fetch task %s: %s", task_id, exc)
            return

        if not task.external_ref.startswith("jira:"):
            log.debug("JiraPlugin: task %s has no jira ref, skipping", task_id)
            return

        issue_key = task.external_ref[len("jira:") :]

        # Transition the issue
        try:
            synced = adapter.sync_task_to_jira(task)
            if synced:
                log.info("JiraPlugin: synced task %s → Jira %s (%s)", task_id, issue_key, conclusion)
        except Exception as exc:
            log.warning("JiraPlugin: failed to transition %s: %s", issue_key, exc)
            return

        # Add a comment on failure
        if conclusion == "failed" and detail:
            try:
                _add_jira_comment(
                    adapter=adapter,
                    issue_key=issue_key,
                    comment=f"Bernstein task `{task_id}` failed:\n\n```\n{detail[:1000]}\n```",
                )
            except Exception as exc:
                log.warning("JiraPlugin: failed to add comment to %s: %s", issue_key, exc)

    def _get_adapter(self) -> Any:
        """Lazy-initialize the Jira adapter (imports bernstein_sdk on first use)."""
        if self._adapter is None:
            from bernstein_sdk.adapters.jira import JiraAdapter

            self._adapter = JiraAdapter.from_env()
        return self._adapter


def _add_jira_comment(adapter: Any, issue_key: str, comment: str) -> None:
    """Post a plain-text comment to a Jira issue."""
    requests = _import_requests()
    url = f"{adapter._base_url}/rest/api/3/issue/{issue_key}/comment"
    payload = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": comment}],
                }
            ],
        }
    }
    resp = requests.post(
        url,
        auth=adapter._auth,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()


def _import_requests() -> Any:
    try:
        import requests  # type: ignore[import-untyped]

        return requests
    except ImportError as exc:
        raise ImportError("pip install bernstein-sdk[jira]") from exc
