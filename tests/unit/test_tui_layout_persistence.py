"""Tests for TUI-016: Persistent layout customization."""

from __future__ import annotations

from pathlib import Path

import yaml

from bernstein.tui.layout_persistence import (
    LayoutConfig,
    _DEFAULT_PANELS,
    _REQUIRED_PANELS,
    load_layout,
    save_layout,
)


class TestLayoutConfigDefaults:
    def test_default_split_ratio(self) -> None:
        cfg = LayoutConfig()
        assert cfg.split_ratio == 0.5

    def test_default_split_disabled(self) -> None:
        cfg = LayoutConfig()
        assert cfg.split_enabled is False

    def test_default_orientation(self) -> None:
        cfg = LayoutConfig()
        assert cfg.orientation == "horizontal"

    def test_default_visible_panels(self) -> None:
        cfg = LayoutConfig()
        assert cfg.visible_panels == _DEFAULT_PANELS

    def test_frozen(self) -> None:
        """LayoutConfig must be immutable."""
        cfg = LayoutConfig()
        try:
            cfg.split_ratio = 0.7  # type: ignore[misc]
            raised = False
        except AttributeError:
            raised = True
        assert raised


class TestTogglePanel:
    def test_toggle_off(self) -> None:
        cfg = LayoutConfig(visible_panels=frozenset({"task-list", "agent-log"}))
        new = cfg.toggle_panel("agent-log")
        assert "agent-log" not in new.visible_panels
        assert "task-list" in new.visible_panels

    def test_toggle_on(self) -> None:
        cfg = LayoutConfig(visible_panels=frozenset({"task-list"}))
        new = cfg.toggle_panel("timeline")
        assert "timeline" in new.visible_panels

    def test_toggle_required_panel_noop(self) -> None:
        """Toggling task-list off must be silently ignored."""
        cfg = LayoutConfig(visible_panels=frozenset({"task-list", "agent-log"}))
        new = cfg.toggle_panel("task-list")
        assert "task-list" in new.visible_panels
        assert new == cfg

    def test_toggle_roundtrip(self) -> None:
        """Toggling a panel twice returns to original state."""
        cfg = LayoutConfig(visible_panels=frozenset({"task-list", "agent-log"}))
        toggled = cfg.toggle_panel("agent-log").toggle_panel("agent-log")
        assert toggled.visible_panels == cfg.visible_panels


class TestSaveLoadRoundtrip:
    def test_roundtrip_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "layout.yaml"
        original = LayoutConfig()
        save_layout(original, config_path=path)
        loaded = load_layout(config_path=path)
        assert loaded.split_ratio == original.split_ratio
        assert loaded.split_enabled == original.split_enabled
        assert loaded.orientation == original.orientation
        assert loaded.visible_panels == original.visible_panels

    def test_roundtrip_custom(self, tmp_path: Path) -> None:
        path = tmp_path / "layout.yaml"
        original = LayoutConfig(
            split_ratio=0.7,
            split_enabled=True,
            visible_panels=frozenset({"task-list", "timeline"}),
            orientation="vertical",
        )
        save_layout(original, config_path=path)
        loaded = load_layout(config_path=path)
        assert loaded.split_ratio == 0.7
        assert loaded.split_enabled is True
        assert loaded.orientation == "vertical"
        assert loaded.visible_panels == frozenset({"task-list", "timeline"})

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "layout.yaml"
        save_layout(LayoutConfig(), config_path=path)
        assert path.exists()


class TestLoadCorruptFile:
    def test_missing_file_returns_default(self, tmp_path: Path) -> None:
        path = tmp_path / "nonexistent.yaml"
        cfg = load_layout(config_path=path)
        assert cfg == LayoutConfig()

    def test_invalid_yaml_returns_default(self, tmp_path: Path) -> None:
        path = tmp_path / "layout.yaml"
        path.write_text("{[invalid yaml !!!!", encoding="utf-8")
        cfg = load_layout(config_path=path)
        assert cfg == LayoutConfig()

    def test_non_dict_yaml_returns_default(self, tmp_path: Path) -> None:
        path = tmp_path / "layout.yaml"
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        cfg = load_layout(config_path=path)
        assert cfg == LayoutConfig()

    def test_bad_ratio_clamped(self, tmp_path: Path) -> None:
        path = tmp_path / "layout.yaml"
        path.write_text(
            yaml.safe_dump({"split_ratio": 99.0}), encoding="utf-8"
        )
        cfg = load_layout(config_path=path)
        assert cfg.split_ratio == 0.8

    def test_bad_ratio_low_clamped(self, tmp_path: Path) -> None:
        path = tmp_path / "layout.yaml"
        path.write_text(
            yaml.safe_dump({"split_ratio": -1.0}), encoding="utf-8"
        )
        cfg = load_layout(config_path=path)
        assert cfg.split_ratio == 0.2

    def test_bad_ratio_string_uses_default(self, tmp_path: Path) -> None:
        path = tmp_path / "layout.yaml"
        path.write_text(
            yaml.safe_dump({"split_ratio": "not-a-number"}), encoding="utf-8"
        )
        cfg = load_layout(config_path=path)
        assert cfg.split_ratio == 0.5

    def test_bad_orientation_uses_default(self, tmp_path: Path) -> None:
        path = tmp_path / "layout.yaml"
        path.write_text(
            yaml.safe_dump({"orientation": "diagonal"}), encoding="utf-8"
        )
        cfg = load_layout(config_path=path)
        assert cfg.orientation == "horizontal"

    def test_bad_panels_uses_default(self, tmp_path: Path) -> None:
        path = tmp_path / "layout.yaml"
        path.write_text(
            yaml.safe_dump({"visible_panels": "not-a-list"}), encoding="utf-8"
        )
        cfg = load_layout(config_path=path)
        assert cfg.visible_panels == _DEFAULT_PANELS


class TestRequiredPanels:
    def test_task_list_always_present(self) -> None:
        assert "task-list" in _REQUIRED_PANELS

    def test_load_injects_required_panels(self, tmp_path: Path) -> None:
        """Even if task-list is absent from the file, it is added on load."""
        path = tmp_path / "layout.yaml"
        path.write_text(
            yaml.safe_dump({"visible_panels": ["agent-log"]}),
            encoding="utf-8",
        )
        cfg = load_layout(config_path=path)
        assert "task-list" in cfg.visible_panels
        assert "agent-log" in cfg.visible_panels
