"""Example plugin: Linear bidirectional sync.

Keeps Linear issues and Bernstein tasks in sync via the plugin hook system.

Features:
- ``on_task_completed`` → transition the linked Linear issue to "Done"
- ``on_task_failed``    → transition the linked Linear issue to "Cancelled"
- ``on_task_created``   → log for auditing

The Linear issue identifier is expected in ``task.external_ref`` as
``"linear:ENG-42"``.  If the ref is absent, the plugin is a no-op.

Prerequisites
-------------
Install the SDK::

    pip install bernstein-sdk

Configure in bernstein.yaml::

    plugins:
      - examples.plugins.linear_plugin:LinearPlugin

Set environment variable::

    export LINEAR_API_KEY=lin_api_...

Optional custom state mappings::

    from bernstein_sdk.state_map import BernsteinToLinear, TaskStatus
    BernsteinToLinear.register(TaskStatus.FAILED, "Blocked")
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from bernstein.plugins import hookimpl

log = logging.getLogger(__name__)


class LinearPlugin:
    """Synchronize Bernstein task state changes back to Linear issues.

    All Linear GraphQL calls run on daemon threads.

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
        log.debug("LinearPlugin: task %s created (role=%s)", task_id, role)

    @hookimpl
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        """Transition the linked Linear issue to Done."""
        if self._role_filter and role != self._role_filter:
            return
        self._sync_async(task_id=task_id, conclusion="done")

    @hookimpl
    def on_task_failed(self, task_id: str, role: str, error: str) -> None:
        """Transition the linked Linear issue to Cancelled."""
        if self._role_filter and role != self._role_filter:
            return
        self._sync_async(task_id=task_id, conclusion="failed")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _sync_async(self, task_id: str, conclusion: str) -> None:
        t = threading.Thread(
            target=self._do_sync,
            kwargs={"task_id": task_id, "conclusion": conclusion},
            daemon=True,
        )
        t.start()

    def _do_sync(self, task_id: str, conclusion: str) -> None:
        try:
            adapter = self._get_adapter()
        except Exception as exc:
            log.warning("LinearPlugin: could not initialize adapter: %s", exc)
            return

        try:
            from bernstein_sdk import BernsteinClient

            with BernsteinClient() as client:
                task = client.get_task(task_id)
        except Exception as exc:
            log.warning("LinearPlugin: could not fetch task %s: %s", task_id, exc)
            return

        if not task.external_ref.startswith("linear:"):
            log.debug("LinearPlugin: task %s has no linear ref, skipping", task_id)
            return

        identifier = task.external_ref[len("linear:") :]
        try:
            synced = adapter.sync_task_to_linear(task)
            if synced:
                log.info(
                    "LinearPlugin: synced task %s → Linear %s (%s)",
                    task_id,
                    identifier,
                    conclusion,
                )
            else:
                log.warning(
                    "LinearPlugin: could not find matching state for task %s (%s)",
                    task_id,
                    conclusion,
                )
        except Exception as exc:
            log.warning("LinearPlugin: failed to sync %s: %s", identifier, exc)

    def _get_adapter(self) -> Any:
        if self._adapter is None:
            from bernstein_sdk.adapters.linear import LinearAdapter

            self._adapter = LinearAdapter.from_env()
        return self._adapter
