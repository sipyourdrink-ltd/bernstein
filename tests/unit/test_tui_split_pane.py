"""Tests for TUI-008: Split-pane view (tasks list + live agent log)."""

from __future__ import annotations

from bernstein.tui.split_pane import (
    SplitOrientation,
    SplitPaneConfig,
    SplitPaneState,
    build_split_layout_css,
    render_split_status,
)


class TestSplitPaneConfig:
    def test_default_config(self) -> None:
        config = SplitPaneConfig()
        assert config.orientation == SplitOrientation.HORIZONTAL
        assert config.ratio == 0.5
        assert config.enabled is False

    def test_custom_config(self) -> None:
        config = SplitPaneConfig(
            orientation=SplitOrientation.VERTICAL,
            ratio=0.7,
            enabled=True,
        )
        assert config.orientation == SplitOrientation.VERTICAL
        assert config.ratio == 0.7
        assert config.enabled is True


class TestSplitPaneState:
    def test_initial_state(self) -> None:
        state = SplitPaneState()
        assert state.enabled is False
        assert state.orientation == SplitOrientation.HORIZONTAL
        assert state.ratio == 0.5

    def test_toggle(self) -> None:
        state = SplitPaneState()
        assert state.toggle() is True
        assert state.enabled is True
        assert state.toggle() is False
        assert state.enabled is False

    def test_cycle_orientation(self) -> None:
        state = SplitPaneState()
        assert state.cycle_orientation() == SplitOrientation.VERTICAL
        assert state.cycle_orientation() == SplitOrientation.HORIZONTAL

    def test_set_ratio(self) -> None:
        state = SplitPaneState()
        state.set_ratio(0.7)
        assert state.ratio == 0.7

    def test_set_ratio_clamped_low(self) -> None:
        state = SplitPaneState()
        state.set_ratio(0.1)
        assert state.ratio == 0.2

    def test_set_ratio_clamped_high(self) -> None:
        state = SplitPaneState()
        state.set_ratio(0.95)
        assert state.ratio == 0.8

    def test_to_config(self) -> None:
        state = SplitPaneState()
        state.toggle()
        state.set_ratio(0.6)
        config = state.to_config()
        assert config.enabled is True
        assert config.ratio == 0.6
        assert config.orientation == SplitOrientation.HORIZONTAL

    def test_from_config(self) -> None:
        config = SplitPaneConfig(enabled=True, ratio=0.3, orientation=SplitOrientation.VERTICAL)
        state = SplitPaneState(config)
        assert state.enabled is True
        assert state.ratio == 0.3
        assert state.orientation == SplitOrientation.VERTICAL


class TestBuildSplitLayoutCss:
    def test_disabled(self) -> None:
        state = SplitPaneState()
        css = build_split_layout_css(state)
        assert "display: none" in css

    def test_horizontal_split(self) -> None:
        config = SplitPaneConfig(enabled=True, ratio=0.5)
        state = SplitPaneState(config)
        css = build_split_layout_css(state)
        assert "width: 50%" in css

    def test_vertical_split(self) -> None:
        config = SplitPaneConfig(
            enabled=True,
            ratio=0.6,
            orientation=SplitOrientation.VERTICAL,
        )
        state = SplitPaneState(config)
        css = build_split_layout_css(state)
        assert "height: 60%" in css

    def test_custom_ids(self) -> None:
        config = SplitPaneConfig(enabled=True)
        state = SplitPaneState(config)
        css = build_split_layout_css(state, primary_id="left", secondary_id="right")
        assert "#left" in css
        assert "#right" in css


class TestRenderSplitStatus:
    def test_disabled(self) -> None:
        state = SplitPaneState()
        text = render_split_status(state)
        assert "off" in text.plain

    def test_horizontal(self) -> None:
        config = SplitPaneConfig(enabled=True, orientation=SplitOrientation.HORIZONTAL)
        state = SplitPaneState(config)
        text = render_split_status(state)
        assert "H" in text.plain

    def test_vertical(self) -> None:
        config = SplitPaneConfig(enabled=True, orientation=SplitOrientation.VERTICAL)
        state = SplitPaneState(config)
        text = render_split_status(state)
        assert "V" in text.plain
