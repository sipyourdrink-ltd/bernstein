"""Tests for retro-demoscene widget integration in the dashboard."""

from __future__ import annotations

from bernstein.tui.crt_shader import CRTMode, CRTShader


def test_crt_shader_integration():
    shader = CRTShader(CRTMode.OFF)
    mode = shader.cycle_mode()
    assert mode == CRTMode.AMBER
    assert shader.active


def test_retro_widgets_importable():
    from bernstein.tui.oscilloscope import OscilloscopeWidget
    from bernstein.tui.plasma import PlasmaCanvas
    from bernstein.tui.tracker_view import TrackerView

    assert PlasmaCanvas is not None
    assert TrackerView is not None
    assert OscilloscopeWidget is not None
