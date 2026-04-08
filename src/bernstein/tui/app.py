"""Main Textual application for the Bernstein TUI session manager."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

if TYPE_CHECKING:
    from rich.text import Text

import httpx
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from bernstein.tui.accessibility import AccessibilityConfig, detect_accessibility
from bernstein.tui.keybinding_config import resolve_all_bindings as _resolve_all_bindings
from bernstein.tui.split_pane import SplitPaneState
from bernstein.tui.timeline import TaskTimeline, TimelineEntry
from bernstein.tui.toast import ToastManager, render_toast_stack
from bernstein.tui.widgets import (
    ActionBar,
    AgentLogWidget,
    ApprovalEntry,
    ApprovalPanel,
    CoordinatorDashboard,
    CoordinatorRow,
    ScratchpadViewer,
    ShortcutsFooter,
    StatusBar,
    TaskListWidget,
    TaskRow,
    ToolObserverWidget,
    WaterfallWidget,
    classify_role,
)


def _build_app_bindings() -> list[BindingType]:
    """Build BINDINGS from the keybinding_config system (TUI-004).

    Resolved at module load time so Textual can see them as a class variable.
    User overrides from ~/.bernstein/keybindings.yaml and keybindings.json
    are applied automatically.
    """
    return [Binding(e.key, e.action, e.description, show=e.show, priority=e.priority) for e in _resolve_all_bindings()]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

SERVER_URL = os.environ.get("BERNSTEIN_SERVER_URL", "http://localhost:8052")
_POLL_INTERVAL: float = 2.0

#: CSS selector for the waterfall trace view widget.
_WATERFALL_VIEW_SELECTOR = "#waterfall-view"
#: CSS selector for the approval panel widget.
_APPROVAL_PANEL_SELECTOR = "#approval-panel"


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
# Toast overlay widget (TUI-009)
# ---------------------------------------------------------------------------


class _ToastOverlay(Static):
    """Overlay widget that renders active toast notifications.

    Docked at the bottom-right corner, renders the ToastManager's active
    toasts as a stacked list. Hidden when there are no active toasts.
    """

    DEFAULT_CSS = """
    _ToastOverlay {
        dock: bottom;
        layer: overlay;
        align: right bottom;
        width: 54;
        height: auto;
        padding: 0 1 1 1;
        background: transparent;
    }
    """

    def __init__(self, manager: ToastManager, **kwargs: Any) -> None:
        """Initialise the overlay.

        Args:
            manager: Shared ToastManager instance.
            **kwargs: Forwarded to Static.
        """
        super().__init__(**kwargs)
        self._manager = manager

    def render(self) -> Text:
        """Render the active toast stack.

        Returns:
            Rich Text of all active toasts stacked vertically.
        """
        return render_toast_stack(self._manager)


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------


class BernsteinApp(App[None]):
    """The central Bernstein TUI application."""

    TITLE = "Bernstein"
    CSS_PATH: ClassVar[str] = "styles.tcss"  # type: ignore[assignment]

    #: Resize debounce delay in seconds (TUI-001).
    RESIZE_DEBOUNCE_S: ClassVar[float] = 0.2

    BINDINGS: ClassVar[list[BindingType]] = _build_app_bindings()

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
        self._resize_timer: object | None = None  # debounce timer handle (TUI-001)
        # TUI-013: detect accessibility mode from environment
        self.accessibility: AccessibilityConfig = AccessibilityConfig.from_level(detect_accessibility())
        # TUI-008: split-pane state
        self._split = SplitPaneState()
        # TUI-009: toast notification manager
        self._toasts = ToastManager()
        # Track seen task IDs to detect completions
        self._seen_done: set[str] = set()

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Build the widget tree: status bar, split-pane body, toast overlay, shortcuts footer."""
        yield StatusBar(id="top-bar")
        # TUI-008: Horizontal container to support split-pane layout
        with Horizontal(id="main-body"):
            with Vertical(id="left-pane"):
                yield TaskListWidget(id="task-list")
                yield TaskTimeline(id="task-timeline")
                yield WaterfallWidget(id="waterfall-view")
                yield ScratchpadViewer(id="scratchpad-viewer")
                yield CoordinatorDashboard(id="coordinator-dashboard")
                yield ApprovalPanel(id="approval-panel")
                yield ToolObserverWidget(id="tool-observer")
                yield ActionBar(id="action-bar")
            with Vertical(id="right-pane"):
                yield AgentLogWidget(id="agent-log")
        # TUI-009: toast overlay widget
        yield _ToastOverlay(self._toasts, id="toast-overlay")
        yield ShortcutsFooter(id="shortcuts-footer")

    def on_mount(self) -> None:
        """Start the periodic poll timer after mounting."""
        # Hide action bar, timeline, scratchpad, waterfall, and others initially.
        self.query_one("#action-bar", ActionBar).display = False
        self.query_one("#task-timeline", TaskTimeline).display = False
        self.query_one(_WATERFALL_VIEW_SELECTOR, WaterfallWidget).display = False
        self.query_one("#scratchpad-viewer", ScratchpadViewer).display = False
        self.query_one("#coordinator-dashboard", CoordinatorDashboard).display = False
        self.query_one(_APPROVAL_PANEL_SELECTOR, ApprovalPanel).display = False
        self.query_one("#tool-observer", ToolObserverWidget).display = False

        # TUI-008: right pane hidden until split is toggled on
        self.query_one("#right-pane").display = False

        # TUI-013: apply accessibility CSS class when enabled
        if self.accessibility.no_animations:
            self.add_class("no-animations")
        if self.accessibility.high_contrast:
            self.add_class("high-contrast")

        self._load_historical_logs()
        self.set_interval(self._poll_interval, self.action_refresh)
        # TUI-009: prune expired toasts and refresh toast overlay every second
        self.set_interval(1.0, self._tick_toasts)

    def on_resize(self, event: object) -> None:
        """Debounce terminal resize events to avoid layout crashes (TUI-001).

        Args:
            event: The Textual Resize event.
        """
        if self._resize_timer is not None:
            self._resize_timer.stop()  # type: ignore[union-attr]
        self._resize_timer = self.set_timer(
            self.RESIZE_DEBOUNCE_S,
            self._apply_resize,
        )

    def _apply_resize(self) -> None:
        """Apply debounced resize with error protection (TUI-001)."""
        self._resize_timer = None
        try:
            self.refresh(layout=True)
        except Exception:
            logger.debug("Layout calculation error during resize (ignored)", exc_info=True)

    # -- historical log loading -----------------------------------------------

    _MAX_HISTORICAL_LINES: ClassVar[int] = 200

    def _load_historical_logs(self) -> None:
        """Read existing agent log files and display them dimmed in the log widget.

        Scans ``.sdd/runtime/*.log`` for any pre-existing log content, loads
        the tail of each file (capped at :attr:`_MAX_HISTORICAL_LINES` total),
        and records byte offsets so that future reads only fetch new data.
        """
        runtime_dir = Path(".sdd/runtime")
        if not runtime_dir.is_dir():
            return

        log_files = sorted(runtime_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
        all_lines: list[str] = []

        for log_path in log_files:
            try:
                size = log_path.stat().st_size
            except OSError:
                continue
            if size == 0:
                continue

            # Record current end-of-file so polling only reads new bytes.
            session_id = log_path.stem
            self._log_offsets[session_id] = size

            try:
                content = log_path.read_text(errors="replace")
            except OSError:
                continue

            lines = [ln for ln in content.splitlines() if ln.strip()]
            all_lines.extend(lines)

        # Keep only the most recent lines to avoid flooding the widget.
        if all_lines:
            tail = all_lines[-self._MAX_HISTORICAL_LINES :]
            self.query_one("#agent-log", AgentLogWidget).load_historical_lines(tail)

    # -- actions --------------------------------------------------------------

    def action_refresh(self) -> None:
        """Poll the server for fresh status and update UI."""
        raw = _get("/status")
        if raw is None or not isinstance(raw, dict):
            self.query_one(StatusBar).set_summary(server_online=False)
            return

        data: dict[str, Any] = raw
        transition_reasons_raw = data.get("transition_reasons")
        transition_reasons: dict[str, dict[str, float]] | None = None
        if isinstance(transition_reasons_raw, dict):
            transition_reasons = cast("dict[str, dict[str, float]]", transition_reasons_raw)
        self.query_one(StatusBar).set_summary(
            agents_active=int(data.get("active_agents", 0)),
            tasks_done=int(data.get("completed", 0)),
            tasks_total=int(data.get("total", 0)),
            tasks_failed=int(data.get("failed", 0)),
            server_online=True,
            transition_reasons=transition_reasons,
        )

        tasks_data: list[Any] = data.get("per_role", [])
        rows: list[TaskRow] = []
        for t in tasks_data:
            if isinstance(t, dict):
                rows.append(TaskRow.from_api(cast("dict[str, Any]", t)))
        self.query_one(TaskListWidget).refresh_tasks(rows)

        # TUI-009: detect newly completed tasks and emit toasts
        for row in rows:
            if row.status == "done" and row.task_id not in self._seen_done:
                self._seen_done.add(row.task_id)
                self._toasts.task_completed(row.task_id, row.title)
                self.query_one("#toast-overlay", _ToastOverlay).refresh()

        # Update timeline if visible
        if self.query_one("#task-timeline", TaskTimeline).display:
            self.run_worker(self._refresh_timeline())

    # -- TUI-008: split-pane --------------------------------------------------

    def action_toggle_split_pane(self) -> None:
        """Toggle split-pane layout (task list left, agent log right)."""
        enabled = self._split.toggle()
        left = self.query_one("#left-pane")
        right = self.query_one("#right-pane")
        if enabled:
            ratio = self._split.ratio
            left.styles.width = f"{int(ratio * 100)}%"
            right.styles.width = f"{int((1 - ratio) * 100)}%"
            right.display = True
            left.focus()
        else:
            left.styles.width = "1fr"
            right.display = False

    # -- TUI-009: toast ticker ------------------------------------------------

    def _tick_toasts(self) -> None:
        """Prune expired toasts and refresh the overlay."""
        pruned = self._toasts.prune()
        if pruned or self._toasts.count > 0:
            self.query_one("#toast-overlay", _ToastOverlay).refresh()

    def action_dismiss_toasts(self) -> None:
        """Dismiss all active toast notifications."""
        self._toasts.dismiss_all()
        self.query_one("#toast-overlay", _ToastOverlay).refresh()

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

    def action_toggle_waterfall(self) -> None:
        """Show/hide the waterfall trace view."""
        waterfall = self.query_one(_WATERFALL_VIEW_SELECTOR, WaterfallWidget)
        waterfall.display = not waterfall.display
        if waterfall.display:
            self.run_worker(self._refresh_waterfall())
            waterfall.focus()

    async def _refresh_waterfall(self) -> None:
        """Fetch the most recent trace and render it as waterfall batches."""
        from bernstein.core.traces import TraceStore, group_trace_steps_into_batches

        traces_dir = Path(".sdd/traces")
        store = TraceStore(traces_dir)
        traces = store.list_traces(limit=1)
        if not traces:
            return
        latest = traces[0]
        batches = group_trace_steps_into_batches(latest.steps)
        self.query_one(_WATERFALL_VIEW_SELECTOR, WaterfallWidget).update_batches(batches)

    def action_toggle_scratchpad(self) -> None:
        """Show/hide the scratchpad viewer."""
        scratchpad = self.query_one("#scratchpad-viewer", ScratchpadViewer)
        scratchpad.display = not scratchpad.display
        if scratchpad.display:
            self.run_worker(self._refresh_scratchpad())
            scratchpad.focus()

    async def _refresh_scratchpad(self) -> None:
        """Fetch scratchpad entries and update widget."""
        from bernstein.tui.widgets import list_scratchpad_files

        entries = list_scratchpad_files()
        self.query_one("#scratchpad-viewer", ScratchpadViewer).refresh_entries(entries)

    def action_scratchpad_filter(self) -> None:
        """Open scratchpad filter input."""
        # Toggle scratchpad if not visible
        scratchpad = self.query_one("#scratchpad-viewer", ScratchpadViewer)
        if not scratchpad.display:
            scratchpad.display = True
            self.run_worker(self._refresh_scratchpad())

        # Show filter prompt in status bar
        self._prompt_scratchpad_filter()

    def _prompt_scratchpad_filter(self) -> None:
        """Show filter prompt for scratchpad viewer."""
        from textual.widgets import Input

        # Check if filter input already exists
        existing = self.query("#scratchpad-filter")
        if existing:
            existing.first().remove()
            return

        input_widget = Input(placeholder="Filter scratchpad files...", id="scratchpad-filter")
        self.mount(input_widget)
        input_widget.focus()

    def on_input_submitted(self, event: Any) -> None:
        """Handle filter input submission."""
        from textual.widgets import Input

        if isinstance(event, Input.Submitted) and event.input.id == "scratchpad-filter":
            query = event.value
            scratchpad = self.query_one("#scratchpad-viewer", ScratchpadViewer)
            scratchpad.set_filter(query)
            event.input.remove()
            scratchpad.focus()

    def action_toggle_coordinator(self) -> None:
        """Show/hide the coordinator mode dashboard."""
        dashboard = self.query_one("#coordinator-dashboard", CoordinatorDashboard)
        dashboard.display = not dashboard.display
        if dashboard.display:
            self._refresh_coordinator_dashboard()
            dashboard.focus()

    def action_toggle_approvals(self) -> None:
        """Show/hide the interactive approval panel."""
        panel = self.query_one(_APPROVAL_PANEL_SELECTOR, ApprovalPanel)
        panel.display = not panel.display
        if panel.display:
            self.run_worker(self._refresh_approvals())
            panel.focus()

    async def _refresh_approvals(self) -> None:
        """Fetch pending approvals from the server."""
        data = _get("/approvals")
        if data and isinstance(data, dict):
            entries = [
                ApprovalEntry(
                    task_id=cast("dict[str, Any]", item)["task_id"],
                    task_title=cast("dict[str, Any]", item).get("task_title", ""),
                    session_id=cast("dict[str, Any]", item).get("session_id", ""),
                    diff_preview=cast("dict[str, Any]", item).get("diff", ""),
                    test_summary=cast("dict[str, Any]", item).get("test_summary", ""),
                )
                for item in data.get("pending", [])
                if isinstance(item, dict) and "task_id" in item
            ]
            self.query_one(_APPROVAL_PANEL_SELECTOR, ApprovalPanel).refresh_entries(entries)

    def _refresh_coordinator_dashboard(self) -> None:
        """Populate coordinator dashboard from current task data."""
        rows = [
            CoordinatorRow(
                role=tr.role,
                task_id=tr.task_id,
                title=tr.title,
                status=tr.status,
                elapsed=tr.elapsed,
            )
            for tr in self._current_rows
        ]
        rows.sort(key=lambda row: {"coordinator": 0, "worker": 1}.get(classify_role(row.role), 2))
        self.query_one("#coordinator-dashboard", CoordinatorDashboard).refresh_data(rows)

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

    @staticmethod
    def _count_active_agents() -> int:
        """Count active agents recorded in the local runtime snapshot.

        Returns:
            Number of active agent entries with a PID in `.sdd/runtime/agents.json`.
        """
        agents_file = Path(".sdd/runtime/agents.json")
        if not agents_file.exists():
            return 0

        try:
            data = json.loads(agents_file.read_text())
        except Exception:
            return 0
        if not isinstance(data, list):
            return 0
        items = cast("list[object]", data)
        count = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            agent = cast("dict[str, object]", item)
            if agent.get("pid"):
                count += 1
        return count


def _kill_agent(session_id: str) -> bool:  # type: ignore[reportUnusedFunction]
    """Kill a specific agent process.

    Args:
        session_id: The session ID of the agent to kill.

    Returns:
        True if the agent was found and a kill signal was sent.
    """
    agents_file = Path(".sdd/runtime/agents.json")
    if not agents_file.exists():
        return False

    from bernstein.core.platform_compat import kill_process

    try:
        data = json.loads(agents_file.read_text())
        for agent in data:
            if agent.get("id") == session_id:
                pid = agent.get("pid")
                if pid:
                    return kill_process(pid, sig=9)
    except Exception:
        pass
    return False


def _kill_all_agents() -> int:  # type: ignore[reportUnusedFunction]
    """Kill all active agent processes listed in agents.json.

    Returns:
        The number of agents successfully killed.
    """
    from bernstein.core.platform_compat import kill_process

    agents_file = Path(".sdd/runtime/agents.json")
    if not agents_file.exists():
        return 0

    killed_count = 0
    try:
        data = json.loads(agents_file.read_text())
        for agent in data:
            pid = agent.get("pid")
            if pid and kill_process(pid, sig=9):
                killed_count += 1
    except Exception:
        pass
    return killed_count


if __name__ == "__main__":
    BernsteinApp().run()
