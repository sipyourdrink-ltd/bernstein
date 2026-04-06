"""Tests for the braille oscilloscope / VU meter widget (TUI-014)."""

from __future__ import annotations

from bernstein.cli.terminal_caps import TerminalCaps
from bernstein.tui.accessibility import AccessibilityConfig, AccessibilityLevel
from bernstein.tui.oscilloscope import (
    _DOT_BITS,
    _SPARKLINE_CHARS,
    BrailleCanvas,
    OscilloscopeWidget,
    ScopeChannel,
    _color_for_agent,
)

# ── BrailleCanvas ────────────────────────────────────────────────────────


def test_braille_canvas_empty() -> None:
    """New canvas renders all empty braille (U+2800)."""
    canvas = BrailleCanvas(3, 2)
    for row in range(canvas.height):
        rendered = canvas.render_row(row)
        assert len(rendered) == 3
        assert all(ch == "\u2800" for ch in rendered)


def test_braille_canvas_set_dot() -> None:
    """Setting individual dots produces the correct bit patterns."""
    canvas = BrailleCanvas(1, 1)

    # (0,0) -> 0x01
    canvas.set_dot(0, 0)
    assert canvas.get_cell(0, 0) == 0x01

    canvas.clear()

    # (1,0) -> 0x08
    canvas.set_dot(1, 0)
    assert canvas.get_cell(0, 0) == 0x08

    canvas.clear()

    # (0,3) -> 0x40
    canvas.set_dot(0, 3)
    assert canvas.get_cell(0, 0) == 0x40


def test_braille_canvas_full_cell() -> None:
    """All 8 dots set in a cell produce chr(0x28FF)."""
    canvas = BrailleCanvas(1, 1)
    for y in range(4):
        for x in range(2):
            canvas.set_dot(x, y)
    assert canvas.get_cell(0, 0) == 0xFF
    assert canvas.render_row(0) == chr(0x28FF)


def test_braille_canvas_clear() -> None:
    """Clear resets all cells to zero."""
    canvas = BrailleCanvas(2, 2)
    canvas.set_dot(0, 0)
    canvas.set_dot(3, 7)
    canvas.clear()
    for row in range(canvas.height):
        for col in range(canvas.width):
            assert canvas.get_cell(col, row) == 0


def test_braille_draw_line_horizontal() -> None:
    """Horizontal line sets dots across multiple cells."""
    canvas = BrailleCanvas(4, 1)
    canvas.draw_line(0, 0, 7, 0)  # full width at row 0
    # Every dot column at y=0 should be set.
    for x in range(8):
        cell_x = x // 2
        dot_x = x % 2
        mask = canvas.get_cell(cell_x, 0)
        assert mask & _DOT_BITS[0][dot_x], f"dot at x={x} not set"


def test_braille_draw_line_vertical() -> None:
    """Vertical line sets dots down a single column."""
    canvas = BrailleCanvas(1, 2)
    canvas.draw_line(0, 0, 0, 7)  # full height at col 0
    for y in range(8):
        cell_y = y // 4
        dot_y = y % 4
        mask = canvas.get_cell(0, cell_y)
        assert mask & _DOT_BITS[dot_y][0], f"dot at y={y} not set"


def test_braille_draw_line_diagonal() -> None:
    """Diagonal line sets intermediate dots (not just endpoints)."""
    canvas = BrailleCanvas(2, 2)
    canvas.draw_line(0, 0, 3, 7)
    # Start and end must be set.
    assert canvas.get_cell(0, 0) & _DOT_BITS[0][0]  # (0,0)
    assert canvas.get_cell(1, 1) & _DOT_BITS[3][1]  # (3,7)
    # Both endpoint cells should have multiple dots from the line
    # passing through them (not just a single corner dot).
    assert bin(canvas.get_cell(0, 0)).count("1") > 1, "line should set multiple dots in start cell"
    assert bin(canvas.get_cell(1, 1)).count("1") > 1, "line should set multiple dots in end cell"


def test_braille_canvas_out_of_bounds_ignored() -> None:
    """Setting dots outside canvas bounds does nothing."""
    canvas = BrailleCanvas(1, 1)
    canvas.set_dot(-1, 0)
    canvas.set_dot(0, -1)
    canvas.set_dot(2, 0)
    canvas.set_dot(0, 4)
    assert canvas.get_cell(0, 0) == 0


