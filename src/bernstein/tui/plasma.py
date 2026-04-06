"""Demoscene plasma effect widget for the Bernstein TUI.

Real-time animated plasma using half-block characters and truecolor.
Classic nested-sine-wave color pattern inspired by 90s demo productions.
Toggle visibility, cycle palettes, modulate speed by agent activity.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import TYPE_CHECKING, Any

from rich.color import Color
from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from bernstein.cli.terminal_caps import TerminalCaps, detect_capabilities
from bernstein.tui.accessibility import (
    AccessibilityConfig,
    detect_accessibility,
)

if TYPE_CHECKING:
    from textual.timer import Timer

# ---------------------------------------------------------------------------
# Precomputed 256-entry sine lookup table
# ---------------------------------------------------------------------------

_SIN_LUT: list[float] = [math.sin(i * 2.0 * math.pi / 256.0) for i in range(256)]


def _sin_lut(x: float) -> float:
    """Fast sine via precomputed LUT with linear interpolation.

    Args:
        x: Input value (radians-like, wraps every 256 units).

    Returns:
        Approximate sine in [-1, +1].
    """
    idx = int(x * 256.0 / (2.0 * math.pi)) % 256
    return _SIN_LUT[idx]


# ---------------------------------------------------------------------------
# HSV to RGB conversion
# ---------------------------------------------------------------------------


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    """Convert HSV to RGB.

    Args:
        h: Hue in degrees (0-360).
        s: Saturation (0-1).
        v: Value/brightness (0-1).

    Returns:
        Tuple of (R, G, B) each in 0-255.
    """
    h = h % 360.0
    c = v * s
    x = c * (1.0 - abs((h / 60.0) % 2.0 - 1.0))
    m = v - c
    if h < 60.0:
        r, g, b = c, x, 0.0
    elif h < 120.0:
        r, g, b = x, c, 0.0
    elif h < 180.0:
        r, g, b = 0.0, c, x
    elif h < 240.0:
        r, g, b = 0.0, x, c
    elif h < 300.0:
        r, g, b = x, 0.0, c
    else:
        r, g, b = c, 0.0, x
    return (int((r + m) * 255.0), int((g + m) * 255.0), int((b + m) * 255.0))


# ---------------------------------------------------------------------------
# Palette modes
# ---------------------------------------------------------------------------


class PaletteMode(Enum):
    """Available plasma palette modes."""

    NEON = "neon"
    FIRE = "fire"
    OCEAN = "ocean"
    ACID = "acid"


_PALETTE_ORDER: list[PaletteMode] = [
    PaletteMode.NEON,
    PaletteMode.FIRE,
    PaletteMode.OCEAN,
    PaletteMode.ACID,
]


def _build_hsv_lut(palette: PaletteMode) -> list[tuple[int, int, int]]:
    """Build a 360-entry HSV-to-RGB lookup table for the given palette.

    Args:
        palette: Which color range to use.

    Returns:
        List of 360 RGB tuples.
    """
    lut: list[tuple[int, int, int]] = []
    for i in range(360):
        t = i / 360.0  # 0..1 normalized position
        if palette == PaletteMode.NEON:
            hue = float(i)  # full rainbow 0-360
            sat, val = 0.8, 0.7
        elif palette == PaletteMode.FIRE:
            hue = t * 60.0  # 0-60 (red to yellow)
            sat, val = 0.9, 0.8
        elif palette == PaletteMode.OCEAN:
            hue = 180.0 + t * 60.0  # 180-240 (cyan to blue)
            sat, val = 0.8, 0.7
        else:  # ACID
            # green-cyan (90-180) + purple (270-330), split at midpoint
            hue = 90.0 + t * 2.0 * 90.0 if t < 0.5 else 270.0 + (t - 0.5) * 2.0 * 60.0
            sat, val = 0.85, 0.75
        lut.append(_hsv_to_rgb(hue, sat, val))
    return lut


# ---------------------------------------------------------------------------
# 16-color shade characters for basic terminals
# ---------------------------------------------------------------------------

_SHADE_CHARS = " ░▒▓█"
_BASIC_COLORS: list[str] = [
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "white",
]


# ---------------------------------------------------------------------------
# Plasma canvas widget
# ---------------------------------------------------------------------------


class PlasmaCanvas(Static):
    """Demoscene plasma effect widget.

    Real-time animated plasma using half-block characters and truecolor.
    Toggle visibility, cycle palettes, modulate speed by agent activity.

    Attributes:
        DEFAULT_CSS: Default CSS for the widget.
    """

    DEFAULT_CSS = """
    PlasmaCanvas {
        height: 12;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._t: float = 0.0
        self._palette: PaletteMode = PaletteMode.NEON
        self._speed_mult: float = 1.0
        self._sin_lut: list[float] = _SIN_LUT
        self._hsv_lut: list[tuple[int, int, int]] = _build_hsv_lut(self._palette)
        self._timer: Timer | None = None
        self._caps: TerminalCaps | None = None
        self._a11y: AccessibilityConfig | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        """Start the animation timer (unless accessibility disables it)."""
        self._caps = detect_capabilities()
        level = detect_accessibility()
        self._a11y = AccessibilityConfig.from_level(level)

        if self._a11y.no_animations:
            # Render one frozen frame
            self.update(self._render_frame())
            return

        self._timer = self.set_interval(1.0 / 15.0, self._tick)

    # ── Animation ─────────────────────────────────────────────────────────

    def _tick(self) -> None:
        """Advance one frame and request re-render."""
        if self.display:
            self._t += 0.1 * self._speed_mult
            self.update(self._render_frame())

    # ── Plasma core ───────────────────────────────────────────────────────

    @staticmethod
    def _plasma_value(x: int, y: int, t: float) -> float:
        """Compute plasma intensity at (x, y) for time t.

        Args:
            x: Horizontal coordinate.
            y: Vertical coordinate.
            t: Animation time parameter.

        Returns:
            Plasma value in [-4.0, +4.0].
        """
        v = _sin_lut(x / 16.0 + t)
        v += _sin_lut(y / 8.0 + t * 0.7)
        v += _sin_lut((x + y) / 16.0 + t * 0.5)
        v += _sin_lut(math.sqrt(x * x + y * y) / 8.0 + t * 0.3)
        return v

    def _value_to_rgb(self, v: float) -> tuple[int, int, int]:
        """Map a plasma value to an RGB color via the current palette LUT.

        Args:
            v: Plasma value in [-4.0, +4.0].

        Returns:
            (R, G, B) tuple, each 0-255.
        """
        hue_idx = int((v + 4.0) / 8.0 * 360.0) % 360
        return self._hsv_lut[hue_idx]

    # ── Rendering ─────────────────────────────────────────────────────────

    def render(self) -> Text:
        """Render the current plasma frame as Rich Text."""
        return self._render_frame()

    def _render_frame(self) -> Text:
        """Build one frame of the plasma effect.

        Returns:
            Rich Text object with colored half-block characters.
        """
        caps = self._caps
        a11y = self._a11y

        # Accessibility: no-unicode fallback
        if a11y and a11y.no_unicode:
            return Text("[ PLASMA EFFECT \u2014 animations disabled ]")

        width = self.size.width if self.size.width > 0 else 40
        height = self.size.height if self.size.height > 0 else 12

        # Determine render mode
        use_truecolor = caps is not None and caps.truecolor
        use_256 = caps is not None and caps.supports_256color
        use_halfblocks = use_truecolor or use_256

        if use_halfblocks:
            return self._render_halfblock(width, height, use_truecolor)
        return self._render_shade(width, height)

    def _render_halfblock(self, width: int, height: int, truecolor: bool) -> Text:
        """Render using half-block characters with fg/bg colors.

        Each terminal row encodes two pixel rows via the upper-half block.

        Args:
            width: Terminal columns.
            height: Terminal rows available.
            truecolor: Use 24-bit color (otherwise 256-color).

        Returns:
            Colored Rich Text.
        """
        text = Text()
        t = self._t

        for row in range(height):
            top_y = row * 2
            bot_y = top_y + 1
            for col in range(width):
                v_top = self._plasma_value(col, top_y, t)
                v_bot = self._plasma_value(col, bot_y, t)
                r_top = self._value_to_rgb(v_top)
                r_bot = self._value_to_rgb(v_bot)

                if truecolor:
                    fg = Color.from_rgb(r_top[0], r_top[1], r_top[2])
                    bg = Color.from_rgb(r_bot[0], r_bot[1], r_bot[2])
                else:
                    # Approximate with 256-color cube
                    fg = Color.from_rgb(r_top[0], r_top[1], r_top[2])
                    bg = Color.from_rgb(r_bot[0], r_bot[1], r_bot[2])

                text.append("\u2580", style=Style(color=fg, bgcolor=bg))
            if row < height - 1:
                text.append("\n")
        return text

    def _render_shade(self, width: int, height: int) -> Text:
        """Render using shade characters with basic 16-color palette.

        Args:
            width: Terminal columns.
            height: Terminal rows.

        Returns:
            Colored Rich Text using shade characters.
        """
        text = Text()
        t = self._t
        for row in range(height):
            for col in range(width):
                v = self._plasma_value(col, row, t)
                # Map [-4, 4] to shade index [0, 4]
                shade_idx = int((v + 4.0) / 8.0 * (len(_SHADE_CHARS) - 1))
                shade_idx = max(0, min(len(_SHADE_CHARS) - 1, shade_idx))
                # Pick a basic color
                color_idx = int((v + 4.0) / 8.0 * (len(_BASIC_COLORS) - 1))
                color_idx = max(0, min(len(_BASIC_COLORS) - 1, color_idx))
                text.append(
                    _SHADE_CHARS[shade_idx],
                    style=Style(color=_BASIC_COLORS[color_idx]),
                )
            if row < height - 1:
                text.append("\n")
        return text

    # ── Public API ────────────────────────────────────────────────────────

    def cycle_palette(self) -> PaletteMode:
        """Cycle to the next palette mode.

        Returns:
            The new active PaletteMode.
        """
        idx = _PALETTE_ORDER.index(self._palette)
        self._palette = _PALETTE_ORDER[(idx + 1) % len(_PALETTE_ORDER)]
        self._hsv_lut = _build_hsv_lut(self._palette)
        return self._palette

    def update_activity(self, agent_count: int) -> None:
        """Set speed multiplier based on agent activity.

        Args:
            agent_count: Number of currently active agents.
        """
        if agent_count == 0:
            self._speed_mult = 0.3
        elif agent_count <= 2:
            self._speed_mult = 0.6
        elif agent_count <= 4:
            self._speed_mult = 1.0
        else:
            self._speed_mult = 1.5

    @property
    def palette(self) -> PaletteMode:
        """Current palette mode."""
        return self._palette

    @property
    def speed_mult(self) -> float:
        """Current speed multiplier."""
        return self._speed_mult

    @property
    def time(self) -> float:
        """Current animation time parameter."""
        return self._t
