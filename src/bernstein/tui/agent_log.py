"""Log and quality gate widgets for the Bernstein TUI."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from rich.text import Text
from textual.widgets import DataTable, RichLog, Static

# ---------------------------------------------------------------------------
# Agent log widget
# ---------------------------------------------------------------------------


class AgentLogWidget(RichLog):
    """Scrollable log output for agent activity with timestamps.

    Tracks a session start timestamp so that historical log lines loaded
    at startup can be visually separated from new activity.  Historical
    entries are rendered dimmed; a horizontal rule marks the boundary.
    """

    # Width (in characters) of the separator rule.
    _SEPARATOR_WIDTH: ClassVar[int] = 60

    def __init__(self, **kwargs: Any) -> None:
        """Initialise the log widget and record the session start time.

        Args:
            **kwargs: Forwarded to :class:`~textual.widgets.RichLog`.
        """
        super().__init__(**kwargs)
        self._session_start_ts: float = time.time()
        self._separator_written: bool = False
        self._has_historical: bool = False

    def _write_separator(self) -> None:
        """Insert a visual separator between historical and live log entries."""
        if self._separator_written:
            return
        self._separator_written = True
        ts_label = datetime.fromtimestamp(self._session_start_ts).strftime("%H:%M:%S")
        rule_char = "\u2500"
        label = f" Session started ({ts_label}) "
        side_len = max(1, (self._SEPARATOR_WIDTH - len(label)) // 2)
        rule_line = rule_char * side_len + label + rule_char * side_len
        self.write(Text.from_markup(f"[bold cyan]{rule_line}[/bold cyan]"))

    def load_historical_lines(self, lines: list[str]) -> None:
        """Load pre-existing log lines rendered in a dim style.

        Call this once at startup before any :meth:`append_line` calls.
        A session separator is written after the historical entries.

        Args:
            lines: Raw log lines (already formatted/timestamped by the
                source file).  Empty or whitespace-only lines are skipped.
        """
        for raw_line in lines:
            stripped = raw_line.rstrip()
            if not stripped:
                continue
            self._has_historical = True
            self.write(Text.from_markup(f"[dim]{stripped}[/dim]"))
        if self._has_historical:
            self._write_separator()

    def append_line(self, line: str) -> None:
        """Append a timestamped line to the log.

        If this is the first live line and no historical lines were loaded,
        the session separator is written first so the user always sees the
        boundary.

        Args:
            line: Text line to append (timestamp is prepended automatically).
        """
        if not self._separator_written:
            self._write_separator()
        ts = datetime.now().strftime("%H:%M:%S")
        self.write(Text.from_markup(f"[dim]{ts}[/dim] {line}"))


class ShortcutsFooter(Static):
    """Single-line footer bar showing keyboard shortcuts."""

    _SHORTCUTS = (
        "\u2191\u2192 navigate",
        "Enter detail",
        "x cancel",
        "p prioritize",
        "n notifications",
        "R record",
        "t retry",
        "k kill",
        "s spawn",
        "c scratchpad",
        "r refresh",
        "S hard-stop",
        "q quit",
    )

    def on_mount(self) -> None:
        """Render shortcut hints on mount."""
        self._render()

    def _render(self) -> None:
        parts = "  [dim]\u2502[/dim]  ".join(
            f"[bold]{hint.split()[0]}[/bold] [dim]{' '.join(hint.split()[1:])}[/dim]" for hint in self._SHORTCUTS
        )
        self.update(Text.from_markup(f"  {parts}  "))


# ---------------------------------------------------------------------------
# Quality gate panel
# ---------------------------------------------------------------------------


@dataclass
class QualityGateResult:
    """Single quality gate result for display."""

    gate: str
    status: str  # "pass", "fail", "warn", "skipped"
    duration_ms: float
    details: str


class QualityGatePanel(DataTable):
    """DataTable widget showing quality gate results with pass/fail badges.

    Columns: Gate | Status | Duration | Details
    Status cell: green "✓ PASS" or red "✗ FAIL" rich markup.
    """

    DEFAULT_CSS = """
    QualityGatePanel {
        height: auto;
        max-height: 40%;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._setup_columns()

    def _setup_columns(self) -> None:
        """Initialize table columns."""
        self.add_columns("Gate", "Status", "Duration", "Details")

    def set_results(self, results: list[QualityGateResult]) -> None:
        """Populate the panel with quality gate results.

        Args:
            results: List of QualityGateResult instances.
        """
        self.clear()
        for result in results:
            # Format status with pass/fail badge
            match result.status:
                case "pass":
                    status_markup = "[green]\u2713 PASS[/green]"
                case "fail":
                    status_markup = "[red]\u2717 FAIL[/red]"
                case "warn":
                    status_markup = "[yellow]\u26a0 WARN[/yellow]"
                case _:
                    status_markup = f"[dim]{result.status.upper()}[/dim]"

            # Format duration
            duration_str = f"{result.duration_ms:.0f}ms"

            self.add_row(
                result.gate,
                status_markup,
                duration_str,
                result.details[:50] + "..." if len(result.details) > 50 else result.details,
            )


# ---------------------------------------------------------------------------
# Color-coded agent identity in all output (T562)
# ---------------------------------------------------------------------------

# Agent role colors for TUI widgets
AGENT_ROLE_COLORS_TUI: dict[str, str] = {
    "manager": "cyan",
    "backend": "green",
    "frontend": "yellow",
    "qa": "magenta",
    "security": "red",
    "architect": "blue",
    "devops": "white",
    "docs": "dim",
    "reviewer": "magenta",
    "ml-engineer": "cyan",
    "prompt-engineer": "yellow",
    "retrieval": "green",
    "vp": "white",
    "analyst": "blue",
    "resolver": "red",
    "visionary": "magenta",
}


def get_agent_role_color(role: str) -> str:
    """Get color for agent role in TUI (T562)."""
    return AGENT_ROLE_COLORS_TUI.get(role, "dim")


def format_agent_label_text(role: str, session_id: str) -> Text:
    """Format color-coded agent label for TUI as Text object (T562)."""
    color = get_agent_role_color(role)
    return Text(f"{role}:{session_id[:8]}", style=color)


# ---------------------------------------------------------------------------
# Compaction event indicators (T563)
# ---------------------------------------------------------------------------


def render_compaction_marker(timestamp: float, duration: float = 0.0) -> str:
    """Render a compaction event marker for the timeline (T563)."""
    time_str = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
    if duration > 0:
        return f"\u26a1 Compaction at {time_str} ({duration:.1f}s)"
    return f"\u26a1 Compaction at {time_str}"
