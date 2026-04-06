"""FastTracker II / ProTracker inspired task monitoring widget.

Displays agent activity in the visual language of a 90s MOD music tracker.
Each agent is a "channel" and events scroll like pattern data in the
Bernstein '89 retro demoscene aesthetic.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from bernstein.tui.accessibility import (
    AccessibilityConfig,
    detect_accessibility,
)

# Musical note names for task-id-to-note mapping.
NOTE_NAMES: list[str] = [
    "C",
    "C#",
    "D",
    "D#",
    "E",
    "F",
    "F#",
    "G",
    "G#",
    "A",
    "A#",
    "B",
]

# VU meter block characters, low to high.
VU_CHARS: str = "░▒▓█"

# Accessible VU replacement characters.
_VU_CHARS_ASCII: str = "-=#X"

# --- FastTracker II palette ---
_ROW_GRAY = "#666666"
_NOTE_ACTIVE = "#aaaaaa"
_NOTE_IDLE = "#444444"
_EFX_YELLOW = "#aaaa00"
_VU_LO = "#004400"
_VU_HI = "#00ff00"
_CUR_FG = "#ffff00"
_CUR_BG = "#000080"
_HEADER_FG = "#ffffff"
_HEADER_BG = "#000060"
_BORDER = "#0000aa"


def task_id_to_note(task_id: str) -> str:
    """Map a task ID to a musical note string like 'C-4'.

    Args:
        task_id: Arbitrary task identifier string.

    Returns:
        A 3-character note string, e.g. 'C-4', 'D#2'.
    """
    h = hash(task_id)
    name = NOTE_NAMES[h % 12]
    octave = h % 8
    # Pad to 3 characters: "C-4", "C#4", "D-2", etc.
    if len(name) == 1:
        return f"{name}-{octave}"
    return f"{name}{octave}"


def format_effect(
    files_changed: int = 0,
    progress_pct: float = 0.0,
    tests_passing: int = 0,
) -> str:
    """Format an effect column value from agent metrics.

    Priority: files changed > progress > tests passing.

    Args:
        files_changed: Number of files changed by the agent.
        progress_pct: Task progress as a percentage 0-100.
        tests_passing: Number of tests passing.

    Returns:
        3-character effect string like 'F08', 'V40', 'T0C', or '\u00b7\u00b7\u00b7'.
    """
    if files_changed > 0:
        return f"F{min(files_changed, 0xFF):02X}"
    if progress_pct > 0.0:
        hex_val = int(progress_pct * 255 / 100.0)
        return f"V{min(hex_val, 0xFF):02X}"
    if tests_passing > 0:
        return f"T{min(tests_passing, 0xFF):02X}"
    return "\u00b7\u00b7\u00b7"


def render_vu(level: float, width: int = 4, *, ascii_mode: bool = False) -> str:
    """Render a VU-meter bar from a normalized activity level.

    Args:
        level: Activity level in [0.0, 1.0].
        width: Maximum bar width in characters.
        ascii_mode: Use ASCII characters instead of unicode blocks.

    Returns:
        A string of ``width`` characters representing the VU level.
    """
    clamped = max(0.0, min(1.0, level))
    filled = int(clamped * width)
    chars = _VU_CHARS_ASCII if ascii_mode else VU_CHARS
    if filled == 0:
        return " " * width
    parts: list[str] = []
    for i in range(filled):
        # Gradient: low chars for early positions, high for later.
        idx = min(int((i / max(width - 1, 1)) * (len(chars) - 1)), len(chars) - 1)
        parts.append(chars[idx])
    return "".join(parts).ljust(width)


def _vu_style(pos: int, total: int) -> Style:
    """Compute a gradient style for a single VU bar character.

    Args:
        pos: Character position within the bar (0-based).
        total: Total number of filled positions.

    Returns:
        A Rich Style with the interpolated color.
    """
    if total <= 1:
        return Style(color=_VU_HI)
    frac = pos / (total - 1)
    # Linear interpolation between _VU_LO (#004400) and _VU_HI (#00ff00).
    r = 0
    g = int(0x44 + frac * (0xFF - 0x44))
    b = 0
    return Style(color=f"#{r:02x}{g:02x}{b:02x}")


@dataclass(frozen=True)
class TrackerRow:
    """One row of tracker data.

    Attributes:
        row_num: Row number 0x00-0xFF, wrapping.
        note: Musical note string, e.g. 'C-4' or '---'.
        effect: Effect column string, e.g. 'F08' or '\u00b7\u00b7\u00b7'.
        vu_level: Normalized activity level in [0.0, 1.0].
    """

    row_num: int
    note: str
    effect: str
    vu_level: float


@dataclass
class TrackerChannel:
    """One tracker channel corresponding to one agent.

    Attributes:
        agent_id: Unique agent session identifier.
        role: Agent role name (e.g. 'backend', 'qa').
        rows: Ring buffer of tracker rows.
        last_seen: Monotonic counter of last update for recency tracking.
    """

    agent_id: str
    role: str
    rows: deque[TrackerRow] = field(default_factory=lambda: deque(maxlen=32))
    last_seen: int = 0


class TrackerView(Static):
    """MOD-tracker style agent activity monitor widget.

    Displays agent channels as vertical columns with scrolling rows of
    note data, effects, and VU meters in a FastTracker II visual style.
    """

    DEFAULT_CSS = """
    TrackerView {
        height: 16;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._channels: list[TrackerChannel] = []
        self._row_counter: int = 0
        self._max_visible_rows: int = 12
        self._max_channels: int = 6
        self._update_seq: int = 0
        self._accessibility: AccessibilityConfig | None = None
        self._detect_accessibility()

    def _detect_accessibility(self) -> None:
        """Detect and cache accessibility configuration."""
        from bernstein.tui.accessibility import AccessibilityLevel

        level = detect_accessibility()
        if level != AccessibilityLevel.OFF:
            self._accessibility = AccessibilityConfig.from_level(level)

    def update_agents(self, agents: list[dict[str, Any]]) -> None:
        """Update tracker with current agent data.

        Each call appends a new row to every channel and advances the
        global row counter.

        Args:
            agents: List of agent dicts with keys:
                - session_id: str
                - role: str
                - current_task_id: str | None
                - files_changed: int
                - progress_pct: float
                - tests_passing: int
                - tokens_per_sec: float
        """
        self._update_seq += 1

        # Index existing channels by agent_id.
        chan_map: dict[str, TrackerChannel] = {ch.agent_id: ch for ch in self._channels}

        seen_ids: set[str] = set()

        for agent in agents:
            sid: str = agent.get("session_id", "")
            role: str = agent.get("role", "???")
            task_id: str | None = agent.get("current_task_id")
            files: int = agent.get("files_changed", 0)
            pct: float = agent.get("progress_pct", 0.0)
            tests: int = agent.get("tests_passing", 0)
            tps: float = agent.get("tokens_per_sec", 0.0)

            seen_ids.add(sid)

            if sid in chan_map:
                ch = chan_map[sid]
                ch.role = role
            else:
                ch = TrackerChannel(agent_id=sid, role=role)
                chan_map[sid] = ch

            ch.last_seen = self._update_seq

            note = task_id_to_note(task_id) if task_id else "---"
            effect = format_effect(files, pct, tests)
            # Normalize tokens_per_sec to 0-1 (assume 100 tps = max).
            vu = min(tps / 100.0, 1.0) if tps > 0 else 0.0

            ch.rows.append(
                TrackerRow(
                    row_num=self._row_counter & 0xFF,
                    note=note,
                    effect=effect,
                    vu_level=vu,
                )
            )

        # Append idle rows for channels not in this update.
        for sid, ch in chan_map.items():
            if sid not in seen_ids:
                ch.rows.append(
                    TrackerRow(
                        row_num=self._row_counter & 0xFF,
                        note="---",
                        effect="\u00b7\u00b7\u00b7",
                        vu_level=0.0,
                    )
                )

        # Advance row counter (wraps at 0xFF).
        self._row_counter = (self._row_counter + 1) & 0xFF

        # Keep only the most recently active channels up to max.
        all_channels = sorted(chan_map.values(), key=lambda c: c.last_seen, reverse=True)
        self._channels = all_channels[: self._max_channels]

        self.refresh()

    def render(self) -> Text:
        """Render the tracker pattern view as a Rich Text object."""
        text = Text()
        cfg = self._accessibility
        ascii_mode = bool(cfg and cfg.no_unicode)

        n_channels = min(len(self._channels), self._max_channels)
        if n_channels == 0:
            text.append("No agents active", style="dim")
            return text

        width = self.size.width
        ch_width = max((width - 1) // n_channels - 1, 18)

        # --- Header row ---
        border_style = Style(color=_BORDER)
        header_style = Style(color=_HEADER_FG, bgcolor=_HEADER_BG)

        for i, ch in enumerate(self._channels[:n_channels]):
            corner = "+" if ascii_mode else ("\u250c" if i == 0 else "\u252c")
            label = f" CH{i + 1}: {ch.role[:8]} "
            header_text = label.center(ch_width - 1, "-" if ascii_mode else "\u2500")
            text.append(corner, style=border_style)
            text.append(header_text, style=header_style)
        end_corner = "+" if ascii_mode else "\u2510"
        text.append(end_corner + "\n", style=border_style)

        # --- Data rows ---
        for row_idx in range(self._max_visible_rows):
            for _ch_idx, ch in enumerate(self._channels[:n_channels]):
                sep = "|" if ascii_mode else "\u2502"
                text.append(sep, style=border_style)

                # Get row data (from visible window).
                visible_rows = list(ch.rows)[-self._max_visible_rows :]
                if row_idx < len(visible_rows):
                    row = visible_rows[row_idx]
                    is_current = row_idx == len(visible_rows) - 1
                else:
                    row = None
                    is_current = False

                self._render_cell(
                    text,
                    row,
                    is_current,
                    ch_width - 1,
                    ascii_mode,
                )
            sep = "|" if ascii_mode else "\u2502"
            text.append(sep + "\n", style=border_style)

        # --- Footer row ---
        for i, _ch in enumerate(self._channels[:n_channels]):
            corner = "+" if ascii_mode else ("\u2514" if i == 0 else "\u2534")
            text.append(corner, style=border_style)
            fill = "-" if ascii_mode else "\u2500"
            text.append(fill * (ch_width - 1), style=border_style)
        end_corner = "+" if ascii_mode else "\u2518"
        text.append(end_corner + "\n", style=border_style)

        return text

    def _render_cell(
        self,
        text: Text,
        row: TrackerRow | None,
        is_current: bool,
        width: int,
        ascii_mode: bool,
    ) -> None:
        """Render a single tracker cell into the text buffer.

        Args:
            text: Rich Text object to append to.
            row: Row data, or None for an empty cell.
            is_current: Whether this is the current (most recent) row.
            width: Available character width for the cell.
            ascii_mode: Use ASCII fallbacks for accessibility.
        """
        if row is None:
            text.append(" " * width)
            return

        # Current-row marker and styling.
        marker = ">" if ascii_mode else "\u25ba"
        if is_current:
            cur_style = Style(color=_CUR_FG, bgcolor=_CUR_BG)
            text.append(marker, style=cur_style)
        else:
            text.append(" ")

        # Row number.
        row_str = f"{row.row_num:02X}"
        row_style = Style(color=_CUR_FG, bgcolor=_CUR_BG) if is_current else Style(color=_ROW_GRAY)
        text.append(row_str, style=row_style)

        text.append(" ", style=Style(bgcolor=_CUR_BG) if is_current else Style())

        # Note.
        is_idle = row.note == "---"
        note_color = _NOTE_IDLE if is_idle else _NOTE_ACTIVE
        note_style = Style(color=_CUR_FG, bgcolor=_CUR_BG) if is_current else Style(color=note_color)
        text.append(f"{row.note:<3}", style=note_style)

        text.append(" ", style=Style(bgcolor=_CUR_BG) if is_current else Style())

        # Effect.
        efx_style = Style(color=_CUR_FG, bgcolor=_CUR_BG) if is_current else Style(color=_EFX_YELLOW)
        text.append(f"{row.effect:<3}", style=efx_style)

        text.append(" ", style=Style(bgcolor=_CUR_BG) if is_current else Style())

        # VU meter (with color gradient).
        vu_width = 4
        vu_str = render_vu(row.vu_level, vu_width, ascii_mode=ascii_mode)
        filled = int(max(0.0, min(1.0, row.vu_level)) * vu_width)
        for ci, char in enumerate(vu_str):
            if is_current:
                text.append(char, style=Style(color=_CUR_FG, bgcolor=_CUR_BG))
            elif ci < filled:
                text.append(char, style=_vu_style(ci, filled))
            else:
                text.append(char, style=Style(color=_VU_LO))

        # Pad remaining width.
        used = 1 + 2 + 1 + 3 + 1 + 3 + 1 + vu_width  # marker+row+sp+note+sp+efx+sp+vu
        remaining = width - used
        if remaining > 0:
            pad_style = Style(bgcolor=_CUR_BG) if is_current else Style()
            text.append(" " * remaining, style=pad_style)
