"""Tests for TUI-013: Accessibility mode."""

from __future__ import annotations

import os
from unittest.mock import patch

from bernstein.tui.accessibility import (
    AccessibilityConfig,
    AccessibilityLevel,
    accessible_keybinding_label,
    accessible_status_label,
    detect_accessibility,
    make_announcement,
    render_accessible_progress,
    replace_unicode,
)


class TestAccessibilityLevel:
    def test_values(self) -> None:
        assert AccessibilityLevel.OFF.value == "off"
        assert AccessibilityLevel.BASIC.value == "basic"
        assert AccessibilityLevel.FULL.value == "full"


class TestAccessibilityConfig:
    def test_off_defaults(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.OFF)
        assert config.screen_reader is False
        assert config.high_contrast is False
        assert config.no_unicode is False

    def test_basic_config(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.BASIC)
        assert config.no_unicode is True
        assert config.verbose_labels is True
        assert config.screen_reader is False

    def test_full_config(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.FULL)
        assert config.screen_reader is True
        assert config.high_contrast is True
        assert config.no_animations is True
        assert config.no_unicode is True
        assert config.verbose_labels is True
        assert config.announce_changes is True


class TestDetectAccessibility:
    @patch.dict(os.environ, {"BERNSTEIN_ACCESSIBILITY": "full"}, clear=False)
    def test_explicit_full(self) -> None:
        assert detect_accessibility() == AccessibilityLevel.FULL

    @patch.dict(os.environ, {"BERNSTEIN_ACCESSIBILITY": "basic"}, clear=False)
    def test_explicit_basic(self) -> None:
        assert detect_accessibility() == AccessibilityLevel.BASIC

    @patch.dict(os.environ, {"BERNSTEIN_ACCESSIBILITY": "off"}, clear=False)
    def test_explicit_off(self) -> None:
        assert detect_accessibility() == AccessibilityLevel.OFF

    @patch.dict(os.environ, {"BERNSTEIN_ACCESSIBILITY": "", "ORCA_RUNNING": "1"}, clear=False)
    def test_orca_detection(self) -> None:
        assert detect_accessibility() == AccessibilityLevel.FULL

    @patch.dict(os.environ, {"BERNSTEIN_ACCESSIBILITY": "", "NVDA_RUNNING": "1"}, clear=False)
    def test_nvda_detection(self) -> None:
        assert detect_accessibility() == AccessibilityLevel.FULL

    @patch.dict(
        os.environ,
        {"BERNSTEIN_ACCESSIBILITY": "", "REDUCE_MOTION": "true"},
        clear=False,
    )
    def test_reduce_motion(self) -> None:
        result = detect_accessibility()
        # REDUCE_MOTION triggers BASIC unless overridden by a screen reader env var
        assert result in (AccessibilityLevel.BASIC, AccessibilityLevel.FULL)

    @patch.dict(
        os.environ,
        {
            "BERNSTEIN_ACCESSIBILITY": "",
            "ORCA_RUNNING": "",
            "NVDA_RUNNING": "",
            "JAWS_RUNNING": "",
            "VOICEOVER_RUNNING": "",
            "REDUCE_MOTION": "",
        },
        clear=False,
    )
    def test_default_off(self) -> None:
        assert detect_accessibility() == AccessibilityLevel.OFF


class TestReplaceUnicode:
    def test_no_config(self) -> None:
        assert replace_unicode("\u25cf running") == "\u25cf running"

    def test_off_config(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.OFF)
        assert replace_unicode("\u25cf running", config) == "\u25cf running"

    def test_basic_replaces_circles(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.BASIC)
        result = replace_unicode("\u25cf done", config)
        assert "[*]" in result

    def test_replaces_check_mark(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.FULL)
        result = replace_unicode("\u2713 passed", config)
        assert "[OK]" in result

    def test_replaces_x_mark(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.FULL)
        result = replace_unicode("\u2717 failed", config)
        assert "[X]" in result

    def test_replaces_warning(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.FULL)
        result = replace_unicode("\u26a0 warning", config)
        assert "[!]" in result

    def test_replaces_progress_bars(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.FULL)
        result = replace_unicode("\u2588\u2588\u2591\u2591", config)
        assert "#" in result
        assert "-" in result


class TestAccessibleStatusLabel:
    def test_normal_mode(self) -> None:
        assert accessible_status_label("in_progress") == "in_progress"

    def test_verbose_mode(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.BASIC)
        assert accessible_status_label("in_progress", config) == "IN PROGRESS"

    def test_done_label(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.FULL)
        assert accessible_status_label("done", config) == "DONE"

    def test_unknown_status(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.FULL)
        result = accessible_status_label("custom_status", config)
        assert result == "CUSTOM_STATUS"


class TestRenderAccessibleProgress:
    def test_normal_mode(self) -> None:
        text = render_accessible_progress(50.0, width=10)
        assert len(text.plain) > 0

    def test_accessible_mode(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.FULL)
        text = render_accessible_progress(50.0, width=10, config=config)
        plain = text.plain
        assert "#" in plain
        assert "-" in plain
        assert "50%" in plain

    def test_zero_percent(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.FULL)
        text = render_accessible_progress(0.0, width=10, config=config)
        assert "0%" in text.plain

    def test_hundred_percent(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.FULL)
        text = render_accessible_progress(100.0, width=10, config=config)
        assert "100%" in text.plain


class TestMakeAnnouncement:
    def test_disabled(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.OFF)
        assert make_announcement("test", config) is None

    def test_basic_disabled(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.BASIC)
        assert make_announcement("test", config) is None

    def test_full_enabled(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.FULL)
        result = make_announcement("Task completed", config)
        assert result is not None
        assert "Announcement" in result
        assert "Task completed" in result


class TestAccessibleKeybindingLabel:
    def test_normal_mode(self) -> None:
        assert accessible_keybinding_label("ctrl+p") == "ctrl+p"

    def test_verbose_mode(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.FULL)
        result = accessible_keybinding_label("ctrl+p", config)
        assert "Control+p" in result

    def test_single_key_verbose(self) -> None:
        config = AccessibilityConfig.from_level(AccessibilityLevel.FULL)
        result = accessible_keybinding_label("q", config)
        assert "key" in result.lower()
