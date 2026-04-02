"""Main Textual application for the Bernstein TUI session manager."""

from __future__ import annotations

import os
import time
from typing import Any, ClassVar, cast

import httpx
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical

from bernstein.tui.timeline import TaskTimeline, TimelineEntry
from bernstein.tui.widgets import (
    ActionBar,
    AgentLogWidget,
    ShortcutsFooter,
    StatusBar,
    TaskListWidget,
    TaskRow,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVER_URL = os.environ.get("BERNSTEIN_SERVER_URL", "http://localhost:8052")
_POLL_INTERVAL: float = 2.0


def _auth_headers() -> dict[str, str]:
    """Return Authorization header dict if BERNSTEIN_AUTH_TOKEN is set.

    Returns:
        Header dict, possibly empty.
    """
    token = os.environ.get("BERNSTEIN_AUTH_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _get(path: str) -> dict[str, Any] | list[Any] | None:
    """HTTP GET from the task server.

    Args:
        path: URL path (e.g. "/status").

    Returns:
        Parsed JSON, or None when the server is unreachable.
    """
    try:
        resp = httpx.get(f"{SERVER_URL}{path}", timeout=5.0, headers=_auth_headers())
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
    except (httpx.ConnectError, httpx.TimeoutException):
        return None
    except Exception:
        return None


def _patch(path: str, data: dict[str, Any]) -> dict[str, Any] | None:  # type: ignore[reportUnusedFunction]
    """HTTP PATCH to the task server.

    Args:
        path: URL path (e.g. "/tasks/{id}").
        data: JSON body payload.

    Returns:
        Parsed JSON response, or None on failure.
    """
    try:
        resp = httpx.patch(
            f"{SERVER_URL}{path}",
            json=data,
            timeout=5.0,
            headers=_auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------


class BernsteinApp(App[None]):
    """The central Bernstein TUI application."""

    CSS_PATH: ClassVar[str] = "styles.tcss"  # type: ignore[assignment]

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("S", "hard_stop", "Hard stop", show=False, priority=True),
        Binding("enter", "toggle_action_bar", "Actions", show=False),
        Binding("s", "spawn_now", "Spawn now", show=False),
        Binding("p", "prioritize", "Prioritize", show=False),
        Binding("k", "kill_agent", "Kill agent", show=False),
        Binding("x", "cancel_task", "Cancel task", show=False),
        Binding("t", "retry_task", "Retry task", show=False),
        Binding("v", "toggle_timeline", "Timeline", show=True),
        Binding("escape", "close_action_bar", "Close", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("?", "show_help", "Help", show=True),
    ]

    def __init__(self, poll_interval: float = _POLL_INTERVAL) -> None:
        """Initialise the application.

        Args:
            poll_interval: Seconds between task-server polls.
        """
        super().__init__()
        self._poll_interval = poll_interval
        self._start_ts = time.time()
        self._action_bar_visible = False
        self._current_rows: list[TaskRow] = []
        self._log_offsets: dict[str, int] = {}  # session_id → last-read byte offset

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Build the widget tree: status bar, task table, action bar, log, shortcuts footer."""
        yield StatusBar(id="top-bar")
        with Vertical(id="main-body"):
            yield TaskListWidget(id="task-list")
            yield TaskTimeline(id="task-timeline")
            yield ActionBar(id="action-bar")
            yield AgentLogWidget(id="agent-log")
        yield ShortcutsFooter(id="shortcuts-footer")

    def on_mount(self) -> None:
        """Start the periodic poll timer after mounting."""
        # Hide action bar and timeline initially.
        self.query_one("#action-bar", ActionBar).display = False
        self.query_one("#task-timeline", TaskTimeline).display = False

        self.set_interval(self._poll_interval, self.action_refresh)

    # -- actions --------------------------------------------------------------

    def action_refresh(self) -> None:
        """Poll the server for fresh status and update UI."""
        raw = _get("/status")
        if raw is None or not isinstance(raw, dict):
            self.query_one(StatusBar).set_summary(server_online=False)
            return

        data: dict[str, Any] = raw
        self.query_one(StatusBar).set_summary(
            agents_active=int(data.get("active_agents", 0)),
            tasks_done=int(data.get("completed", 0)),
            tasks_total=int(data.get("total", 0)),
            tasks_failed=int(data.get("failed", 0)),
            server_online=True,
        )

        tasks_data: list[Any] = data.get("per_role", [])
        rows: list[TaskRow] = []
        for t in tasks_data:
            if isinstance(t, dict):
                rows.append(TaskRow.from_api(cast("dict[str, Any]", t)))
        self.query_one(TaskListWidget).refresh_tasks(rows)

        # Update timeline if visible
        if self.query_one("#task-timeline", TaskTimeline).display:
            self.run_worker(self._refresh_timeline())

    def action_toggle_timeline(self) -> None:
        """Show/hide the task execution timeline."""
        timeline = self.query_one("#task-timeline", TaskTimeline)
        timeline.display = not timeline.display
        if timeline.display:
            self.run_worker(self._refresh_timeline())

    async def _refresh_timeline(self) -> None:
        """Fetch timeline data and update widget."""
        data = _get("/observability/timeline")
        if data and isinstance(data, dict):
            entries = [
                TimelineEntry(
                    task_id=e["task_id"],
                    title=e["title"],
                    start_time=e["start_time"],
                    end_time=e["end_time"],
                    status=e["status"],
                )
                for e in data.get("entries", [])
            ]
            self.query_one("#task-timeline", TaskTimeline).update_data(entries)

    def action_toggle_action_bar(self) -> None:
        """Toggle the action bar for the selected task."""
        action_bar = self.query_one(ActionBar)
        action_bar.display = not action_bar.display
        if action_bar.display:
            action_bar.focus()

    def action_close_action_bar(self) -> None:
        """Hide the action bar."""
        self.query_one(ActionBar).display = False
        self.query_one(TaskListWidget).focus()

    async def action_quit(self) -> None:
        """Exit the application."""
        self.exit()

    # -- sub-screens ----------------------------------------------------------

    def action_show_help(self) -> None:
        """Show help overlay."""
        from bernstein.tui.help_screen import HelpScreen

        self.push_screen(HelpScreen())


if __name__ == "__main__":
    BernsteinApp().run()