def test_braille_render_row_out_of_bounds() -> None:
    """Rendering an invalid row returns empty string."""
    canvas = BrailleCanvas(2, 2)
    assert canvas.render_row(-1) == ""
    assert canvas.render_row(2) == ""


# ── ScopeChannel ─────────────────────────────────────────────────────────


def test_scope_channel_creation() -> None:
    """Channel initializes with empty deque and zero peak."""
    ch = ScopeChannel(agent_id="a1", role="backend", color="#ff0000")
    assert ch.agent_id == "a1"
    assert ch.role == "backend"
    assert len(ch.samples) == 0
    assert ch.peak == 0.0
    assert ch.peak_age == 0


def test_scope_channel_deque_maxlen() -> None:
    """Default deque has maxlen=120."""
    ch = ScopeChannel(agent_id="a1", role="qa", color="#00ff00")
    assert ch.samples.maxlen == 120


# ── OscilloscopeWidget ───────────────────────────────────────────────────

# Helper: terminal caps with braille support.
_BRAILLE_CAPS = TerminalCaps(
    is_tty=True,
    supports_truecolor=True,
    supports_256color=True,
    supports_kitty=False,
    supports_iterm2=False,
    supports_sixel=False,
    term_width=80,
    term_height=24,
)

# Helper: terminal caps without braille (non-TTY).
_NO_BRAILLE_CAPS = TerminalCaps.null()

# Helper: default accessibility off.
_A11Y_OFF = AccessibilityConfig(level=AccessibilityLevel.OFF)

# Helper: accessibility with no_animations.
_A11Y_NO_ANIM = AccessibilityConfig(
    level=AccessibilityLevel.BASIC,
    no_animations=True,
)

# Helper: accessibility with no_unicode.
_A11Y_NO_UNICODE = AccessibilityConfig(
    level=AccessibilityLevel.BASIC,
    no_unicode=True,
)


def test_scope_add_samples() -> None:
    """Samples appear in channel deque via add_samples."""
    w = OscilloscopeWidget(caps=_BRAILLE_CAPS, a11y=_A11Y_OFF)
    w.update_agents([{"session_id": "s1", "role": "backend"}])
    w.add_samples({"s1": 5.0})
    w.add_samples({"s1": 10.0})
    ch = w.channels[0]
    assert list(ch.samples) == [5.0, 10.0]


def test_scope_sample_window() -> None:
    """Deque maxlen is enforced (samples beyond window are dropped)."""
    window = 10
    w = OscilloscopeWidget(
        sample_window=window,
        caps=_BRAILLE_CAPS,
        a11y=_A11Y_OFF,
    )
    w.update_agents([{"session_id": "s1", "role": "qa"}])
    for i in range(20):
        w.add_samples({"s1": float(i)})
    ch = w.channels[0]
    assert len(ch.samples) == window
    assert ch.samples[0] == 10.0  # oldest surviving sample


def test_scope_render_no_channels() -> None:
    """Empty oscilloscope shows 'No agent activity'."""
    w = OscilloscopeWidget(caps=_BRAILLE_CAPS, a11y=_A11Y_OFF)
    rendered = w.render()
    assert "No agent activity" in rendered.plain


def test_scope_render_with_data() -> None:
    """With data, rendered output contains braille characters."""
    w = OscilloscopeWidget(caps=_BRAILLE_CAPS, a11y=_A11Y_OFF)
    w.update_agents([{"session_id": "s1", "role": "backend"}])
    for i in range(20):
        w.add_samples({"s1": float(i % 5)})
    rendered = w.render()
    text = rendered.plain
    # Should contain at least one non-empty braille character.
    has_braille = any("\u2800" <= ch <= "\u28FF" for ch in text)
    assert has_braille, "expected braille characters in output"


