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
from bernstein.tui.command_palette import DEFAULT_PALETTE_COMMANDS, CommandPalette, CommandPaletteScreen, PaletteCommand
from bernstein.tui.keybinding_config import resolve_all_bindings as _resolve_all_bindings
from bernstein.tui.layout_persistence import LayoutConfig, load_layout, save_layout
from bernstein.tui.notification_badge import NotificationCenterPanel, NotificationHistory
from bernstein.tui.progress_bar import TaskProgress
from bernstein.tui.session_recorder import (
    RecordingFrame,
    RecordingSummary,
    SessionPlayer,
    SessionRecorder,
    SessionRecorderPanel,
    list_recordings,
)
from bernstein.tui.split_pane import SplitPaneState
from bernstein.tui.task_context import TaskContextPanel, TaskContextSummary
from bernstein.tui.task_search import TaskSearchInput, matches_task_search, parse_task_search
from bernstein.tui.themes import ThemeMode, cycle_theme, load_theme_config, save_theme_config
from bernstein.tui.timeline import TaskTimeline, TimelineEntry
from bernstein.tui.toast import Toast, ToastLevel, ToastManager, render_toast_stack
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
from bernstein.tui.worktree_status import RuntimeHealthPanel

_AGENTS_JSON_PATH = ".sdd/runtime/agents.json"


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_DICT_STR_ANY = "dict[str, Any]"


def _build_app_bindings() -> list[BindingType]:
    """Build BINDINGS from the keybinding_config system (TUI-004).

    Resolved at module load time so Textual can see them as a class variable.
    User overrides from ~/.bernstein/keybindings.yaml and keybindings.json
    are applied automatically.
    """
    bindings = [
        Binding(e.key, e.action, e.description, show=e.show, priority=e.priority) for e in _resolve_all_bindings()
    ]
    bindings.append(Binding("/", "focus_task_search", "Search", show=True))
    bindings.append(Binding("n", "acknowledge_notifications", "Mark notifications read", show=False))
    bindings.append(Binding("R", "toggle_session_recording", "Toggle recording", show=False))
    bindings.append(Binding("1", "layout_focus", "Focus layout", show=False))
    bindings.append(Binding("2", "layout_balanced", "Balanced layout", show=False))
    bindings.append(Binding("3", "layout_observability", "Observability layout", show=False))
    return cast("list[BindingType]", bindings)


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
#: CSS selectors for panels queried in multiple places.
_TASK_TIMELINE_SELECTOR = "#task-timeline"
_SCRATCHPAD_VIEWER_SELECTOR = "#scratchpad-viewer"
_COORDINATOR_DASHBOARD_SELECTOR = "#coordinator-dashboard"
_TASK_CONTEXT_SELECTOR = "#task-context"
_TOAST_OVERLAY_SELECTOR = "#toast-overlay"


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


def _compute_run_pct(summary: dict[str, Any], data: dict[str, Any]) -> float | None:
    """Compute aggregate run-level progress percentage."""
    tasks_total = int(summary.get("total", data.get("total", 0)))
    if not tasks_total:
        return None
    tasks_done_count = int(summary.get("done", data.get("completed", 0)))
    return (tasks_done_count / tasks_total) * 100.0


