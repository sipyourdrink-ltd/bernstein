"""Scheduling panel widget for TUI — visualizes scheduling decisions."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from textual.widgets import DataTable

if TYPE_CHECKING:
    from pathlib import Path


class SchedulingPanel(DataTable[Any]):
    """DataTable showing scheduling decisions.

    Columns: Task | → Agent | Model | Reason
    Populated from .sdd/metrics/routing_decisions.jsonl
    Auto-refreshes every 5s.
    """

    DEFAULT_CSS = """
    SchedulingPanel {
        height: auto;
        max-height: 50%;
    }
    """

    def __init__(self, workdir: Path | None = None, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._workdir = workdir
        self._setup_columns()

    def _setup_columns(self) -> None:
        """Initialize table columns."""
        self.add_columns("Task", "→ Agent", "Model", "Reason")

    def load_decisions(self, limit: int = 20) -> None:
        """Load and display routing decisions.

        Args:
            limit: Maximum number of decisions to show.
        """
        if self._workdir is None:
            return

        filepath = self._workdir / ".sdd" / "metrics" / "routing_decisions.jsonl"

        if not filepath.exists():
            return

        self.clear()

        decisions: list[dict[str, Any]] = []
        with filepath.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = cast("dict[str, Any]", json.loads(line))
                    decisions.append(data)
                except json.JSONDecodeError:
                    continue

        # Show most recent first
        for decision in reversed(decisions[-limit:]):
            task_id = cast("str", decision.get("task_id", "unknown"))
            adapter = cast("str", decision.get("adapter", "unknown"))
            model = f"{cast('str', decision.get('model', 'unknown'))}/{cast('str', decision.get('effort', '?'))}"
            reasons = cast("list[str]", decision.get("reasons", []))
            reason = "; ".join(reasons[:2]) if reasons else ""

            self.add_row(task_id, adapter, model, reason)
