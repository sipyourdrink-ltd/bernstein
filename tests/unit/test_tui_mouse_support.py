"""Tests for TUI-017: Mouse support for panel interaction."""

from __future__ import annotations

from pathlib import Path

from bernstein.tui.mouse_support import MouseConfig, load_mouse_config


class TestMouseConfig:
    def test_default_enabled(self) -> None:
        config = MouseConfig()
        assert config.click_to_select is True
        assert config.scroll_enabled is True
        assert config.drag_resize is True

    def test_load_missing_config(self) -> None:
        config = load_mouse_config(Path("/nonexistent.yaml"))
        assert config == MouseConfig()

    def test_load_from_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text(
            "mouse:\n  click_to_select: false\n  scroll_enabled: true\n  drag_resize: false\n",
            encoding="utf-8",
        )
        config = load_mouse_config(yaml_file)
        assert config.click_to_select is False
        assert config.scroll_enabled is True
        assert config.drag_resize is False

    def test_load_no_mouse_section(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text("server:\n  port: 8052\n", encoding="utf-8")
        config = load_mouse_config(yaml_file)
        assert config == MouseConfig()