def _extract_task_dicts(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract task dicts from status response, falling back to /tasks endpoint."""
    tasks_section = data.get("tasks")
    if isinstance(tasks_section, dict):
        tasks_payload = cast(_CAST_DICT_STR_ANY, tasks_section)
        items = tasks_payload.get("items")
        if isinstance(items, list):
            items_list = cast("list[object]", items)
            return [cast(_CAST_DICT_STR_ANY, item) for item in items_list if isinstance(item, dict)]
        return []
    fallback = _get("/tasks")
    fallback_items = fallback if isinstance(fallback, list) else []
    return [cast(_CAST_DICT_STR_ANY, item) for item in fallback_items if isinstance(item, dict)]


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
        self._layout = load_layout()
        self._current_rows: list[TaskRow] = []
        self._all_rows: list[TaskRow] = []
        self._task_lookup: dict[str, TaskRow] = {}
        self._selected_task_id: str | None = None
        self._search_query = ""
        self._palette_action_labels: dict[str, str] = {}
        self._recent_palette_actions: list[str] = []
        self._log_offsets: dict[str, int] = {}  # session_id → last-read byte offset
        self._resize_timer: object | None = None  # debounce timer handle (TUI-001)
        # TUI-013: detect accessibility mode from environment
        self.accessibility: AccessibilityConfig = AccessibilityConfig.from_level(detect_accessibility())
        # TUI-008: split-pane state
        self._split = SplitPaneState()
        # TUI-009: toast notification manager
        self._toasts = ToastManager()
        self._notifications = NotificationHistory()
        self._recordings_dir = Path(".sdd/recordings/tui")
        self._session_recorder: SessionRecorder | None = None
        self._recording_started_at = 0.0
        self._active_recording_path: Path | None = None
        self._replay_frames: list[RecordingFrame] = []
        self._replay_index = 0
        self._selected_replay_path: Path | None = None
        self._replay_timer: object | None = None
        # Track seen task IDs to detect completions
        self._seen_done: set[str] = set()
        # TUI-011: theme — load persisted preference, fall back to auto-detect
        self._theme_mode: ThemeMode = load_theme_config()
        # TUI-010: cached task progress entries for aggregate run bar
        self._task_progresses: list[TaskProgress] = []

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Build the widget tree: status bar, split-pane body, toast overlay, shortcuts footer."""
        yield StatusBar(id="top-bar")
        # TUI-008: Horizontal container to support split-pane layout
        with Horizontal(id="main-body"):
            with Vertical(id="left-pane"):
                yield TaskSearchInput(id="task-search")
                yield TaskListWidget(id="task-list")
                yield TaskTimeline(id="task-timeline")
                yield WaterfallWidget(id="waterfall-view")
                yield ScratchpadViewer(id="scratchpad-viewer")
                yield CoordinatorDashboard(id="coordinator-dashboard")
                yield ApprovalPanel(id="approval-panel")
                yield ToolObserverWidget(id="tool-observer")
                yield ActionBar(id="action-bar")
            with Vertical(id="right-pane"):
                yield TaskContextPanel(id="task-context")
                yield RuntimeHealthPanel(id="runtime-health")
                yield NotificationCenterPanel(id="notification-center")
                yield SessionRecorderPanel(id="session-recorder")
                yield AgentLogWidget(id="agent-log")
        # TUI-009: toast overlay widget
        yield _ToastOverlay(self._toasts, id="toast-overlay")
        yield ShortcutsFooter(id="shortcuts-footer")

    def on_mount(self) -> None:
        """Start the periodic poll timer after mounting."""
        # Hide action bar, timeline, scratchpad, waterfall, and others initially.
        self.query_one("#action-bar", ActionBar).display = False
        self.query_one(_TASK_TIMELINE_SELECTOR, TaskTimeline).display = "task-timeline" in self._layout.visible_panels
        self.query_one(_WATERFALL_VIEW_SELECTOR, WaterfallWidget).display = (
            "waterfall-view" in self._layout.visible_panels
        )
        self.query_one(_SCRATCHPAD_VIEWER_SELECTOR, ScratchpadViewer).display = (
            "scratchpad-viewer" in self._layout.visible_panels
        )
        self.query_one(_COORDINATOR_DASHBOARD_SELECTOR, CoordinatorDashboard).display = (
            "coordinator-dashboard" in self._layout.visible_panels
        )
        self.query_one(_APPROVAL_PANEL_SELECTOR, ApprovalPanel).display = (
            "approval-panel" in self._layout.visible_panels
        )
        self.query_one("#tool-observer", ToolObserverWidget).display = "tool-observer" in self._layout.visible_panels
        self.query_one(_TASK_CONTEXT_SELECTOR, TaskContextPanel).display = "task-context" in self._layout.visible_panels
        self.query_one("#runtime-health", RuntimeHealthPanel).display = "runtime-health" in self._layout.visible_panels
        self.query_one("#notification-center", NotificationCenterPanel).display = (
            "notification-center" in self._layout.visible_panels
        )
        self.query_one("#session-recorder", SessionRecorderPanel).display = (
            "session-recorder" in self._layout.visible_panels
        )

        # TUI-013: apply accessibility CSS class when enabled
        if self.accessibility.no_animations:
            self.add_class("no-animations")
        if self.accessibility.high_contrast:
            self.add_class("high-contrast")

        # TUI-011: apply initial theme
        self._apply_theme()

        self._split.set_ratio(self._layout.split_ratio)
        if self._layout.split_enabled and not self._split.enabled:
            self._split.toggle()
        self._apply_split_layout()
        self._refresh_notification_center()
        self._refresh_session_recorder_panel()

        self._load_historical_logs()
        self.set_interval(self._poll_interval, self.action_refresh)
        # TUI-009: prune expired toasts and refresh toast overlay every second
        self.set_interval(1.0, self._tick_toasts)

    def _apply_split_layout(self) -> None:
        """Apply the current split-pane state to the live layout."""
        left = self.query_one("#left-pane")
        right = self.query_one("#right-pane")
        if self._split.enabled:
            ratio = self._split.ratio
            left.styles.width = f"{int(ratio * 100)}%"
            right.styles.width = f"{int((1 - ratio) * 100)}%"
            right.display = True
        else:
            left.styles.width = "1fr"
            right.display = False

    def _persist_layout(self) -> None:
        """Persist the current layout state to disk."""
        self._layout = LayoutConfig(
            split_ratio=self._split.ratio,
            split_enabled=self._split.enabled,
            visible_panels=self._layout.visible_panels,
            orientation=self._layout.orientation,
            preset=self._layout.preset,
        )
        save_layout(self._layout)

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
        # TUI-010: compute aggregate run-level progress percentage
        summary = cast(_CAST_DICT_STR_ANY, data.get("summary", {})) if isinstance(data.get("summary"), dict) else {}
        run_pct = _compute_run_pct(summary, data)

        self.query_one(StatusBar).set_summary(
            agents_active=int(summary.get("active_agents", data.get("active_agents", 0))),
            tasks_done=int(summary.get("done", data.get("completed", 0))),
            tasks_total=int(summary.get("total", data.get("total", 0))),
            tasks_failed=int(summary.get("failed", data.get("failed", 0))),
            server_online=True,
            transition_reasons=transition_reasons,
            run_progress_pct=run_pct,
        )

        task_dicts = _extract_task_dicts(data)
        rows = [TaskRow.from_api(item) for item in task_dicts]
        self._all_rows = rows
        self._task_lookup = {row.task_id: row for row in rows}
        self._apply_task_filter()
        runtime_snapshot = data.get("runtime")
        if isinstance(runtime_snapshot, dict):
            runtime = cast(_CAST_DICT_STR_ANY, runtime_snapshot)
            self.query_one(_TASK_CONTEXT_SELECTOR, TaskContextPanel).set_runtime_snapshot(runtime)
            self.query_one("#runtime-health", RuntimeHealthPanel).set_snapshot(runtime)

        # TUI-010: update aggregate progress for run-level bar
        self._task_progresses = [
            TaskProgress(
                task_id=row.task_id,
                custom_pct=row.progress_pct,
            )
            for row in rows
            if row.progress_pct is not None
        ]

        # TUI-009: detect newly completed tasks and emit toasts
        for row in rows:
            if row.status == "done" and row.task_id not in self._seen_done:
                self._seen_done.add(row.task_id)
                self._remember_toast(self._toasts.task_completed(row.task_id, row.title))

        if self._session_recorder is not None and self._session_recorder.recording:
            self._session_recorder.record_frame(
                timestamp=time.time() - self._recording_started_at,
                event_type="status_update",
                data={
                    "summary": summary,
                    "selected_task_id": self._selected_task_id or "",
                    "visible_tasks": [row.task_id for row in rows[:5]],
                },
            )
            self._refresh_session_recorder_panel()

        # Update timeline if visible
        if self.query_one(_TASK_TIMELINE_SELECTOR, TaskTimeline).display:
            self.run_worker(self._refresh_timeline())

    def _apply_task_filter(self) -> None:
        """Apply the current structured task filter to the cached task rows."""
        parsed = parse_task_search(self._search_query)
        visible_rows = [row for row in self._all_rows if matches_task_search(row, parsed)]
        self._current_rows = visible_rows
        self.query_one(TaskListWidget).refresh_tasks(visible_rows)
        self._sync_task_context()

    def _sync_task_context(self) -> None:
        """Update the task context pane from the current selection."""
        current: TaskRow | None = None
        if self._selected_task_id:
            current = self._task_lookup.get(self._selected_task_id)
        if current is None and self._current_rows:
            current = self._current_rows[0]
            self._selected_task_id = current.task_id
        summary = (
            TaskContextSummary(
                task_id=current.task_id,
                title=current.title,
                status=current.status,
                role=current.role,
                priority=current.priority,
                model=current.model,
                assigned_agent=current.assigned_agent,
                age_display=current.age_display,
                elapsed=current.elapsed,
                retry_count=current.retry_count,
                blocked_reason=current.blocked_reason,
                depends_on_count=current.depends_on_count,
                owned_files_count=current.owned_files_count,
                estimated_cost_usd=current.estimated_cost_usd,
                verification_count=current.verification_count,
                flagged_unverified=current.flagged_unverified,
            )
            if current is not None
            else None
        )
        self.query_one(_TASK_CONTEXT_SELECTOR, TaskContextPanel).set_task(summary)

    # -- TUI-011: theme cycling -----------------------------------------------

    def _apply_theme(self) -> None:
        """Apply the current theme mode to the app.

        Uses Textual's built-in ``dark`` toggle for dark/light mode.
        High-contrast mode additionally adds a CSS class that styles.tcss
        can target.
        """
        from bernstein.tui.themes import ThemeMode as _TM

        resolved = self._theme_mode
        if resolved == _TM.AUTO:
            from bernstein.tui.themes import detect_terminal_theme

            resolved = detect_terminal_theme()

        # Textual built-in dark/light toggle
        self.dark = resolved != _TM.LIGHT

        # High-contrast CSS class
        if resolved == _TM.HIGH_CONTRAST:
            self.add_class("high-contrast")
        else:
            self.remove_class("high-contrast")

        try:
            self.refresh_css()
        except Exception:
            logger.debug("Could not refresh CSS after theme change", exc_info=True)

    def action_cycle_theme(self) -> None:
        """Cycle through dark → light → high-contrast → dark themes."""
        self._theme_mode = cycle_theme(self._theme_mode)
        save_theme_config(self._theme_mode)
        self._apply_theme()
        self._remember_toast(self._toasts.add(f"Theme: {self._theme_mode.value}", level=ToastLevel.INFO))

    # -- TUI-008: split-pane --------------------------------------------------

    def action_toggle_split_pane(self) -> None:
        """Toggle split-pane layout (task list left, agent log right)."""
        self._split.toggle()
        self._apply_split_layout()
        self._persist_layout()
        self.query_one("#left-pane").focus()

    def _apply_layout_preset(self, preset: str) -> None:
        """Apply a named layout preset and persist it."""
        self._layout = self._layout.apply_preset(preset)
        self._split.set_ratio(self._layout.split_ratio)
        if not self._split.enabled:
            self._split.toggle()
        for panel_id in (
            "task-timeline",
            "waterfall-view",
            "scratchpad-viewer",
            "coordinator-dashboard",
            "approval-panel",
            "tool-observer",
            "task-context",
            "runtime-health",
            "notification-center",
            "session-recorder",
        ):
            self.query_one(f"#{panel_id}").display = panel_id in self._layout.visible_panels
        self._apply_split_layout()
        self._persist_layout()

    def action_layout_focus(self) -> None:
        """Apply the compact focus layout preset."""
        self._apply_layout_preset("focus")

    def action_layout_balanced(self) -> None:
        """Apply the balanced layout preset."""
        self._apply_layout_preset("balanced")

    def action_layout_observability(self) -> None:
        """Apply the observability-heavy layout preset."""
        self._apply_layout_preset("observability")

    # -- TUI-009: toast ticker ------------------------------------------------

    def _tick_toasts(self) -> None:
        """Prune expired toasts and refresh the overlay."""
        pruned = self._toasts.prune()
        if pruned or self._toasts.count > 0:
            self.query_one(_TOAST_OVERLAY_SELECTOR, _ToastOverlay).refresh()

    def action_dismiss_toasts(self) -> None:
        """Dismiss all active toast notifications."""
        self._toasts.dismiss_all()
        self.query_one(_TOAST_OVERLAY_SELECTOR, _ToastOverlay).refresh()

    def action_acknowledge_notifications(self) -> None:
        """Mark all notification-center entries as read."""
        self._notifications.mark_all_read()
        self._refresh_notification_center()

    def _ensure_panel_visible(self, panel_id: str) -> None:
        """Force a panel visible and persist the updated layout."""
        if panel_id not in self._layout.visible_panels:
            self._layout = self._layout.toggle_panel(panel_id)
            self.query_one(f"#{panel_id}").display = True
            self._persist_layout()

    def action_toggle_session_recording(self) -> None:
        """Start or stop TUI session recording."""
        self._ensure_panel_visible("session-recorder")
        if self._session_recorder is not None and self._session_recorder.recording:
            self._session_recorder.stop()
            stopped_path = self._active_recording_path
            self._session_recorder = None
            self._active_recording_path = None
            if stopped_path is not None:
                self._remember_toast(
                    self._toasts.add(
                        f"Recording saved: {stopped_path.stem}",
                        level=ToastLevel.SUCCESS,
                        source=str(stopped_path),
                    )
                )
            self._refresh_session_recorder_panel()
            return

        self._recordings_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        path = self._recordings_dir / f"session-{timestamp}.jsonl"
        self._session_recorder = SessionRecorder(path)
        self._session_recorder.start()
        self._active_recording_path = path
        self._recording_started_at = time.time()
        self._remember_toast(
            self._toasts.add(
                f"Recording started: {path.stem}",
                level=ToastLevel.INFO,
                source=str(path),
            )
        )
        self._refresh_session_recorder_panel()

    def action_replay_latest_recording(self) -> None:
        """Replay the most recent saved TUI session recording."""
        recordings = self._current_recordings()
        if not recordings:
            self._remember_toast(self._toasts.error("No TUI recordings available yet."))
            return
        self._start_replay(recordings[0].path)

    def _current_recordings(self) -> list[RecordingSummary]:
        """Return recent recording summaries from disk."""
        return list_recordings(self._recordings_dir)

    def _start_replay(self, recording_path: Path) -> None:
        """Load a saved recording and begin playback preview in the side panel."""
        self._ensure_panel_visible("session-recorder")
        self._stop_replay()
        self._replay_frames = SessionPlayer(recording_path).load_frames()
        if not self._replay_frames:
            self._remember_toast(self._toasts.error(f"Recording is empty: {recording_path.stem}"))
            return
        self._selected_replay_path = recording_path
        self._replay_index = 0
        self._refresh_session_recorder_panel()
        self._replay_timer = self.set_interval(0.5, self._advance_replay)

    def _advance_replay(self) -> None:
        """Advance the playback preview to the next recorded frame."""
        if not self._replay_frames:
            self._stop_replay()
            return
        self._replay_index += 1
        if self._replay_index >= len(self._replay_frames):
            self._stop_replay()
            return
        self._refresh_session_recorder_panel()

    def _stop_replay(self) -> None:
        """Stop the active playback preview timer, if one exists."""
        if self._replay_timer is not None:
            self._replay_timer.stop()  # type: ignore[union-attr]
            self._replay_timer = None

    def _refresh_session_recorder_panel(self) -> None:
        """Refresh the recorder panel with current recording and playback state."""
        recordings = list_recordings(self._recordings_dir)
        playback_frame = self._replay_frames[self._replay_index] if self._replay_frames else None
        self.query_one("#session-recorder", SessionRecorderPanel).set_snapshot(
            recording_active=bool(self._session_recorder and self._session_recorder.recording),
            active_recording=self._active_recording_path,
            recordings=recordings,
            selected_recording=self._selected_replay_path,
            playback_frame=playback_frame,
        )

    def action_toggle_timeline(self) -> None:
        """Show/hide the task execution timeline."""
        timeline = self.query_one(_TASK_TIMELINE_SELECTOR, TaskTimeline)
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
                    lane=(
                        f"{self._task_lookup[e['task_id']].role}:{e['task_id'][:4]}"
                        if e["task_id"] in self._task_lookup
                        else e["task_id"][:8]
                    ),
                )
                for e in data.get("entries", [])
            ]
            self.query_one(_TASK_TIMELINE_SELECTOR, TaskTimeline).update_data(entries)

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
        scratchpad = self.query_one(_SCRATCHPAD_VIEWER_SELECTOR, ScratchpadViewer)
        scratchpad.display = not scratchpad.display
        if scratchpad.display:
            self.run_worker(self._refresh_scratchpad())
            scratchpad.focus()

    async def _refresh_scratchpad(self) -> None:
        """Fetch scratchpad entries and update widget."""
        from bernstein.tui.widgets import list_scratchpad_files

        entries = list_scratchpad_files()
        self.query_one(_SCRATCHPAD_VIEWER_SELECTOR, ScratchpadViewer).refresh_entries(entries)

    def action_scratchpad_filter(self) -> None:
        """Open scratchpad filter input."""
        # Toggle scratchpad if not visible
        scratchpad = self.query_one(_SCRATCHPAD_VIEWER_SELECTOR, ScratchpadViewer)
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
            scratchpad = self.query_one(_SCRATCHPAD_VIEWER_SELECTOR, ScratchpadViewer)
            scratchpad.set_filter(query)
            event.input.remove()
            scratchpad.focus()

    def on_input_changed(self, event: Any) -> None:
        """Handle live task-search updates."""
        from textual.widgets import Input

        if isinstance(event, Input.Changed) and event.input.id == "task-search":
            self._search_query = event.value.strip()
            self._apply_task_filter()

    def action_focus_task_search(self) -> None:
        """Focus the task search input."""
        self.query_one("#task-search", TaskSearchInput).focus()

    def on_data_table_row_selected(self, event: Any) -> None:
        """Sync the selected task into the side context pane."""
        if getattr(event.data_table, "id", "") != "task-list":
            return
        row_key = str(getattr(event, "row_key", "") or "")
        if not row_key and getattr(event, "cursor_row", None) is not None:
            row_key = str(getattr(event.cursor_row, "key", "") or "")
        if row_key:
            self._selected_task_id = row_key
            self._sync_task_context()

    def action_toggle_coordinator(self) -> None:
        """Show/hide the coordinator mode dashboard."""
        dashboard = self.query_one(_COORDINATOR_DASHBOARD_SELECTOR, CoordinatorDashboard)
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
                    task_id=cast(_CAST_DICT_STR_ANY, item)["task_id"],
                    task_title=cast(_CAST_DICT_STR_ANY, item).get("task_title", ""),
                    session_id=cast(_CAST_DICT_STR_ANY, item).get("session_id", ""),
                    diff_preview=cast(_CAST_DICT_STR_ANY, item).get("diff", ""),
                    test_summary=cast(_CAST_DICT_STR_ANY, item).get("test_summary", ""),
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
        self.query_one(_COORDINATOR_DASHBOARD_SELECTOR, CoordinatorDashboard).refresh_data(rows)

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

        self.push_screen(
            HelpScreen(
                recent_actions=self._recent_palette_actions,
                visible_panels=sorted(self._layout.visible_panels),
            )
        )

    def action_command_palette(self) -> None:
        """Open the command palette with dynamic jump/filter actions."""
        palette = CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))
        palette.register_many(
            [
                PaletteCommand(
                    "Filter blocked tasks",
                    "set_search_query:status:blocked",
                    "Show only blocked tasks",
                    category="filter",
                ),
                PaletteCommand(
                    "Filter failed tasks",
                    "set_search_query:status:failed",
                    "Show only failed tasks",
                    category="filter",
                ),
                PaletteCommand(
                    "Filter critical tasks",
                    "set_search_query:priority:1",
                    "Show only priority 1 tasks",
                    category="filter",
                ),
                PaletteCommand(
                    "Clear task filter",
                    "set_search_query:",
                    "Reset the task filter",
                    category="filter",
                ),
                PaletteCommand(
                    "Focus layout preset",
                    "layout_focus",
                    "Compact task-first layout",
                    category="layout",
                ),
                PaletteCommand(
                    "Balanced layout preset",
                    "layout_balanced",
                    "Task list plus context",
                    category="layout",
                ),
                PaletteCommand(
                    "Observability layout preset",
                    "layout_observability",
                    "Surface timeline, approvals, and tool observer",
                    category="layout",
                ),
                PaletteCommand(
                    "Mark notifications read",
                    "acknowledge_notifications",
                    "Clear unread state in notification center",
                    category="notifications",
                ),
                PaletteCommand(
                    "Toggle session recording",
                    "toggle_session_recording",
                    "Start or stop TUI session recording",
                    category="recording",
                ),
                PaletteCommand(
                    "Replay latest recording",
                    "replay_latest_recording",
                    "Preview the most recent recorded TUI session",
                    category="recording",
                ),
            ]
        )
        for recording in self._current_recordings():
            palette.register(
                PaletteCommand(
                    name=f"Replay {recording.path.stem}",
                    action=f"replay_recording:{recording.path}",
                    description=f"{recording.frame_count} frames · {recording.duration_s:.1f}s",
                    category="recording",
                )
            )
        for row in self._all_rows[:40]:
            palette.register(
                PaletteCommand(
                    name=f"Jump to {row.task_id} {row.title[:36]}",
                    action=f"select_task:{row.task_id}",
                    description=f"{row.status} · {row.role}",
                    category="jump",
                )
            )
        self._palette_action_labels = {command.action: command.name for command in palette.commands}
        self.push_screen(CommandPaletteScreen(palette), callback=self._handle_palette_result)

    def _handle_palette_result(self, result: str | None) -> None:
        """Execute a command palette result."""
        if not result:
            return
        label = self._palette_action_labels.get(result)
        if label:
            self._recent_palette_actions = [*self._recent_palette_actions[-4:], label]
        if result.startswith("set_search_query:"):
            query = result.split(":", 1)[1]
            self._search_query = query
            search = self.query_one("#task-search", TaskSearchInput)
            search.value = query
            self._apply_task_filter()
            return
        if result.startswith("select_task:"):
            task_id = result.split(":", 1)[1]
            self._selected_task_id = task_id
            self._sync_task_context()
            return
        if result.startswith("replay_recording:"):
            recording_path = Path(result.split(":", 1)[1])
            self._start_replay(recording_path)
            return
        action = getattr(self, f"action_{result}", None)
        if callable(action):
            action()

    def _remember_toast(self, toast: Toast) -> None:
        """Mirror a toast into persistent notification history and refresh the UI."""
        self._notifications.add(
            toast.message,
            level=toast.level.value,
            source=toast.source,
            timestamp=toast.timestamp,
        )
        self._refresh_notification_center()
        self.query_one(_TOAST_OVERLAY_SELECTOR, _ToastOverlay).refresh()

    def _refresh_notification_center(self) -> None:
        """Render the latest notification history into the side panel."""
        panel = self.query_one("#notification-center", NotificationCenterPanel)
        panel.set_history(self._notifications.get_history(limit=5), self._notifications.get_unread_count())

    @staticmethod
    def _count_active_agents() -> int:
        """Count active agents recorded in the local runtime snapshot.

        Returns:
            Number of active agent entries with a PID in `.sdd/runtime/agents.json`.
        """
        agents_file = Path(_AGENTS_JSON_PATH)
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
    agents_file = Path(_AGENTS_JSON_PATH)
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

    agents_file = Path(_AGENTS_JSON_PATH)
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
