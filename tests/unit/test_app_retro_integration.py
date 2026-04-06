"""Tests for retro-demoscene widget integration in the lightweight TUI."""

from __future__ import annotations

from bernstein.tui.crt_shader import CRTMode, CRTShader


def test_crt_shader_cycles():
    shader = CRTShader(CRTMode.OFF)
    assert shader.cycle_mode() == CRTMode.AMBER
    assert shader.cycle_mode() == CRTMode.GREEN
    assert shader.cycle_mode() == CRTMode.COOL_WHITE
    assert shader.cycle_mode() == CRTMode.OFF


def test_retro_widgets_importable():
    from bernstein.tui.oscilloscope import OscilloscopeWidget
    from bernstein.tui.plasma import PlasmaCanvas
    from bernstein.tui.tracker_view import TrackerView

    assert PlasmaCanvas is not None
    assert TrackerView is not None
    assert OscilloscopeWidget is not None
