"""Truecolor gradient background engine for terminal output.

Uses half-block characters (▄) with 24-bit ANSI color codes to achieve
2× vertical pixel resolution: one terminal cell = two pixel rows.

  • Top pixel    → background color:  \\033[48;2;R;G;Bm
  • Bottom pixel → foreground + ▄:    \\033[38;2;R;G;Bm + ▄

Performance target: <20ms for an 80×24 gradient.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

# ── Type alias ─────────────────────────────────────────────────────────────────

type RGB = tuple[int, int, int]

# ── Bernstein default palette ──────────────────────────────────────────────────

PALETTE_NAVY: RGB = (0x0A, 0x0E, 0x1A)  # #0a0e1a — dark navy
PALETTE_TEAL: RGB = (0x0A, 0x3D, 0x62)  # #0a3d62 — teal
PALETTE_BLACK: RGB = (0x00, 0x00, 0x00)  # #000000 — black

#: Default Bernstein gradient: dark navy → teal → black
BERNSTEIN_COLORS: list[RGB] = [PALETTE_NAVY, PALETTE_TEAL, PALETTE_BLACK]

# ── ANSI escape helpers ────────────────────────────────────────────────────────

_RESET = "\033[0m"
_LOWER_HALF = "\u2584"  # ▄  U+2584 LOWER HALF BLOCK


def _bg(r: int, g: int, b: int) -> str:
    return f"\033[48;2;{r};{g};{b}m"


def _fg(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m"


# ── Color stops & interpolation ────────────────────────────────────────────────


def _make_stops(
    colors: Sequence[RGB],
    stops: Sequence[float] | None,
) -> list[tuple[float, RGB]]:
    """Build a sorted list of (position, color) pairs.

    Args:
        colors: Sequence of RGB tuples.
        stops: Explicit positions (0.0–1.0) matching each color, or *None* to
               distribute colors evenly across [0, 1].

    Returns:
        Sorted list of ``(position, color)`` pairs.

    Raises:
        ValueError: If *colors* is empty or *stops* length mismatches *colors*.
    """
    n = len(colors)
    if n == 0:
        raise ValueError("At least one color is required")
    if n == 1:
        c = colors[0]
        return [(0.0, c), (1.0, c)]

    if stops is None:
        positions: list[float] = [i / (n - 1) for i in range(n)]
    else:
        if len(stops) != n:
            raise ValueError(f"stops length {len(stops)} != colors length {n}")
        positions = list(stops)

    return sorted(zip(positions, colors), key=lambda s: s[0])


def _lerp_color(t: float, color_stops: list[tuple[float, RGB]]) -> RGB:
    """Linearly interpolate color at position *t* (0.0–1.0) across sorted stops."""
    if t <= color_stops[0][0]:
        return color_stops[0][1]
    if t >= color_stops[-1][0]:
        return color_stops[-1][1]

    for i in range(len(color_stops) - 1):
        p0, c0 = color_stops[i]
        p1, c1 = color_stops[i + 1]
        if p0 <= t <= p1:
            f = (t - p0) / (p1 - p0)
            return (
                round(c0[0] + f * (c1[0] - c0[0])),
                round(c0[1] + f * (c1[1] - c0[1])),
                round(c0[2] + f * (c1[2] - c0[2])),
            )

    return color_stops[-1][1]


# ── Pixel grid builders ────────────────────────────────────────────────────────


def _linear_pixel_grid(
    width: int,
    pixel_height: int,
    color_stops: list[tuple[float, RGB]],
    direction: Literal["top_bottom", "left_right", "diagonal"],
) -> list[list[RGB]]:
    """Build a *pixel_height* × *width* RGB grid for a linear gradient."""
    ph_1 = max(pixel_height - 1, 1)
    w_1 = max(width - 1, 1)
    grid: list[list[RGB]] = []

    for py in range(pixel_height):
        py_t = py / ph_1
        row: list[RGB] = []
        for px in range(width):
            if direction == "top_bottom":
                t = py_t
            elif direction == "left_right":
                t = px / w_1
            else:  # diagonal
                t = (py_t + px / w_1) * 0.5
            row.append(_lerp_color(t, color_stops))
        grid.append(row)

    return grid


def _radial_pixel_grid(
    width: int,
    pixel_height: int,
    color_stops: list[tuple[float, RGB]],
    cx: float,
    cy: float,
) -> list[list[RGB]]:
    """Build a *pixel_height* × *width* RGB grid for a radial gradient.

    Args:
        cx: Center x in pixel coordinates.
        cy: Center y in pixel coordinates.
    """
    corners = [
        math.hypot(cx, cy),
        math.hypot(width - 1 - cx, cy),
        math.hypot(cx, pixel_height - 1 - cy),
        math.hypot(width - 1 - cx, pixel_height - 1 - cy),
    ]
    max_dist = max(corners) or 1.0

    grid: list[list[RGB]] = []
    for py in range(pixel_height):
        dy = py - cy
        row: list[RGB] = []
        for px in range(width):
            t = math.hypot(px - cx, dy) / max_dist
            row.append(_lerp_color(t, color_stops))
        grid.append(row)

    return grid


# ── Half-block renderer ────────────────────────────────────────────────────────


def _render_half_block(width: int, height: int, pixel_grid: list[list[RGB]]) -> str:
    """Encode a pixel grid as half-block ANSI text.

    Terminal row ``r`` covers pixel rows ``2r`` (top / background) and
    ``2r+1`` (bottom / foreground via ▄).  Consecutive identical escape
    sequences are suppressed to reduce output size.

    Args:
        width: Terminal columns.
        height: Terminal rows.
        pixel_grid: At least ``height * 2`` rows of *width* RGB columns.

    Returns:
        Multi-line ANSI string (``height`` lines joined with ``\\n``).
    """
    lines: list[str] = []
    for r in range(height):
        top_row = pixel_grid[r * 2]
        bot_row = pixel_grid[r * 2 + 1]
        parts: list[str] = []
        prev_bg_r, prev_bg_g, prev_bg_b = -1, -1, -1
        prev_fg_r, prev_fg_g, prev_fg_b = -1, -1, -1
        for c in range(width):
            bg_r, bg_g, bg_b = top_row[c]
            fg_r, fg_g, fg_b = bot_row[c]
            cell = ""
            if bg_r != prev_bg_r or bg_g != prev_bg_g or bg_b != prev_bg_b:
                cell += _bg(bg_r, bg_g, bg_b)
                prev_bg_r, prev_bg_g, prev_bg_b = bg_r, bg_g, bg_b
            if fg_r != prev_fg_r or fg_g != prev_fg_g or fg_b != prev_fg_b:
                cell += _fg(fg_r, fg_g, fg_b)
                prev_fg_r, prev_fg_g, prev_fg_b = fg_r, fg_g, fg_b
            cell += _LOWER_HALF
            parts.append(cell)
        lines.append("".join(parts) + _RESET)
    return "\n".join(lines)


# ── Public API ─────────────────────────────────────────────────────────────────


def linear_gradient(
    width: int,
    height: int,
    colors: Sequence[RGB],
    direction: Literal["top_bottom", "left_right", "diagonal"] = "top_bottom",
    stops: Sequence[float] | None = None,
) -> str:
    """Render a linear gradient as a half-block ANSI string.

    Each terminal row represents two pixel rows (half-block resolution).
    The returned string can be printed directly or fed into a FrameBuffer.

    Args:
        width: Terminal columns.
        height: Terminal rows (output has *height* lines).
        colors: RGB color tuples from first stop to last.
        direction: ``'top_bottom'``, ``'left_right'``, or ``'diagonal'``.
        stops: Explicit positions (0.0–1.0) for each color.  *None* distributes
               colors evenly.

    Returns:
        ANSI-encoded string; lines joined with ``\\n``.  Empty string if
        *width* or *height* is zero.

    Example::

        from bernstein.cli.gradients import linear_gradient, BERNSTEIN_COLORS
        print(linear_gradient(80, 24, BERNSTEIN_COLORS))
    """
    if width <= 0 or height <= 0:
        return ""
    color_stops = _make_stops(colors, stops)
    grid = _linear_pixel_grid(width, height * 2, color_stops, direction)
    return _render_half_block(width, height, grid)


def radial_gradient(
    width: int,
    height: int,
    center_color: RGB,
    edge_color: RGB,
    center_x: float = 0.5,
    center_y: float = 0.5,
    extra_stops: list[tuple[float, RGB]] | None = None,
) -> str:
    """Render a radial (center-glow) gradient as a half-block ANSI string.

    Args:
        width: Terminal columns.
        height: Terminal rows.
        center_color: RGB color at the glow center.
        edge_color: RGB color at the outer edge.
        center_x: Horizontal center as fraction of width (0.0–1.0).
        center_y: Vertical center as fraction of pixel height (0.0–1.0).
        extra_stops: Optional intermediate stops as ``[(position, color), ...]``
                     inserted between center (0.0) and edge (1.0).

    Returns:
        ANSI-encoded string; lines joined with ``\\n``.  Empty string if
        *width* or *height* is zero.
    """
    if width <= 0 or height <= 0:
        return ""
    pixel_height = height * 2
    color_stops: list[tuple[float, RGB]] = [(0.0, center_color)]
    if extra_stops:
        color_stops.extend(extra_stops)
    color_stops.append((1.0, edge_color))
    color_stops.sort(key=lambda s: s[0])

    cx = center_x * (width - 1)
    cy = center_y * (pixel_height - 1)
    grid = _radial_pixel_grid(width, pixel_height, color_stops, cx, cy)
    return _render_half_block(width, height, grid)
