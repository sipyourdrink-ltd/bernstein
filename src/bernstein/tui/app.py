"""Main Textual application for the Bernstein TUI session manager."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical

if TYPE_CHECKING:
    from textual.widgets import DataTable

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


def _post(path: str, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """HTTP POST to the task server.

    Args:
        path: URL path (e.g. "/shutdown").
        data: JSON body payload.

    Returns:
        Parsed JSON response, or None on failure.
    """
    try:
        resp = httpx.post(
            f"{SERVER_URL}{path}",
            json=data or {},
            timeout=5.0,
            headers=_auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
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


def _kill_agent(session_id: str) -> bool:
    """Kill an agent process by its session ID.

    Reads agents.json to find the PID, then sends SIGTERM to the process group.

    Args:
        session_id: The agent's session identifier.

    Returns:
        True if the signal was sent successfully, False otherwise.
    """
    agents_json = Path(".sdd/runtime/agents.json")
    if not agents_json.exists():
        return False
    try:
        data = json.loads(agents_json.read_text())
    except (OSError, ValueError):
        return False
    for agent in data.get("agents", []):
        if agent.get("id") == session_id:
            pid = agent.get("pid")
            if pid:
                try:
                    os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
                    return True
                except OSError:
                    pass
    return False


def _kill_all_agents() -> int:
    """Kill all agent processes listed in agents.json.

    Returns:
        Number of agents successfully signalled.
    """
    agents_json = Path(".sdd/runtime/agents.json")
    if not agents_json.exists():
        return 0
    try:
        data = json.loads(agents_json.read_text())
    except (OSError, ValueError):
        return 0
    killed = 0
    for agent in data.get("agents", []):
        pid = agent.get("pid")
        if pid:
            try:
                os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
                killed += 1
            except OSError:
                pass
    return killed


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

CSS_PATH = "styles.tcss"


class BernsteinApp(App[None]):
    """Textual TUI for monitoring a Bernstein orchestration session.

    Slim, htop-inspired layout: no Header/Footer widgets, single-line
    status bar, interactive task list with action bar, and a compact log.
    """

    TITLE = "Bernstein"
    CSS_PATH = CSS_PATH

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
        Binding("escape", "close_action_bar", "Close", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("j", "cursor_down", "Down", show=False),
    ]

    def __init__(self, *, poll_interval: float = _POLL_INTERVAL) -> None:
        """Initialise the app.

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
            yield ActionBar(id="action-bar")
            yield AgentLogWidget(id="agent-log")
        yield ShortcutsFooter(id="shortcuts-footer")

    def on_mount(self) -> None:
        """Start the periodic poll timer after mounting."""
        # Hide action bar initially.
        action_bar = self.query_one("#action-bar", ActionBar)
        action_bar.display = False

        self.set_interval(self._poll_interval, self._poll_server)
        # Fire an immediate poll so the UI is populated straight away.
        self.call_later(self._poll_server)

    # -- actions --------------------------------------------------------------

    def action_refresh(self) -> None:
        """Force an immediate server poll (bound to 'r')."""
        self._poll_server()

    def action_soft_stop(self) -> None:
        """Soft stop: POST to /shutdown and show status."""
        status_bar = self.query_one("#top-bar", StatusBar)
        log_widget = self.query_one("#agent-log", AgentLogWidget)
        result = _post("/shutdown")
        if result is not None:
            log_widget.append_line("[yellow]Soft stop requested \u2014 waiting for agents...[/yellow]")
        else:
            log_widget.append_line("[red]Soft stop failed \u2014 server unreachable[/red]")
        status_bar.update("Stopping...")

    def action_spawn_now(self) -> None:
        """Force-spawn an agent for the selected task (action bar must be open)."""
        if not self._action_bar_visible:
            return
        task_id = self._selected_task_id()
        if not task_id:
            return
        log_widget = self.query_one("#agent-log", AgentLogWidget)
        result = _post(f"/tasks/{task_id}/force-claim")
        if result is not None:
            log_widget.append_line(f"[green]Spawn queued for task {task_id} (priority 0)[/green]")
        else:
            log_widget.append_line(f"[red]Spawn failed for task {task_id}[/red]")
        self.action_close_action_bar()

    def action_prioritize(self) -> None:
        """Bump the selected task to priority 0 (next in queue)."""
        task_id = self._selected_task_id()
        if not task_id:
            return
        log_widget = self.query_one("#agent-log", AgentLogWidget)
        result = _post(f"/tasks/{task_id}/prioritize")
        if result is not None:
            log_widget.append_line(f"[cyan]Task {task_id} bumped to priority 0[/cyan]")
        else:
            log_widget.append_line(f"[red]Prioritize failed for task {task_id}[/red]")
        self.action_close_action_bar()

    def action_cancel_task(self) -> None:
        """Cancel the selected task (bound to 'x')."""
        task_id = self._selected_task_id()
        if not task_id:
            return
        log_widget = self.query_one("#agent-log", AgentLogWidget)
        result = _post(f"/tasks/{task_id}/cancel", {"reason": "cancelled via TUI"})
        if result is not None:
            log_widget.append_line(f"[dim]Task {task_id} cancelled[/dim]")
        else:
            log_widget.append_line(f"[red]Cancel failed for task {task_id}[/red]")
        self.action_close_action_bar()

    def action_retry_task(self) -> None:
        """Retry the selected task by resetting it to open with priority 0 (bound to 't')."""
        task_id = self._selected_task_id()
        if not task_id:
            return
        log_widget = self.query_one("#agent-log", AgentLogWidget)
        result = _post(f"/tasks/{task_id}/force-claim")
        if result is not None:
            log_widget.append_line(f"[green]Task {task_id} reset to open (priority 0)[/green]")
        else:
            log_widget.append_line(f"[red]Retry failed for task {task_id} \u2014 may be terminal[/red]")
        self.action_close_action_bar()

    def action_hard_stop(self) -> None:
        """Hard stop: kill all agent PIDs immediately."""
        log_widget = self.query_one("#agent-log", AgentLogWidget)
        killed = _kill_all_agents()
        log_widget.append_line(f"[bold red]Hard stop \u2014 killed {killed} agent(s)[/bold red]")

    def action_toggle_action_bar(self) -> None:
        """Toggle the inline action bar for the selected task."""
        task_list = self.query_one("#task-list", TaskListWidget)
        action_bar = self.query_one("#action-bar", ActionBar)

        if self._action_bar_visible:
            action_bar.display = False
            self._action_bar_visible = False
            return

        # Find the selected task row.
        row_key, _ = task_list.coordinate_to_cell_key(task_list.cursor_coordinate)
        task_id = str(row_key.value) if row_key.value is not None else ""
        if not task_id:
            return

        action_bar.set_task(task_id)
        action_bar.display = True
        self._action_bar_visible = True

    def action_close_action_bar(self) -> None:
        """Close the action bar if visible."""
        if self._action_bar_visible:
            action_bar = self.query_one("#action-bar", ActionBar)
            action_bar.display = False
            self._action_bar_visible = False

    def action_kill_agent(self) -> None:
        """Kill the agent for the currently selected task."""
        task_list = self.query_one("#task-list", TaskListWidget)
        log_widget = self.query_one("#agent-log", AgentLogWidget)

        row_key, _ = task_list.coordinate_to_cell_key(task_list.cursor_coordinate)
        task_id = str(row_key.value) if row_key.value is not None else ""
        if not task_id:
            return

        # Find session_id from cached rows.
        session_id = ""
        for r in self._current_rows:
            if r.task_id == task_id:
                session_id = r.session_id
                break

        if session_id and _kill_agent(session_id):
            log_widget.append_line(f"[red]Killed agent {session_id} (task {task_id})[/red]")
        else:
            log_widget.append_line(f"[dim]No running agent found for task {task_id}[/dim]")

    def action_cursor_down(self) -> None:
        """Move task list cursor down."""
        task_list = self.query_one("#task-list", TaskListWidget)
        task_list.action_cursor_down()

    def action_cursor_up(self) -> None:
        """Move task list cursor up."""
        task_list = self.query_one("#task-list", TaskListWidget)
        task_list.action_cursor_up()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle click/Enter on a task row \u2014 toggle the action bar.

        Args:
            event: The row-selected event from the DataTable.
        """
        action_bar = self.query_one("#action-bar", ActionBar)
        task_id = str(event.row_key.value) if event.row_key.value is not None else ""
        if not task_id:
            return

        action_bar.set_task(task_id)
        action_bar.display = True
        self._action_bar_visible = True

    # -- helpers --------------------------------------------------------------

    def _selected_task_id(self) -> str:
        """Return the task ID of the currently highlighted row, or empty string."""
        task_list = self.query_one("#task-list", TaskListWidget)
        try:
            row_key, _ = task_list.coordinate_to_cell_key(task_list.cursor_coordinate)
            return str(row_key.value) if row_key.value is not None else ""
        except Exception:
            return ""

    # -- data fetching --------------------------------------------------------

    def _poll_server(self) -> None:
        """Fetch data from the task server and update widgets."""
        status_bar = self.query_one("#top-bar", StatusBar)
        task_list = self.query_one("#task-list", TaskListWidget)
        log_widget = self.query_one("#agent-log", AgentLogWidget)

        status_raw = _get("/status")
        if status_raw is None or not isinstance(status_raw, dict):
            status_bar.set_summary(server_online=False)
            return

        tasks_raw = _get("/tasks")
        tasks: list[dict[str, Any]] = (
            [t for t in tasks_raw if isinstance(t, dict)] if isinstance(tasks_raw, list) else []
        )

        # Parse tasks.
        rows = [TaskRow.from_api(t) for t in tasks]
        self._current_rows = rows
        task_list.refresh_tasks(rows)

        # Agent count from agents.json.
        agents_active = self._count_active_agents()

        elapsed = time.time() - self._start_ts
        status_bar.set_summary(
            agents_active=agents_active,
            tasks_done=int(status_raw.get("done", 0)),
            tasks_total=int(status_raw.get("total", 0)),
            tasks_failed=int(status_raw.get("failed", 0)),
            cost_usd=float(status_raw.get("total_cost_usd", 0.0)),
            elapsed_seconds=elapsed,
            server_online=True,
        )

        # Append recent task completions / failures to the log.
        self._update_log(log_widget, tasks)

        # Tail live agent log files for near-real-time output.
        self._tail_agent_logs(log_widget)

        # Force a screen redraw so updates render without waiting for input.
        self.refresh()

    @staticmethod
    def _count_active_agents() -> int:
        """Read agent count from the orchestrator's agents.json file.

        Returns:
            Number of non-dead agents.
        """
        agents_json = Path(".sdd/runtime/agents.json")
        if not agents_json.exists():
            return 0
        try:
            data = json.loads(agents_json.read_text())
            agents: list[dict[str, Any]] = data.get("agents", [])
            return sum(1 for a in agents if a.get("status") != "dead")
        except (OSError, ValueError, KeyError):
            return 0

    def _update_log(self, log_widget: AgentLogWidget, tasks: list[dict[str, Any]]) -> None:
        """Write recent task events to the agent log widget.

        Shows the most recent task transitions as log entries.

        Args:
            log_widget: The RichLog widget to append to.
            tasks: Raw task dicts from the server.
        """
        for task in tasks:
            progress: list[dict[str, Any]] = task.get("progress_log", [])
            if not progress:
                continue
            last = progress[-1]
            msg = last.get("message", "")
            task_id = task.get("id", "?")
            status = task.get("status", "open")
            if msg:
                log_widget.append_line(f"[{status}] {task_id}: {msg}")

    def _tail_agent_logs(self, log_widget: AgentLogWidget) -> None:
        """Tail log files for active agents and stream new output to the log widget.

        Reads from the last known byte offset in each agent's ``.sdd/runtime/{id}.log``
        file, appending any new lines since the previous poll. Called on every poll
        tick for near-real-time agent output in the TUI.

        Args:
            log_widget: The widget to append log lines to.
        """
        agents_json = Path(".sdd/runtime/agents.json")
        if not agents_json.exists():
            return
        try:
            data = json.loads(agents_json.read_text())
        except (OSError, ValueError):
            return

        for agent in data.get("agents", []):
            if agent.get("status") == "dead":
                continue
            session_id = agent.get("id", "")
            if not session_id:
                continue
            log_path = Path(f".sdd/runtime/{session_id}.log")
            if not log_path.exists():
                continue
            try:
                size = log_path.stat().st_size
                offset = self._log_offsets.get(session_id, 0)
                if size <= offset:
                    continue
                with log_path.open("rb") as f:
                    f.seek(offset)
                    new_bytes = f.read()
                self._log_offsets[session_id] = size
                role = agent.get("role", "?")
                text = new_bytes.decode("utf-8", errors="replace")
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped:
                        log_widget.append_line(f"[cyan]{role}[/cyan][dim]/{session_id[:8]}[/dim] {stripped}")
            except OSError:
                pass
