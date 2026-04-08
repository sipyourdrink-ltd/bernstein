"""Tests for TUI-011: Dark/light theme support."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from bernstein.tui.themes import (
    DARK_THEME,
    HIGH_CONTRAST_THEME,
    LIGHT_THEME,
    THEMES,
    ThemeColors,
    ThemeMode,
    cycle_theme,
    detect_terminal_theme,
    generate_theme_css,
    get_theme,
    load_theme_config,
    save_theme_config,
    theme_color,
)


class TestThemeMode:
    def test_all_modes_have_themes(self) -> None:
        """All non-auto modes have corresponding theme definitions."""
        for mode in ThemeMode:
            if mode != ThemeMode.AUTO:
                assert mode in THEMES


class TestThemeColors:
    def test_dark_theme_has_all_fields(self) -> None:
        assert DARK_THEME.background
        assert DARK_THEME.foreground
        assert DARK_THEME.primary
        assert DARK_THEME.success
        assert DARK_THEME.warning
        assert DARK_THEME.error

    def test_light_theme_has_all_fields(self) -> None:
        assert LIGHT_THEME.background
        assert LIGHT_THEME.foreground
        assert LIGHT_THEME.primary

    def test_high_contrast_theme(self) -> None:
        assert HIGH_CONTRAST_THEME.background == "#000000"
        assert HIGH_CONTRAST_THEME.foreground == "#ffffff"

    def test_themes_are_distinct(self) -> None:
        assert DARK_THEME.background != LIGHT_THEME.background
        assert DARK_THEME.foreground != LIGHT_THEME.foreground


class TestDetectTerminalTheme:
    @patch.dict(os.environ, {"BERNSTEIN_THEME": "light"}, clear=False)
    def test_explicit_light(self) -> None:
        assert detect_terminal_theme() == ThemeMode.LIGHT

    @patch.dict(os.environ, {"BERNSTEIN_THEME": "dark"}, clear=False)
    def test_explicit_dark(self) -> None:
        assert detect_terminal_theme() == ThemeMode.DARK

    @patch.dict(os.environ, {"BERNSTEIN_THEME": "high_contrast"}, clear=False)
    def test_explicit_high_contrast(self) -> None:
        assert detect_terminal_theme() == ThemeMode.HIGH_CONTRAST

    @patch.dict(os.environ, {"COLORFGBG": "0;15", "BERNSTEIN_THEME": ""}, clear=False)
    def test_colorfgbg_light(self) -> None:
        assert detect_terminal_theme() == ThemeMode.LIGHT

    @patch.dict(os.environ, {"COLORFGBG": "15;0", "BERNSTEIN_THEME": ""}, clear=False)
    def test_colorfgbg_dark(self) -> None:
        assert detect_terminal_theme() == ThemeMode.DARK

    @patch.dict(os.environ, {"BERNSTEIN_THEME": "", "COLORFGBG": ""}, clear=False)
    def test_default_is_dark(self) -> None:
        assert detect_terminal_theme() == ThemeMode.DARK


class TestGetTheme:
    def test_get_dark(self) -> None:
        assert get_theme(ThemeMode.DARK) == DARK_THEME

    def test_get_light(self) -> None:
        assert get_theme(ThemeMode.LIGHT) == LIGHT_THEME

    def test_get_high_contrast(self) -> None:
        assert get_theme(ThemeMode.HIGH_CONTRAST) == HIGH_CONTRAST_THEME

    def test_auto_returns_theme(self) -> None:
        theme = get_theme(ThemeMode.AUTO)
        assert isinstance(theme, ThemeColors)

    def test_none_returns_theme(self) -> None:
        theme = get_theme(None)
        assert isinstance(theme, ThemeColors)


class TestCycleTheme:
    def test_dark_to_light(self) -> None:
        assert cycle_theme(ThemeMode.DARK) == ThemeMode.LIGHT

    def test_light_to_high_contrast(self) -> None:
        assert cycle_theme(ThemeMode.LIGHT) == ThemeMode.HIGH_CONTRAST

    def test_high_contrast_to_dark(self) -> None:
        assert cycle_theme(ThemeMode.HIGH_CONTRAST) == ThemeMode.DARK

    def test_auto_resolves_and_cycles(self) -> None:
        result = cycle_theme(ThemeMode.AUTO)
        # AUTO resolves to detected (likely DARK), then cycles
        assert result in (ThemeMode.DARK, ThemeMode.LIGHT, ThemeMode.HIGH_CONTRAST)


class TestGenerateThemeCss:
    def test_dark_theme_css(self) -> None:
        css = generate_theme_css(DARK_THEME)
        assert "Screen" in css
        assert DARK_THEME.background in css

    def test_light_theme_css(self) -> None:
        css = generate_theme_css(LIGHT_THEME)
        assert LIGHT_THEME.background in css

    def test_status_classes(self) -> None:
        css = generate_theme_css(DARK_THEME)
        assert ".status-done" in css
        assert ".status-failed" in css


class TestThemeColor:
    def test_known_role(self) -> None:
        assert theme_color(DARK_THEME, "success") == DARK_THEME.success

    def test_unknown_role(self) -> None:
        assert theme_color(DARK_THEME, "nonexistent") == DARK_THEME.foreground


class TestLoadSaveThemeConfig:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        config = tmp_path / "tui_config.json"
        save_theme_config(ThemeMode.LIGHT, config_path=config)
        loaded = load_theme_config(config_path=config)
        assert loaded == ThemeMode.LIGHT

    def test_save_and_load_high_contrast(self, tmp_path: Path) -> None:
        config = tmp_path / "tui_config.json"
        save_theme_config(ThemeMode.HIGH_CONTRAST, config_path=config)
        loaded = load_theme_config(config_path=config)
        assert loaded == ThemeMode.HIGH_CONTRAST

    def test_load_missing_file_returns_auto(self, tmp_path: Path) -> None:
        config = tmp_path / "nonexistent.json"
        loaded = load_theme_config(config_path=config)
        assert loaded == ThemeMode.AUTO

    def test_load_corrupt_file_returns_auto(self, tmp_path: Path) -> None:
        config = tmp_path / "tui_config.json"
        config.write_text("{invalid json", encoding="utf-8")
        loaded = load_theme_config(config_path=config)
        assert loaded == ThemeMode.AUTO

    def test_save_preserves_other_keys(self, tmp_path: Path) -> None:
        config = tmp_path / "tui_config.json"
        config.write_text(json.dumps({"other_key": "value"}), encoding="utf-8")
        save_theme_config(ThemeMode.DARK, config_path=config)
        data = json.loads(config.read_text())
        assert data["other_key"] == "value"
        assert data["theme"] == "dark"

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        config = tmp_path / "nested" / "dir" / "tui_config.json"
        save_theme_config(ThemeMode.DARK, config_path=config)
        assert config.exists()
        assert json.loads(config.read_text())["theme"] == "dark"
