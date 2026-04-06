"""TUI-014: Braille oscilloscope / VU meter widget.

Renders agent activity as real-time waveforms using Unicode braille
characters (U+2800..U+28FF), achieving smooth curves at 2x4 subpixel
resolution per terminal cell.  Part of the "Bernstein '89" retro
demoscene aesthetic.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, ClassVar

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from bernstein.cli.terminal_caps import TerminalCaps, detect_capabilities
from bernstein.tui.accessibility import (
    AccessibilityConfig,
    detect_accessibility,
)

# ── Braille dot-bit lookup ────────────────────────────────────────────────
#
# Unicode braille patterns (U+2800..U+28FF) encode a 2x4 dot grid:
#
#   Col 0   Col 1
#   (0,0)=0x01  (1,0)=0x08   Row 0
#   (0,1)=0x02  (1,1)=0x10   Row 1
#   (0,2)=0x04  (1,2)=0x20   Row 2
#   (0,3)=0x40  (1,3)=0x80   Row 3
#
# Character = chr(0x2800 + bitmask)

_DOT_BITS: list[list[int]] = [
    [0x01, 0x08],  # Row 0
    [0x02, 0x10],  # Row 1
    [0x04, 0x20],  # Row 2
    [0x40, 0x80],  # Row 3
]

# Sparkline block characters (8 height levels) for non-braille fallback.
_SPARKLINE_CHARS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"

# Role abbreviations for the left-margin channel label.
_ROLE_ABBREV: dict[str, str] = {
    "backend": "BE",
    "frontend": "FE",
    "qa": "QA",
    "security": "SC",
    "devops": "DO",
    "architect": "AR",
    "docs": "DC",
    "reviewer": "RV",
    "ml-engineer": "ML",
    "prompt-engineer": "PR",
    "retrieval": "RT",
    "visionary": "VI",
    "analyst": "AN",
    "resolver": "RS",
    "ci-fixer": "CI",
    "manager": "MG",
    "vp": "VP",
}


# ── Braille canvas ────────────────────────────────────────────────────────


class BrailleCanvas:
    """2D canvas that renders to braille characters.

    Each terminal cell contains a 2x4 dot grid, giving effective resolution
    of (width*2) x (height*4) dots for a (width x height) cell area.
    """

    def __init__(self, width: int, height: int) -> None:
        """Initialize canvas.

        Args:
            width: Width in terminal cells.
            height: Height in terminal cells.
        """
        self._width = width
        self._height = height
        self._dot_width = width * 2
        self._dot_height = height * 4
        self._cells: list[list[int]] = [[0] * width for _ in range(height)]

    @property
    def width(self) -> int:
        """Terminal-cell width."""
        return self._width

    @property
    def height(self) -> int:
        """Terminal-cell height."""
        return self._height

    @property
    def dot_width(self) -> int:
        """Dot-space horizontal resolution."""
        return self._dot_width

    @property
    def dot_height(self) -> int:
        """Dot-space vertical resolution."""
        return self._dot_height

    def clear(self) -> None:
        """Clear all dots."""
        for row in self._cells:
            for i in range(len(row)):
                row[i] = 0

    def set_dot(self, x: int, y: int) -> None:
        """Set a dot at dot-space coordinates.

        Args:
            x: Horizontal position (0..dot_width-1).
            y: Vertical position (0..dot_height-1, 0=top).
        """
        if 0 <= x < self._dot_width and 0 <= y < self._dot_height:
            cell_x = x // 2
            cell_y = y // 4
            dot_x = x % 2
            dot_y = y % 4
            self._cells[cell_y][cell_x] |= _DOT_BITS[dot_y][dot_x]

    def get_cell(self, cell_x: int, cell_y: int) -> int:
        """Return the bitmask for a given terminal cell.

        Args:
            cell_x: Cell column index.
            cell_y: Cell row index.

        Returns:
            Braille bitmask for that cell.
        """
        if 0 <= cell_x < self._width and 0 <= cell_y < self._height:
            return self._cells[cell_y][cell_x]
        return 0

    def draw_line(self, x0: int, y0: int, x1: int, y1: int) -> None:
        """Draw a line using Bresenham's algorithm in dot-space.

        Args:
            x0: Start X.
            y0: Start Y.
            x1: End X.
            y1: End Y.
        """
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        while True:
            self.set_dot(x0, y0)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    def render_row(self, row: int) -> str:
        """Render one row of cells as braille characters.

        Args:
            row: Row index (0..height-1).

        Returns:
            String of braille characters for that row.
        """
        if 0 <= row < self._height:
            return "".join(chr(0x2800 + mask) for mask in self._cells[row])
        return ""


# ── Data model ────────────────────────────────────────────────────────────


@dataclass
class ScopeChannel:
    """One oscilloscope channel tracking an agent's activity.

    Attributes:
        agent_id: Unique agent identifier.
        role: Agent role (e.g. "backend", "qa").
        color: Hex color string for this channel.
        samples: Ring buffer of activity values.
        peak: Peak hold value (highest recent sample).
        peak_age: Ticks since peak was set (decays over time).
    """

    agent_id: str
    role: str
    color: str
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=120))
    peak: float = 0.0
    peak_age: int = 0


def _abbreviate_role(role: str) -> str:
    """Return a 2-character abbreviation for a role.

    Args:
        role: Role name.

    Returns:
        Two-character uppercase abbreviation.
    """
    return _ROLE_ABBREV.get(role.lower(), role[:2].upper())


def _color_for_agent(agent_id: str, palette: list[str]) -> str:
    """Deterministic color selection by agent ID hash.

    Args:
        agent_id: Agent identifier.
        palette: List of hex color strings.

    Returns:
        Hex color string from palette.
    """
    return palette[hash(agent_id) % len(palette)]


def _dim_color(hex_color: str, factor: float) -> str:
    """Dim a hex color by a brightness factor.

    Args:
        hex_color: Color like "#ff6b6b".
        factor: Brightness multiplier (0.0..1.0).

    Returns:
        Dimmed hex color string.
    """
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return "#808080"
    r = int(int(hex_color[0:2], 16) * factor)
    g = int(int(hex_color[2:4], 16) * factor)
    b = int(int(hex_color[4:6], 16) * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


# ── Rendering helpers ─────────────────────────────────────────────────────


def _render_channel_braille(
    channel: ScopeChannel,
    width: int,
    rows: int,
) -> tuple[BrailleCanvas, BrailleCanvas]:
    """Render a channel's waveform and grid onto separate canvases.

    Args:
        channel: Channel to render.
        width: Canvas width in terminal cells.
        rows: Canvas height in terminal cells.

    Returns:
        Tuple of (waveform_canvas, grid_canvas).
    """
    wave = BrailleCanvas(width, rows)
    grid = BrailleCanvas(width, rows)
    dot_h = wave.dot_height
    dot_w = wave.dot_width

    # Draw grid lines at 25%, 50%, 75% of height.
    for pct in (0.25, 0.50, 0.75):
        gy = int(pct * (dot_h - 1))
        for gx in range(0, dot_w, 3):  # dashed
            grid.set_dot(gx, gy)

    samples = list(channel.samples)
    if not samples:
        return wave, grid

    # Take the last dot_w samples (one per dot column).
    visible = samples[-dot_w:] if len(samples) > dot_w else samples

    # Determine max value for normalization.
    max_val = max(max(visible), channel.peak, 1e-9)

    # Map samples to dot-space Y coordinates and draw connected line.
    prev_x: int | None = None
    prev_y: int | None = None
    x_offset = dot_w - len(visible)
    for i, val in enumerate(visible):
        norm = val / max_val
        sx = x_offset + i
        sy = (dot_h - 1) - int(norm * (dot_h - 1))
        if prev_x is not None and prev_y is not None:
            wave.draw_line(prev_x, prev_y, sx, sy)
        else:
            wave.set_dot(sx, sy)
        prev_x, prev_y = sx, sy

    # Peak hold marker: bright dot at peak Y across last few columns.
    if channel.peak > 0:
        peak_y = (dot_h - 1) - int((channel.peak / max_val) * (dot_h - 1))
        peak_x = dot_w - 1
        wave.set_dot(peak_x, peak_y)
        if peak_x > 0:
            wave.set_dot(peak_x - 1, peak_y)

    return wave, grid


def _render_sparkline_fallback(
    channels: list[ScopeChannel],
    width: int,
) -> Text:
    """Render channels as sparkline block characters (non-braille fallback).

    Args:
        channels: Active channels.
        width: Available terminal width.

    Returns:
        Rich Text with sparkline rendering.
    """
    text = Text()
    for i, ch in enumerate(channels):
        if i > 0:
            text.append("\n")
        label = _abbreviate_role(ch.role)
        text.append(f"{label:>2} ", style=Style(color=ch.color, bold=True))

        samples = list(ch.samples)
        usable = width - 3  # account for label
        visible = samples[-usable:] if len(samples) > usable else samples
        if not visible:
            text.append(" " * usable, style="dim")
            continue

        max_val = max(max(visible), 1e-9)
        for val in visible:
            norm = val / max_val
            idx = min(int(norm * (len(_SPARKLINE_CHARS) - 1)), len(_SPARKLINE_CHARS) - 1)
            text.append(_SPARKLINE_CHARS[idx], style=Style(color=ch.color))
    return text


def _render_ascii_fallback(
    channels: list[ScopeChannel],
    width: int,
) -> Text:
    """Render channels as ASCII for accessibility mode.

    Shows latest value as a simple bar chart with text values.

    Args:
        channels: Active channels.
        width: Available terminal width.

    Returns:
        Rich Text with ASCII bar chart.
    """
    text = Text()
    for i, ch in enumerate(channels):
        if i > 0:
            text.append("\n")
        label = _abbreviate_role(ch.role)
        latest = ch.samples[-1] if ch.samples else 0.0
        bar_width = width - 12  # label + value
        if bar_width < 1:
            bar_width = 1
        max_val = max(ch.peak, latest, 1e-9)
        filled = int((latest / max_val) * bar_width)
        empty = bar_width - filled
        text.append(f"{label:>2} ")
        text.append("|" * filled + "-" * empty)
        text.append(f" {latest:.1f}")
    return text


def _render_accessibility_static(
    channels: list[ScopeChannel],
    width: int,
) -> Text:
    """Render static bar chart for no_animations mode.

    Single row of block chars per agent showing latest value.

    Args:
        channels: Active channels.
        width: Available terminal width.

    Returns:
        Rich Text with static bars.
    """
    text = Text()
    for i, ch in enumerate(channels):
        if i > 0:
            text.append(" ")
        label = _abbreviate_role(ch.role)
        latest = ch.samples[-1] if ch.samples else 0.0
        max_val = max(ch.peak, latest, 1e-9)
        norm = latest / max_val
        idx = min(int(norm * (len(_SPARKLINE_CHARS) - 1)), len(_SPARKLINE_CHARS) - 1)
        text.append(f"{label}:", style=Style(color=ch.color, bold=True))
        text.append(_SPARKLINE_CHARS[idx], style=Style(color=ch.color))
    return text


# ── Widget ────────────────────────────────────────────────────────────────


class OscilloscopeWidget(Static):
    """Agent activity rendered as oscilloscope waveforms using braille characters.

    Each active agent gets a channel with a scrolling waveform.  Supports
    braille (full fidelity), sparkline (block-char fallback), and ASCII
    (accessibility) rendering modes.
    """

    DEFAULT_CSS = """
    OscilloscopeWidget {
        height: 16;
    }
    """

    CHANNEL_COLORS: ClassVar[list[str]] = [
        "#ff6b6b",
        "#4ecdc4",
        "#45b7d1",
        "#96ceb4",
        "#feca57",
        "#ff9ff3",
        "#54a0ff",
        "#5f27cd",
        "#01a3a4",
        "#f368e0",
        "#ff6348",
        "#7bed9f",
    ]

    def __init__(
        self,
        *,
        max_channels: int = 4,
        sample_window: int = 120,
        rows_per_channel: int = 4,
        peak_hold_ticks: int = 15,
        caps: TerminalCaps | None = None,
        a11y: AccessibilityConfig | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the oscilloscope widget.

        Args:
            max_channels: Maximum simultaneous channels displayed.
            sample_window: Samples retained per channel ring buffer.
            rows_per_channel: Terminal rows per waveform.
            peak_hold_ticks: Ticks before peak starts decaying.
            caps: Terminal capabilities (auto-detected if None).
            a11y: Accessibility config (auto-detected if None).
            **kwargs: Forwarded to Textual Static.
        """
        super().__init__(**kwargs)
        self._channels: list[ScopeChannel] = []
        self._max_channels = max_channels
        self._sample_window = sample_window
        self._rows_per_channel = rows_per_channel
        self._peak_hold_ticks = peak_hold_ticks
        self._caps = caps
        self._a11y = a11y

    @property
    def channels(self) -> list[ScopeChannel]:
        """Current channels (read-only copy)."""
        return list(self._channels)

    def _get_caps(self) -> TerminalCaps:
        """Resolve terminal capabilities."""
        if self._caps is not None:
            return self._caps
        return detect_capabilities()

    def _get_a11y(self) -> AccessibilityConfig:
        """Resolve accessibility config."""
        if self._a11y is not None:
            return self._a11y
        level = detect_accessibility()
        return AccessibilityConfig.from_level(level)

    def _find_channel(self, agent_id: str) -> ScopeChannel | None:
        """Find a channel by agent ID."""
        for ch in self._channels:
            if ch.agent_id == agent_id:
                return ch
        return None

    def update_agents(self, agents: list[dict[str, Any]]) -> None:
        """Update with current agent data.

        Creates channels for new agents and removes channels for agents
        no longer present.  Respects max_channels limit.

        Args:
            agents: List of agent dicts with keys:
                - session_id: str
                - role: str
                - tokens_per_sec: float (optional activity metric)
        """
        current_ids = {a["session_id"] for a in agents if "session_id" in a}

        # Remove channels for agents that are gone.
        self._channels = [ch for ch in self._channels if ch.agent_id in current_ids]

        # Add new channels up to max.
        for agent in agents:
            aid = agent.get("session_id", "")
            if not aid:
                continue
            if self._find_channel(aid) is not None:
                continue
            if len(self._channels) >= self._max_channels:
                break
            role = agent.get("role", "agent")
            color = _color_for_agent(aid, self.CHANNEL_COLORS)
            self._channels.append(
                ScopeChannel(
                    agent_id=aid,
                    role=role,
                    color=color,
                    samples=deque(maxlen=self._sample_window),
                )
            )

    def add_samples(self, agent_samples: dict[str, float]) -> None:
        """Add new sample values for each agent.

        Also updates peak hold tracking.

        Args:
            agent_samples: Mapping of agent_id to activity value.
        """
        for ch in self._channels:
            val = agent_samples.get(ch.agent_id, 0.0)
            ch.samples.append(val)

            # Peak hold: track highest recent value.
            if val >= ch.peak:
                ch.peak = val
                ch.peak_age = 0
            else:
                ch.peak_age += 1
                if ch.peak_age > self._peak_hold_ticks:
                    # Decay: reduce peak toward current max.
                    recent = list(ch.samples)[-10:] if len(ch.samples) >= 10 else list(ch.samples)
                    ch.peak = max(recent) if recent else 0.0
                    ch.peak_age = 0

    def render(self) -> Text:
        """Render all channels as braille waveforms.

        Returns:
            Rich Text with the oscilloscope display.
        """
        a11y = self._get_a11y()
        caps = self._get_caps()
        width = 40  # default; real width comes from Textual layout

        if not self._channels:
            return Text("No agent activity", style="dim")

        active = self._channels[: self._max_channels]

        # Accessibility: no_animations -> static bar chart.
        if a11y.no_animations:
            return _render_accessibility_static(active, width)

        # Accessibility: no_unicode -> ASCII bars.
        if a11y.no_unicode:
            return _render_ascii_fallback(active, width)

        # Terminal without braille support -> sparkline fallback.
        if not caps.braille:
            return _render_sparkline_fallback(active, width)

        # Full braille rendering.
        return self._render_braille(active, width)

    def _render_braille(self, channels: list[ScopeChannel], width: int) -> Text:
        """Render channels as braille waveforms.

        Args:
            channels: Channels to render.
            width: Available terminal width.

        Returns:
            Rich Text with braille waveforms.
        """
        text = Text()
        label_width = 3  # "XX "
        canvas_width = max(width - label_width, 4)

        for ch_idx, ch in enumerate(channels):
            if ch_idx > 0:
                text.append("\n")

            wave, grid = _render_channel_braille(ch, canvas_width, self._rows_per_channel)
            label = _abbreviate_role(ch.role)
            wave_style = Style(color=ch.color)
            grid_style = Style(color=_dim_color(ch.color, 0.3))

            for row in range(self._rows_per_channel):
                if row > 0:
                    text.append("\n")

                # Left margin label on first row only.
                if row == 0:
                    text.append(f"{label:>2} ", style=Style(color=ch.color, bold=True))
                else:
                    text.append("   ")

                # Render each cell: waveform over grid.
                for col in range(canvas_width):
                    w_mask = wave.get_cell(col, row)
                    g_mask = grid.get_cell(col, row)
                    if w_mask:
                        # Waveform dots take priority; merge grid underneath.
                        text.append(chr(0x2800 + (w_mask | g_mask)), style=wave_style)
                    elif g_mask:
                        text.append(chr(0x2800 + g_mask), style=grid_style)
                    else:
                        text.append(chr(0x2800))  # empty braille

        return text
