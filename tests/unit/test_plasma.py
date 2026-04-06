"""Tests for the demoscene plasma canvas widget."""
# pyright: reportUnknownMemberType=false

from __future__ import annotations

import pytest
from rich.text import Text

from bernstein.tui.plasma import (
    _SIN_LUT,
    PaletteMode,
    PlasmaCanvas,
    _hsv_to_rgb,
)

# ── Sine LUT tests ───────────────────────────────────────────────────────


def test_sin_lut_length() -> None:
    """LUT has exactly 256 entries."""
    assert len(_SIN_LUT) == 256


def test_sin_lut_values() -> None:
    """Key sine positions: sin(0)~0, sin(pi/2)~1, sin(pi)~0, sin(3pi/2)~-1."""
    assert _SIN_LUT[0] == pytest.approx(0.0, abs=0.03)
    assert _SIN_LUT[64] == pytest.approx(1.0, abs=0.03)
    assert _SIN_LUT[128] == pytest.approx(0.0, abs=0.03)
    assert _SIN_LUT[192] == pytest.approx(-1.0, abs=0.03)


# ── Plasma value tests ───────────────────────────────────────────────────


def test_plasma_value_range() -> None:
    """Plasma output stays in [-4, +4] for various inputs."""
    for x in range(0, 50, 10):
        for y in range(0, 50, 10):
            for t in [0.0, 1.0, 5.0, 10.0]:
                v = PlasmaCanvas._plasma_value(x, y, t)
                assert -4.0 <= v <= 4.0, f"Out of range at ({x}, {y}, {t}): {v}"


# ── HSV to RGB tests ─────────────────────────────────────────────────────


def test_hsv_to_rgb_red() -> None:
    """H=0, S=1, V=1 produces approximately (255, 0, 0)."""
    r, g, b = _hsv_to_rgb(0.0, 1.0, 1.0)
    assert r == 255
    assert g == 0
    assert b == 0


def test_hsv_to_rgb_green() -> None:
    """H=120, S=1, V=1 produces approximately (0, 255, 0)."""
    r, g, b = _hsv_to_rgb(120.0, 1.0, 1.0)
    assert r == 0
    assert g == 255
    assert b == 0


def test_hsv_to_rgb_blue() -> None:
    """H=240, S=1, V=1 produces approximately (0, 0, 255)."""
    r, g, b = _hsv_to_rgb(240.0, 1.0, 1.0)
    assert r == 0
    assert g == 0
    assert b == 255


# ── Render tests ──────────────────────────────────────────────────────────


def test_plasma_render_returns_text() -> None:
    """render() returns a Rich Text object."""
    canvas = PlasmaCanvas()
    # Inject caps so rendering works without a live terminal
    from bernstein.cli.terminal_caps import TerminalCaps

    canvas._caps = TerminalCaps(
        is_tty=True,
        supports_truecolor=True,
        supports_256color=True,
        supports_kitty=False,
        supports_iterm2=False,
        supports_sixel=False,
        term_width=40,
        term_height=12,
    )
    canvas._a11y = None
    result = canvas.render()
    assert isinstance(result, Text)


def test_plasma_render_uses_half_blocks() -> None:
    """Output contains half-block characters when truecolor is available."""
    canvas = PlasmaCanvas()
    from bernstein.cli.terminal_caps import TerminalCaps

    canvas._caps = TerminalCaps(
        is_tty=True,
        supports_truecolor=True,
        supports_256color=True,
        supports_kitty=False,
        supports_iterm2=False,
        supports_sixel=False,
        term_width=40,
        term_height=12,
    )
    canvas._a11y = None
    result = canvas.render()
    assert "\u2580" in result.plain


# ── Palette cycling ───────────────────────────────────────────────────────


def test_plasma_cycle_palette() -> None:
    """Cycles NEON -> FIRE -> OCEAN -> ACID -> NEON."""
    canvas = PlasmaCanvas()
    assert canvas.palette == PaletteMode.NEON

    assert canvas.cycle_palette() == PaletteMode.FIRE
    assert canvas.palette == PaletteMode.FIRE

    assert canvas.cycle_palette() == PaletteMode.OCEAN
    assert canvas.palette == PaletteMode.OCEAN

    assert canvas.cycle_palette() == PaletteMode.ACID
    assert canvas.palette == PaletteMode.ACID

    assert canvas.cycle_palette() == PaletteMode.NEON
    assert canvas.palette == PaletteMode.NEON


# ── Activity modulation ──────────────────────────────────────────────────


def test_plasma_activity_modulation() -> None:
    """Speed changes with agent count."""
    canvas = PlasmaCanvas()

    canvas.update_activity(0)
    assert canvas.speed_mult == pytest.approx(0.3)

    canvas.update_activity(1)
    assert canvas.speed_mult == pytest.approx(0.6)

    canvas.update_activity(2)
    assert canvas.speed_mult == pytest.approx(0.6)

    canvas.update_activity(3)
    assert canvas.speed_mult == pytest.approx(1.0)

    canvas.update_activity(4)
    assert canvas.speed_mult == pytest.approx(1.0)

    canvas.update_activity(5)
    assert canvas.speed_mult == pytest.approx(1.5)

    canvas.update_activity(10)
    assert canvas.speed_mult == pytest.approx(1.5)


# ── Accessibility: frozen mode ────────────────────────────────────────────


def test_plasma_frozen_when_no_animations() -> None:
    """no_animations config prevents timer start."""
    from bernstein.tui.accessibility import AccessibilityConfig, AccessibilityLevel

    canvas = PlasmaCanvas()
    # Simulate on_mount with accessibility enabled
    from bernstein.cli.terminal_caps import TerminalCaps

    canvas._caps = TerminalCaps(
        is_tty=True,
        supports_truecolor=True,
        supports_256color=True,
        supports_kitty=False,
        supports_iterm2=False,
        supports_sixel=False,
        term_width=40,
        term_height=12,
    )
    canvas._a11y = AccessibilityConfig.from_level(AccessibilityLevel.FULL)

    # Timer should not be set when no_animations is True
    assert canvas._a11y.no_animations is True
    assert canvas._timer is None
