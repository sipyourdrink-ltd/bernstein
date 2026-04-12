"""Approval, waterfall, tool observer, and SLO widgets for the Bernstein TUI."""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual.containers import Container, Vertical
from textual.widgets import DataTable, Label, Static

from bernstein.tui.task_list import generate_sparkline

#: CSS selector for the approval details label inside ApprovalPanel.
_APPROVAL_DETAILS_SELECTOR = "#approval-details"

# ---------------------------------------------------------------------------
# ApprovalPanel — interactive permission approval widget
# ---------------------------------------------------------------------------


@dataclass
class ApprovalEntry:
    """One pending approval request."""

    task_id: str
    task_title: str
    session_id: str
    diff_preview: str
    test_summary: str


class ApprovalPanel(Static):
    """Interactive panel showing pending approval requests.

    Operators can select a pending request, view diff/test details,
    and approve or deny it via the server API.
    """

    DEFAULT_CSS = """
    ApprovalPanel {
        height: 1fr;
        border: tall $primary 50%;
        content-align: center middle;
    }
    .approval-info {
        margin-left: 1;
    }
    .approval-empty {
        color: $text-muted 50%;
        text-align: center;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialise the panel.

        Args:
            *args: Static args.
            **kwargs: Static kwargs.
        """
        super().__init__(*args, **kwargs)
        self._pending: list[ApprovalEntry] = []
        self._selected_index: int = -1

    def on_mount(self) -> None:
        """Build the layout with left-side list and right-side details."""
        self._build_layout()

    def _build_layout(self) -> None:
        """Create split layout (list + details)."""
        # Two-pane layout: list on left, details on right
        container = Container(id="approval-container")
        self.mount(container)

        list_view = DataTable(id="approval-list")
        details_view = Vertical(Label("", id="approval-details"), id="approval-details-pane")

        container.mount(list_view, details_view)

        list_view.add_columns("Task", "Title")
        list_view.cursor_type = "row"
        list_view.zebra_stripes = True

    def refresh_entries(self, entries: list[ApprovalEntry]) -> None:
        """Populate the panel with new pending approvals."""
        self._pending = entries
        self._selected_index = -1
        table = self.query_one("#approval-list", DataTable)
        table.clear()
        for entry in entries:
            table.add_row(
                Text(entry.task_id, style="cyan"),
                Text(entry.title[:60] if hasattr(entry, "title") else entry.task_title[:60], style="dim"),
                key=entry.task_id,
            )
        if not entries:
            self.query_one(_APPROVAL_DETAILS_SELECTOR, Label).update("No pending approvals.")
            self.query_one(_APPROVAL_DETAILS_SELECTOR, Label).add_class("approval-empty")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection to show details."""
        if event.data_table.id == "approval-list":
            key = str(event.cursor_row.key) if event.cursor_row else ""
            try:
                idx = next(i for i, e in enumerate(self._pending) if e.task_id == key)
                self._selected_index = idx
                entry = self._pending[idx]
                details_label = self.query_one(_APPROVAL_DETAILS_SELECTOR, Label)
                details = (
                    f"\n[b]Task:[/b] {entry.task_id}\n"
                    f"[b]Title:[/b] {entry.task_title}\n"
                    f"[b]Session:[/b] {entry.session_id}\n"
                    f"[b]Tests:[/b] {entry.test_summary}\n"
                    f"\n[b]Diff preview (first 500 chars):[/b]\n"
                    f"[dim]{entry.diff_preview[:500]}[/dim]"
                    if entry.diff_preview
                    else ""
                )
                details_label.update(details)
                details_label.remove_class("approval-empty")
            except StopIteration:
                pass  # No approval entry selected; nothing to display

    async def action_approve(self) -> None:
        """Approve the currently selected pending task."""
        if self._selected_index < 0:
            return
        entry = self._pending[self._selected_index]
        self.app.post_message(ApprovalAction(approved=True, task_id=entry.task_id, reason="Approved via TUI"))

    async def action_reject(self) -> None:
        """Reject the currently selected pending task."""
        if self._selected_index < 0:
            return
        entry = self._pending[self._selected_index]
        self.app.post_message(ApprovalAction(approved=False, task_id=entry.task_id, reason="Rejected via TUI"))

    def on_approval_action(self, event: ApprovalAction) -> None:
        """Handle approval action from self or parent."""
        self.notify("Approval sent: " + ("approved" if event.approved else "rejected"))


@dataclass
class ApprovalAction:
    """Message posted when user approves or rejects a pending task."""

    approved: bool
    task_id: str
    reason: str = ""


# ---------------------------------------------------------------------------
# Waterfall trace view (T412)
# ---------------------------------------------------------------------------

#: Step-type display labels used in waterfall rows.
_WATERFALL_TYPE_LABELS: dict[str, str] = {
    "spawn": "spawn",
    "orient": "read",
    "plan": "plan",
    "edit": "write",
    "verify": "exec",
    "complete": "done",
    "fail": "fail",
    "compact": "cmpct",
}

#: Rich colour names for each step type in the waterfall.
_WATERFALL_TYPE_COLORS: dict[str, str] = {
    "spawn": "cyan",
    "orient": "blue",
    "plan": "dim",
    "edit": "green",
    "verify": "yellow",
    "complete": "bright_green",
    "fail": "red",
    "compact": "magenta",
}


def _waterfall_type_label(step_type: str) -> str:
    return _WATERFALL_TYPE_LABELS.get(step_type, step_type[:5])


def _waterfall_type_color(step_type: str) -> str:
    return _WATERFALL_TYPE_COLORS.get(step_type, "white")


def _unique_step_types(batch: Any) -> list[str]:
    """Return deduplicated step types in order of first appearance."""
    seen: list[str] = []
    for step in batch.steps:
        if step.type not in seen:
            seen.append(step.type)
    return seen


def _waterfall_batch_label(batch: Any) -> tuple[str, str]:
    """Build the label column text and style for a batch row."""
    is_abort = bool(batch.abort_reason)
    label = f"B{batch.batch_id:<2}"
    label += " \u21c9" if batch.is_concurrent else "  "
    return f"{label:<6}", ("bold" if is_abort else "dim")


def _waterfall_timing_bar(
    batch: Any,
    total_start: float,
    duration: float,
    bar_width: int,
    seen_types: list[str],
    row_color: str | None,
) -> tuple[str, str, str, str]:
    """Compute leading spaces, bar chars, bar style, and duration text."""
    start_off = int(((batch.start_ts - total_start) / duration) * bar_width)
    bar_len = max(1, int(((batch.end_ts - batch.start_ts) / duration) * bar_width))
    start_off = max(0, min(start_off, bar_width - 1))
    bar_len = min(bar_len, bar_width - start_off)

    bar_char = "\u2593" if batch.is_concurrent else "\u2588"
    bar_style = row_color or _waterfall_type_color(seen_types[0] if seen_types else "plan")

    batch_dur_ms = int((batch.end_ts - batch.start_ts) * 1000)
    dur_str = f" {batch_dur_ms / 1000:.1f}s" if batch_dur_ms >= 1000 else f" {batch_dur_ms}ms"

    return " " * start_off, bar_char * bar_len, bar_style, dur_str


def render_waterfall_batches(
    batches: list[Any],
    *,
    bar_width: int = 48,
    label_width: int = 18,
) -> Text:
    """Render waterfall tool batches as a Rich Text object (T412).

    Each batch occupies one or more rows (one per step type in concurrent
    batches).  Timing bars are drawn proportional to the total trace
    duration.  Abort batches are highlighted in red with a back-reference
    to the triggering batch.

    Args:
        batches: List of ToolBatch objects (from
            ``group_trace_steps_into_batches``).
        bar_width: Characters available for the horizontal timing bar.
        label_width: Characters reserved for the left-hand label column.

    Returns:
        Rich Text suitable for rendering in a Textual ``Static`` widget.
    """
    text = Text()
    if not batches:
        text.append("No trace batches to display.", style="dim")
        return text

    total_start = min(b.start_ts for b in batches)
    total_end = max(b.end_ts for b in batches)
    duration = max(total_end - total_start, 1.0)

    for batch in batches:
        is_abort = bool(batch.abort_reason)
        row_color = "red" if is_abort else None

        label_text, label_style = _waterfall_batch_label(batch)
        text.append(label_text, style=label_style)

        seen_types = _unique_step_types(batch)
        type_str = f"[{','.join(_waterfall_type_label(t) for t in seen_types)}]"
        text.append(f"{type_str:<{label_width}}", style=row_color or "cyan")

        leading, bar_chars, bar_style, dur_str = _waterfall_timing_bar(
            batch,
            total_start,
            duration,
            bar_width,
            seen_types,
            row_color,
        )
        text.append(leading)
        text.append(bar_chars, style=bar_style)
        text.append(dur_str, style="dim")
        text.append("\n")

        if is_abort:
            indent = " " * 8
            reason_short = batch.abort_reason[:60] + ("\u2026" if len(batch.abort_reason) > 60 else "")
            trig = f" \u2190 triggered by B{batch.triggering_batch_id}" if batch.triggering_batch_id is not None else ""
            text.append(f"{indent}\u2717 {reason_short}{trig}\n", style="red")

    return text


class WaterfallWidget(Static):
    """Waterfall trace view showing tool batches and timing (T412).

    Renders serial and concurrent tool batches as horizontal timing bars.
    Abort/fail batches are highlighted in red and linked back to the
    batch that triggered the early exit.

    Usage::

        widget = WaterfallWidget(id="waterfall")
        widget.update_batches(group_trace_steps_into_batches(trace.steps))
    """

    DEFAULT_CSS = """
    WaterfallWidget {
        height: auto;
        max-height: 60%;
        border: tall $primary 30%;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._batches: list[Any] = []

    def update_batches(self, batches: list[Any]) -> None:
        """Replace the current batch list and refresh the display.

        Args:
            batches: Ordered list of ToolBatch objects.
        """
        self._batches = batches
        self.refresh()

    def render(self) -> Text:
        """Render all batches as a Rich waterfall diagram."""
        width = max(self.size.width - 4, 20)
        bar_w = max(width - 30, 20)
        return render_waterfall_batches(self._batches, bar_width=bar_w)


# ---------------------------------------------------------------------------
# Live tool execution observer (T405)
# ---------------------------------------------------------------------------


@dataclass
class ToolObserverEntry:
    """A single completed tool call event for the live observer.

    Attributes:
        tool_name: Name of the tool that was called.
        session_id: Agent session that invoked the tool.
        total_ms: Wall-clock execution time in milliseconds.
        timestamp: Unix epoch when the call completed.
        status: Completion status — always ``"done"`` for JSONL-sourced records.
    """

    tool_name: str
    session_id: str
    total_ms: float
    timestamp: float
    status: str = field(default="done")


def read_new_tool_calls(
    jsonl_path: Path,
    file_pos: int = 0,
    max_records: int = 50,
) -> tuple[list[ToolObserverEntry], int]:
    """Read new tool call records from a JSONL file since *file_pos*.

    Performs an incremental read by seeking to *file_pos* and reading only
    the bytes written since the last poll, so repeated calls are O(new data).

    Args:
        jsonl_path: Path to ``tool_timing.jsonl``.
        file_pos: Byte offset from the last successful read (0 on first call).
        max_records: Cap on how many new entries to return in a single call.

    Returns:
        A tuple of ``(new_entries, new_file_position)``.  When the file does
        not exist or is unreadable, returns ``([], file_pos)`` unchanged.
    """
    if not jsonl_path.exists():
        return [], file_pos

    entries: list[ToolObserverEntry] = []
    new_pos = file_pos

    try:
        with jsonl_path.open("rb") as f:
            f.seek(file_pos)
            for raw_line in f:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    data: dict[str, Any] = json.loads(line)
                    entries.append(
                        ToolObserverEntry(
                            tool_name=str(data["tool_name"]),
                            session_id=str(data.get("session_id", "")),
                            total_ms=float(data.get("total_ms", 0.0)),
                            timestamp=float(data.get("timestamp", 0.0)),
                        )
                    )
                except (KeyError, ValueError, TypeError):
                    continue
            new_pos = f.tell()
    except OSError:
        return [], file_pos

    # Trim to cap so a single burst of records doesn't flood the buffer.
    if len(entries) > max_records:
        entries = entries[-max_records:]

    return entries, new_pos


def render_tool_observer(
    entries: deque[ToolObserverEntry],
    now: float | None = None,
) -> Text:
    """Render tool observer ring-buffer contents as Rich Text.

    Entries are shown newest-first with tool name, duration, abbreviated
    session ID, and age since completion.

    Args:
        entries: Ring buffer of :class:`ToolObserverEntry` (most recent last).
        now: Reference timestamp for age computation; defaults to ``time.time()``.

    Returns:
        Rich :class:`~rich.text.Text` ready for a Textual ``Static`` widget.
    """
    if now is None:
        now = time.time()

    text = Text()
    if not entries:
        text.append("Waiting for tool calls\u2026", style="dim")
        return text

    # Header row
    text.append(f"{'Tool':<22}{'Duration':>9}  {'Session':<10}  Age\n", style="bold dim")

    for entry in reversed(entries):  # newest first
        age_s = max(0.0, now - entry.timestamp)
        age_str = f"{age_s:.0f}s" if age_s < 60 else f"{age_s / 60:.0f}m"

        dur_ms = entry.total_ms
        dur_str = f"{dur_ms / 1000:.2f}s" if dur_ms >= 1000 else f"{dur_ms:.0f}ms"

        sess_short = entry.session_id[:10] if len(entry.session_id) > 10 else entry.session_id

        # Color by duration thresholds
        if dur_ms < 500:
            dur_color = "green"
        elif dur_ms < 3000:
            dur_color = "yellow"
        else:
            dur_color = "red"

        text.append("\u2713 ", style="green")
        text.append(f"{entry.tool_name:<20}", style="cyan")
        text.append(f"{dur_str:>9}", style=dur_color)
        text.append(f"  {sess_short:<10}", style="dim")
        text.append(f"  {age_str}", style="dim")
        text.append("\n")

    return text


class ToolObserverWidget(Static):
    """Live tool execution observer widget (T405).

    Polls ``tool_timing.jsonl`` incrementally on each refresh and maintains
    a ring buffer of the last :attr:`MAX_RECORDS` completed tool calls.
    Display shows tool name, wall-clock duration (colour-coded by speed),
    abbreviated session ID, and age since completion.

    Usage::

        widget = ToolObserverWidget(id="tool-observer")
        # Call refresh_from_jsonl() periodically to pull new records.
        widget.refresh_from_jsonl()
    """

    DEFAULT_CSS = """
    ToolObserverWidget {
        height: auto;
        max-height: 60%;
        border: tall $primary 30%;
        padding: 0 1;
    }
    """

    MAX_RECORDS: ClassVar[int] = 50

    def __init__(self, **kwargs: Any) -> None:
        """Initialise the observer.

        Args:
            **kwargs: Forwarded to :class:`~textual.widgets.Static`.
        """
        super().__init__(**kwargs)
        self._ring: deque[ToolObserverEntry] = deque(maxlen=self.MAX_RECORDS)
        self._file_pos: int = 0
        self._jsonl_path: Path = Path.cwd() / ".sdd" / "metrics" / "tool_timing.jsonl"

    def refresh_from_jsonl(self, jsonl_path: Path | None = None) -> None:
        """Pull new records from JSONL into the ring buffer and repaint.

        This is O(new bytes) — safe to call on every poll cycle even under
        high tool churn, because only newly appended lines are read.

        Args:
            jsonl_path: Override path to ``tool_timing.jsonl``; uses the
                default ``.sdd/metrics/tool_timing.jsonl`` when ``None``.
        """
        path = jsonl_path or self._jsonl_path
        new_entries, new_pos = read_new_tool_calls(path, self._file_pos, self.MAX_RECORDS)
        self._file_pos = new_pos
        self._ring.extend(new_entries)
        self.refresh()

    def render(self) -> Text:
        """Render the current ring buffer as a Rich text table."""
        return render_tool_observer(self._ring)


# ---------------------------------------------------------------------------
# SLO burn-down rate widget (OBS-150)
# ---------------------------------------------------------------------------


def build_slo_burndown_text(burndown: dict[str, object]) -> Text:
    """Render SLO burn-down data as a Rich Text block for TUI display.

    Shows:
    - Current SLO compliance % vs. target
    - Error budget consumption with a progress bar
    - Burn rate sparkline over recent history
    - Days-to-breach projection in colour-coded text

    Args:
        burndown: Dict as returned by GET /slo/burndown.

    Returns:
        Rich Text object ready for rendering.
    """
    text = Text()

    slo_target = float(burndown.get("slo_target", 0.9))
    slo_current = float(burndown.get("slo_current", 0.0))
    burn_rate = float(burndown.get("burn_rate", 0.0))
    budget_fraction = float(burndown.get("budget_fraction", 1.0))
    budget_consumed_pct = float(burndown.get("budget_consumed_pct", 0.0))
    breach_projection = str(burndown.get("breach_projection", ""))
    status = str(burndown.get("status", "green"))
    total_tasks = int(burndown.get("total_tasks", 0))  # type: ignore[arg-type]

    # Status indicator
    status_color = {"green": "green", "yellow": "yellow", "red": "red"}.get(status, "white")
    status_dot = {"green": "\u25cf", "yellow": "\u25c9", "red": "\u25cd"}.get(status, "\u25cb")
    text.append(f"{status_dot} SLO Burn-Down\n", style=f"bold {status_color}")

    # SLO compliance row
    slo_pct = slo_current * 100
    target_pct = slo_target * 100
    slo_color = "green" if slo_current >= slo_target else ("yellow" if slo_current >= slo_target * 0.95 else "red")
    text.append("  Compliance: ", style="dim")
    text.append(f"{slo_pct:.1f}%", style=f"bold {slo_color}")
    text.append(f" / {target_pct:.0f}% target\n", style="dim")

    # Burn rate row
    burn_color = "green" if burn_rate <= 1.0 else ("yellow" if burn_rate <= 2.0 else "red")
    text.append("  Burn rate:  ", style="dim")
    text.append(f"{burn_rate:.2f}x", style=f"bold {burn_color}")
    text.append(" (1.0 = on-target)\n", style="dim")

    # Error budget bar
    bar_width = 20
    consumed_chars = int((1.0 - budget_fraction) * bar_width)
    remaining_chars = bar_width - consumed_chars
    bar_color = "green" if budget_fraction > 0.5 else ("yellow" if budget_fraction > 0.1 else "red")
    bar = "\u2588" * consumed_chars + "\u2591" * remaining_chars
    text.append("  Budget:     ", style="dim")
    text.append(f"[{bar}]", style=bar_color)
    text.append(f" {budget_consumed_pct:.0f}% consumed\n", style="dim")

    # Sparkline of recent burn rate history
    sparkline_data = burndown.get("sparkline", [])
    if sparkline_data and isinstance(sparkline_data, list):
        burn_rates = [float(pt.get("burn_rate", 0.0)) for pt in sparkline_data]  # type: ignore[union-attr]
        if burn_rates:
            sparkline_str = generate_sparkline(burn_rates, width=20)
            text.append("  Trend:      ", style="dim")
            text.append(sparkline_str, style=burn_color)
            text.append(" (burn rate)\n", style="dim")

    # Projection / breach warning
    text.append(f"  {breach_projection}\n", style=status_color)

    # Task count footer
    text.append(f"  Tasks: {total_tasks}", style="dim")

    return text


class SLOBurnDownWidget(Static):
    """TUI widget showing SLO burn-down rate and breach projection (OBS-150).

    Displays the current SLO compliance, error budget consumption, burn rate
    sparkline, and a linear projection of when the SLO will be breached.

    Usage::

        widget = SLOBurnDownWidget()
        widget.update_from_data(burndown_dict)  # call on each poll cycle
    """

    DEFAULT_CSS = """
    SLOBurnDownWidget {
        height: auto;
        border: tall $primary 30%;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialise the burn-down widget."""
        super().__init__(**kwargs)
        self._burndown: dict[str, object] = {}

    def update_from_data(self, burndown: dict[str, object]) -> None:
        """Refresh widget state from a /slo/burndown response.

        Args:
            burndown: Response dict from GET /slo/burndown.
        """
        self._burndown = burndown
        self.refresh()

    def render(self) -> Text:
        """Render burn-down data as Rich text."""
        if not self._burndown:
            return Text("  Waiting for SLO data\u2026", style="dim")
        return build_slo_burndown_text(self._burndown)
