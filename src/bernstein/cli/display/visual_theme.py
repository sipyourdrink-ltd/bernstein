"""Shared visual theme helpers for premium Bernstein CLI rendering.

Centralizes palette selection and lightweight color transforms so splash,
dashboard, and asset generation stay visually consistent without duplicating
hex literals across modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from rich.markup import escape

if TYPE_CHECKING:
    from collections.abc import Sequence

RGB = tuple[int, int, int]


@dataclass(frozen=True)
class VisualPalette:
    """Named Bernstein colors used across the premium CLI theme."""

    navy: str = "#08121F"
    navy_soft: str = "#0E2235"
    teal: str = "#0D5E73"
    cyan: str = "#39D6FF"
    glow: str = "#9DEEFF"
    phosphor: str = "#33FF99"
    surface: str = "#0B1724"
    surface_alt: str = "#102133"
    line: str = "#18435B"
    text: str = "#E8F6FF"
    text_dim: str = "#7EA4B8"
    warning: str = "#FFCF5A"
    danger: str = "#FF6B6B"
    success: str = "#59F3A3"


PALETTE = VisualPalette()
BERNSTEIN_GRADIENT: tuple[str, ...] = (PALETTE.navy_soft, PALETTE.teal, PALETTE.cyan)
CRT_GRADIENT: tuple[str, ...] = ("#1F4221", "#33FF99", PALETTE.cyan)

MODEL_BRAND_COLORS: dict[str, str] = {
    "claude": "#F59E0B",
    "codex": "#34D399",
    "gemini": "#60A5FA",
    "cursor": "#C084FC",
}

ROLE_COLORS: dict[str, str] = {
    "backend": PALETTE.cyan,
    "frontend": "#FFB86B",
    "qa": "#FFD166",
    "manager": "#7DD3FC",
    "reviewer": "#FCA5A5",
    "ops": "#86EFAC",
    "devops": "#86EFAC",
}

STATUS_COLORS: dict[str, str] = {
    "working": PALETTE.success,
    "starting": PALETTE.warning,
    "idle": PALETTE.warning,
    "done": PALETTE.success,
    "failed": PALETTE.danger,
    "dead": PALETTE.danger,
    "blocked": PALETTE.warning,
    "open": PALETTE.text_dim,
}


def hex_to_rgb(color: str) -> RGB:
    """Convert a ``#RRGGBB`` string into an RGB tuple.

    Args:
        color: Hex color string with or without a leading ``#``.

    Returns:
        Three-channel RGB tuple.

    Raises:
        ValueError: If ``color`` is not a 6-digit hex string.
    """
    normalized = color.strip().lstrip("#")
    if len(normalized) != 6:
        raise ValueError(f"Expected 6-digit hex color, got {color!r}")
    try:
        return (
            int(normalized[0:2], 16),
            int(normalized[2:4], 16),
            int(normalized[4:6], 16),
        )
    except ValueError as exc:
        raise ValueError(f"Invalid hex color: {color!r}") from exc


def rgb_to_hex(rgb: RGB) -> str:
    """Convert an RGB tuple into ``#RRGGBB`` format."""
    r, g, b = rgb
    return f"#{max(0, min(255, r)):02X}{max(0, min(255, g)):02X}{max(0, min(255, b)):02X}"


def lerp_color(start: RGB, end: RGB, t: float) -> RGB:
    """Linearly interpolate between two RGB colors."""
    ratio = max(0.0, min(1.0, t))
    return (
        round(start[0] + (end[0] - start[0]) * ratio),
        round(start[1] + (end[1] - start[1]) * ratio),
        round(start[2] + (end[2] - start[2]) * ratio),
    )


@lru_cache(maxsize=32)
def sample_gradient(colors: tuple[str, ...], steps: int) -> tuple[str, ...]:
    """Sample a multi-stop gradient into ``steps`` hex colors.

    Args:
        colors: Ordered gradient stop colors.
        steps: Number of colors to generate.

    Returns:
        Tuple of hex colors sized to ``steps`` (or empty when ``steps <= 0``).
    """
    empty: tuple[str, ...] = ()
    if steps <= 0:
        return empty
    if not colors:
        return empty
    if len(colors) == 1:
        return tuple(colors[0] for _ in range(steps))

    stops = [hex_to_rgb(color) for color in colors]
    if steps == 1:
        return (rgb_to_hex(stops[0]),)

    result: list[str] = []
    segments = len(stops) - 1
    for index in range(steps):
        position = index / max(steps - 1, 1)
        scaled = position * segments
        segment = min(int(scaled), segments - 1)
        local_t = scaled - segment
        result.append(rgb_to_hex(lerp_color(stops[segment], stops[segment + 1], local_t)))
    return tuple(result)


def gradient_markup_lines(
    lines: Sequence[str],
    *,
    colors: Sequence[str] = BERNSTEIN_GRADIENT,
    style: str = "bold",
) -> str:
    """Apply line-wise Rich markup gradient styling.

    Args:
        lines: Lines to colorize.
        colors: Gradient stops used for sampling.
        style: Rich style prefix applied to every line.

    Returns:
        Rich-markup string with each non-empty line styled separately.
    """
    palette = sample_gradient(tuple(colors), len(lines))
    styled: list[str] = []
    for index, line in enumerate(lines):
        if not line:
            styled.append("")
            continue
        styled.append(f"[{style} {palette[index]}]{escape(line)}[/]")
    return "\n".join(styled)


def role_color(role: str) -> str:
    """Return the preferred accent color for a role label."""
    return ROLE_COLORS.get(role.lower(), PALETTE.cyan)


def model_color(model_or_adapter: str) -> str:
    """Return a brand color for a model or adapter name."""
    normalized = model_or_adapter.lower()
    for key, color in MODEL_BRAND_COLORS.items():
        if key in normalized:
            return color
    return PALETTE.cyan


def status_color(status: str) -> str:
    """Return a status color for TUI labels and activity log lines."""
    return STATUS_COLORS.get(status.lower(), PALETTE.text)


def budget_color(percentage_used: float) -> str:
    """Return a budget color for usage bars and labels."""
    if percentage_used >= 0.95:
        return PALETTE.danger
    if percentage_used >= 0.80:
        return PALETTE.warning
    return PALETTE.success
