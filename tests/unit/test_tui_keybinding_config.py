"""Tests for TUI-004: Configurable keybinding system with YAML key map."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.tui.keybinding_config import (
    EXTENDED_BINDINGS,
    KeyMapEntry,
    get_key_for_action,
    load_yaml_keybindings,
    resolve_all_bindings,
)

# --- YAML loading ---


class TestLoadYamlKeybindings:
    def test_loads_from_yaml(self, tmp_path: Path) -> None:
        """Loads keybinding overrides from a bernstein.yaml."""
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text(
            "keybindings:\n  quit: Q\n  refresh: F5\n",
            encoding="utf-8",
        )
        result = load_yaml_keybindings(yaml_file)
        assert result["quit"] == "Q"
        assert result["refresh"] == "F5"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """Returns empty dict when file doesn't exist."""
        result = load_yaml_keybindings(tmp_path / "nonexistent.yaml")
        assert result == {}

    def test_no_keybindings_section(self, tmp_path: Path) -> None:
        """Returns empty when YAML has no keybindings section."""
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text("server:\n  port: 8052\n", encoding="utf-8")
        result = load_yaml_keybindings(yaml_file)
        assert result == {}

    def test_skips_reserved_keys(self, tmp_path: Path) -> None:
        """Reserved keys in YAML are skipped."""
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text(
            "keybindings:\n  quit: ctrl+c\n  refresh: F5\n",
            encoding="utf-8",
        )
        result = load_yaml_keybindings(yaml_file)
        assert "quit" not in result
        assert result["refresh"] == "F5"

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        """Handles malformed YAML gracefully."""
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text("{{invalid yaml{{{", encoding="utf-8")
        result = load_yaml_keybindings(yaml_file)
        assert result == {}

    def test_non_dict_yaml(self, tmp_path: Path) -> None:
        """Handles YAML that's not a dict."""
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text("- item1\n- item2\n", encoding="utf-8")
        result = load_yaml_keybindings(yaml_file)
        assert result == {}


# --- Resolution ---


class TestResolveAllBindings:
    def test_returns_key_map_entries(self) -> None:
        """Resolves to a list of KeyMapEntry objects."""
        entries = resolve_all_bindings(
            yaml_path=Path("/nonexistent.yaml"),
            json_path=Path("/nonexistent.json"),
        )
        assert len(entries) > 0
        assert all(isinstance(e, KeyMapEntry) for e in entries)

    def test_includes_extended_bindings(self) -> None:
        """Extended bindings (new TUI features) are included."""
        entries = resolve_all_bindings(
            yaml_path=Path("/nonexistent.yaml"),
            json_path=Path("/nonexistent.json"),
        )
        actions = {e.action for e in entries}
        assert "copy_to_clipboard" in actions
        assert "toggle_split_pane" in actions
        assert "command_palette" in actions

    def test_yaml_overrides_defaults(self, tmp_path: Path) -> None:
        """YAML config overrides default keys."""
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text("keybindings:\n  quit: Q\n", encoding="utf-8")
        entries = resolve_all_bindings(
            yaml_path=yaml_file,
            json_path=Path("/nonexistent.json"),
        )
        quit_entry = next(e for e in entries if e.action == "quit")
        assert quit_entry.key == "Q"
        assert quit_entry.source == "yaml"

    def test_json_overrides_yaml(self, tmp_path: Path) -> None:
        """JSON overrides take priority over YAML."""
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text("keybindings:\n  quit: Q\n", encoding="utf-8")
        json_file = tmp_path / "keybindings.json"
        json_file.write_text(
            json.dumps({"quit": "X"}),
            encoding="utf-8",
        )
        entries = resolve_all_bindings(yaml_path=yaml_file, json_path=json_file)
        quit_entry = next(e for e in entries if e.action == "quit")
        assert quit_entry.key == "X"
        assert quit_entry.source == "json"

    def test_default_source_tracking(self) -> None:
        """Unmodified bindings have source='default'."""
        entries = resolve_all_bindings(
            yaml_path=Path("/nonexistent.yaml"),
            json_path=Path("/nonexistent.json"),
        )
        for entry in entries:
            assert entry.source == "default"


# --- get_key_for_action ---


class TestGetKeyForAction:
    def test_finds_existing_action(self) -> None:
        """Returns key for a known action."""
        entries = resolve_all_bindings(
            yaml_path=Path("/nonexistent.yaml"),
            json_path=Path("/nonexistent.json"),
        )
        key = get_key_for_action("quit", entries)
        assert key is not None
        assert key == "q"

    def test_returns_none_for_unknown(self) -> None:
        """Returns None for an unknown action."""
        entries = resolve_all_bindings(
            yaml_path=Path("/nonexistent.yaml"),
            json_path=Path("/nonexistent.json"),
        )
        assert get_key_for_action("nonexistent_action", entries) is None


# --- EXTENDED_BINDINGS ---


class TestExtendedBindings:
    def test_has_clipboard_binding(self) -> None:
        actions = {b.action for b in EXTENDED_BINDINGS}
        assert "copy_to_clipboard" in actions

    def test_has_split_pane_binding(self) -> None:
        actions = {b.action for b in EXTENDED_BINDINGS}
        assert "toggle_split_pane" in actions

    def test_has_command_palette_binding(self) -> None:
        actions = {b.action for b in EXTENDED_BINDINGS}
        assert "command_palette" in actions

    def test_has_theme_binding(self) -> None:
        actions = {b.action for b in EXTENDED_BINDINGS}
        assert "cycle_theme" in actions

    def test_has_accessibility_binding(self) -> None:
        actions = {b.action for b in EXTENDED_BINDINGS}
        assert "toggle_accessibility" in actions