def test_scope_peak_hold() -> None:
    """Peak tracks highest recent value and decays after hold period."""
    w = OscilloscopeWidget(
        peak_hold_ticks=3,
        caps=_BRAILLE_CAPS,
        a11y=_A11Y_OFF,
    )
    w.update_agents([{"session_id": "s1", "role": "qa"}])

    # Push a high value.
    w.add_samples({"s1": 100.0})
    assert w.channels[0].peak == 100.0

    # Push lower values; peak should hold during hold period.
    for _ in range(3):
        w.add_samples({"s1": 1.0})
    assert w.channels[0].peak == 100.0

    # One more tick exceeds hold period, triggering decay.
    w.add_samples({"s1": 2.0})
    # Peak should have decayed to the max of the last 10 samples
    # (which is 100.0 since it's still in the window). Push enough
    # low values to flush the 100.0 out of the recent-10 window.
    for _ in range(12):
        w.add_samples({"s1": 2.0})
    assert w.channels[0].peak < 100.0


def test_scope_color_deterministic() -> None:
    """Same agent_id always maps to the same color."""
    c1 = _color_for_agent("agent-42", OscilloscopeWidget.CHANNEL_COLORS)
    c2 = _color_for_agent("agent-42", OscilloscopeWidget.CHANNEL_COLORS)
    c3 = _color_for_agent("agent-99", OscilloscopeWidget.CHANNEL_COLORS)
    assert c1 == c2
    # Different agents can get different colors (not guaranteed, but
    # exceedingly likely for these two strings).
    assert isinstance(c3, str) and c3.startswith("#")


def test_scope_max_channels() -> None:
    """Only max_channels agents are rendered even if more are provided."""
    w = OscilloscopeWidget(
        max_channels=4,
        caps=_BRAILLE_CAPS,
        a11y=_A11Y_OFF,
    )
    agents = [{"session_id": f"s{i}", "role": "backend"} for i in range(8)]
    w.update_agents(agents)
    assert len(w.channels) == 4


def test_scope_accessibility_fallback_no_animations() -> None:
    """no_animations accessibility mode renders static sparkline bars."""
    w = OscilloscopeWidget(caps=_BRAILLE_CAPS, a11y=_A11Y_NO_ANIM)
    w.update_agents([{"session_id": "s1", "role": "backend"}])
    w.add_samples({"s1": 5.0})
    rendered = w.render()
    text = rendered.plain
    # Should contain a sparkline character (block element).
    has_sparkline = any(ch in _SPARKLINE_CHARS for ch in text)
    assert has_sparkline, f"expected sparkline chars in: {text!r}"


def test_scope_accessibility_fallback_no_unicode() -> None:
    """no_unicode accessibility mode renders ASCII bars."""
    w = OscilloscopeWidget(caps=_BRAILLE_CAPS, a11y=_A11Y_NO_UNICODE)
    w.update_agents([{"session_id": "s1", "role": "backend"}])
    w.add_samples({"s1": 5.0})
    rendered = w.render()
    text = rendered.plain
    # ASCII bars use | and - characters.
    assert "|" in text or "-" in text


def test_scope_sparkline_fallback_no_braille() -> None:
    """Non-TTY terminal falls back to sparkline rendering."""
    w = OscilloscopeWidget(caps=_NO_BRAILLE_CAPS, a11y=_A11Y_OFF)
    w.update_agents([{"session_id": "s1", "role": "qa"}])
    for _ in range(5):
        w.add_samples({"s1": 3.0})
    rendered = w.render()
    text = rendered.plain
    has_sparkline = any(ch in _SPARKLINE_CHARS for ch in text)
    assert has_sparkline, f"expected sparkline fallback in: {text!r}"


def test_scope_update_agents_removes_departed() -> None:
    """Channels for removed agents are cleaned up."""
    w = OscilloscopeWidget(caps=_BRAILLE_CAPS, a11y=_A11Y_OFF)
    w.update_agents([
        {"session_id": "s1", "role": "backend"},
        {"session_id": "s2", "role": "qa"},
    ])
    assert len(w.channels) == 2

    # Remove s2.
    w.update_agents([{"session_id": "s1", "role": "backend"}])
    assert len(w.channels) == 1
    assert w.channels[0].agent_id == "s1"


def test_scope_channel_preserves_samples_on_update() -> None:
    """Existing channels keep their sample history across updates."""
    w = OscilloscopeWidget(caps=_BRAILLE_CAPS, a11y=_A11Y_OFF)
    w.update_agents([{"session_id": "s1", "role": "backend"}])
    w.add_samples({"s1": 42.0})

    # Re-update with same agent set.
    w.update_agents([{"session_id": "s1", "role": "backend"}])
    assert len(w.channels) == 1
    assert 42.0 in w.channels[0].samples
