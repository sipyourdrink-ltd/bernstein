"""Example plugin: metrics collector.

Appends structured JSON events to `.sdd/metrics/plugin_events.jsonl` for
every hook that fires.  Useful as a foundation for custom dashboards or
feeding data into an external observability platform.

Usage — add to bernstein.yaml:

    plugins:
      - examples.plugins.metrics_plugin:MetricsPlugin

The JSONL file location can be overridden:

    export BERNSTEIN_METRICS_DIR=/path/to/metrics
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bernstein.plugins import hookimpl

log = logging.getLogger(__name__)


class MetricsPlugin:
    """Writes all lifecycle events to a JSONL metrics file.

    Each line is a JSON object with at minimum:
        {"ts": "2026-03-29T00:00:00+00:00", "event": "task_created", ...}
    """

    def __init__(self, metrics_dir: Path | str | None = None) -> None:
        if metrics_dir is not None:
            self._metrics_dir = Path(metrics_dir)
        elif env := os.getenv("BERNSTEIN_METRICS_DIR"):
            self._metrics_dir = Path(env)
        else:
            self._metrics_dir = Path.cwd() / ".sdd" / "metrics"

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    @hookimpl
    def on_task_created(self, task_id: str, role: str, title: str) -> None:
        self._write("task_created", task_id=task_id, role=role, title=title)

    @hookimpl
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        self._write("task_completed", task_id=task_id, role=role, result_summary=result_summary)

    @hookimpl
    def on_task_failed(self, task_id: str, role: str, error: str) -> None:
        self._write("task_failed", task_id=task_id, role=role, error=error)

    @hookimpl
    def on_agent_spawned(self, session_id: str, role: str, model: str) -> None:
        self._write("agent_spawned", session_id=session_id, role=role, model=model)

    @hookimpl
    def on_agent_reaped(self, session_id: str, role: str, outcome: str) -> None:
        self._write("agent_reaped", session_id=session_id, role=role, outcome=outcome)

    @hookimpl
    def on_evolve_proposal(self, proposal_id: str, title: str, verdict: str) -> None:
        self._write("evolve_proposal", proposal_id=proposal_id, title=title, verdict=verdict)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write(self, event: str, **fields: Any) -> None:
        """Append a JSON event record to plugin_events.jsonl."""
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            **fields,
        }
        try:
            self._metrics_dir.mkdir(parents=True, exist_ok=True)
            path = self._metrics_dir / "plugin_events.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as exc:
            log.warning("MetricsPlugin: could not write event %r: %s", event, exc)
