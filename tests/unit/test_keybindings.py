"""Tests for keybindings — customizable keybinding system for TUI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.keybindings import (
    DEFAULT_BINDINGS,
    RESERVED_KEYS,
    KeyAction,
    _validate_override,
    apply_overrides,
    format_keybindings_help,
    load_user_overrides,
    resolve_bindings,
    to_textual_bindings,
)

# --- Fixtures ---


@pytest.fixture()
def valid_overrides_file(tmp_path: Path) -> Path:
    """Create a valid keybindings overrides file."""
    p = tmp_path / "keybindings.json"
    p.write_text(
        json.dumps({"quit": "Q", "refresh": "F5", "show_help": "h"}),
        encoding="utf-8",
    )
    return p


@pytest.fixture()
def reserved_overrides_file(tmp_path: Path) -> Path:
    """Create a keybindings file that tries to override reserved keys."""
    p = tmp_path / "keybindings.json"
    p.write_text(
        json.dumps({"quit": "ctrl+c", "refresh": "F5"}),
        encoding="utf-8",
    )
    return p


@pytest.fixture()
def invalid_json_file(tmp_path: Path) -> Path:
    """Create an invalid JSON file."""
    p = tmp_path / "keybindings.json"
    p.write_text("not valid json {{{", encoding="utf-8")
    return p


@pytest.fixture()
def non_dict_file(tmp_path: Path) -> Path:
    """Create a JSON file that's not an object."""
    p = tmp_path / "keybindings.json"
    p.write_text(json.dumps(["quit", "Q"]), encoding="utf-8")
    return p


# --- TestKeyAction ---


class TestKeyAction:
    def test_defaults(self) -> None:
        ka = KeyAction("q", "quit", "Quit")
        assert ka.key == "q"
        assert ka.action == "quit"
        assert ka.description == "Quit"
        assert ka.show is False
        assert ka.priority is False

    def test_frozen(self) -> None:
        ka = KeyAction("q", "quit", "Quit")
        with pytest.raises(AttributeError):
            ka.key = "x"  # type: ignore[misc]


# --- TestReservedKeys ---


class TestReservedKeys:
    def test_ctrl_c_reserved(self) -> None:
        assert "ctrl+c" in RESERVED_KEYS

    def test_ctrl_d_reserved(self) -> None:
        assert "ctrl+d" in RESERVED_KEYS

    def test_q_not_reserved(self) -> None:
        assert "q" not in RESERVED_KEYS


# --- TestLoadUserOverrides ---


class TestLoadUserOverrides:
    def test_loads_valid(self, valid_overrides_file: Path) -> None:
        overrides = load_user_overrides(valid_overrides_file)
        assert overrides["quit"] == "Q"
        assert overrides["refresh"] == "F5"
        assert overrides["show_help"] == "h"

    def test_missing_file(self, tmp_path: Path) -> None:
        overrides = load_user_overrides(tmp_path / "nonexistent.json")
        assert overrides == {}

    def test_invalid_json(self, invalid_json_file: Path) -> None:
        overrides = load_user_overrides(invalid_json_file)
        assert overrides == {}

    def test_non_dict(self, non_dict_file: Path) -> None:
        overrides = load_user_overrides(non_dict_file)
        assert overrides == {}


# --- TestValidateOverride ---


class TestValidateOverride:
    def test_valid_key(self) -> None:
        assert _validate_override("quit", "Q") is True

    def test_reserved_ctrl_c(self) -> None:
        assert _validate_override("quit", "ctrl+c") is False

    def test_reserved_ctrl_d(self) -> None:
        assert _validate_override("quit", "ctrl+d") is False

    def test_reserved_case_insensitive(self) -> None:
        assert _validate_override("quit", "CTRL+C") is False


# --- TestApplyOverrides ---


class TestApplyOverrides:
    def test_applies_valid(self) -> None:
        overrides = {"quit": "Q", "refresh": "F5"}
        result = apply_overrides(DEFAULT_BINDINGS, overrides)
        quit_action = next(a for a in result if a.action == "quit")
        assert quit_action.key == "Q"
        refresh_action = next(a for a in result if a.action == "refresh")
        assert refresh_action.key == "F5"

    def test_skips_reserved(self) -> None:
        overrides = {"quit": "ctrl+c", "refresh": "F5"}
        result = apply_overrides(DEFAULT_BINDINGS, overrides)
        quit_action = next(a for a in result if a.action == "quit")
        # Should keep default key, not ctrl+c
        assert quit_action.key == "q"
        refresh_action = next(a for a in result if a.action == "refresh")
        assert refresh_action.key == "F5"

    def test_preserves_unmodified(self) -> None:
        overrides = {"quit": "Q"}
        result = apply_overrides(DEFAULT_BINDINGS, overrides)
        # All actions should still be present
        assert len(result) == len(DEFAULT_BINDINGS)

    def test_empty_overrides(self) -> None:
        result = apply_overrides(DEFAULT_BINDINGS, {})
        assert result == DEFAULT_BINDINGS


# --- TestResolveBindings ---


class TestResolveBindings:
    def test_no_overrides_file(self) -> None:
        bindings = resolve_bindings(overrides_path=Path("/nonexistent/path.json"))
        assert bindings == DEFAULT_BINDINGS

    def test_with_overrides(self, valid_overrides_file: Path) -> None:
        bindings = resolve_bindings(overrides_path=valid_overrides_file)
        quit_action = next(a for a in bindings if a.action == "quit")
        assert quit_action.key == "Q"


# --- TestToTextualBindings ---


class TestToTextualBindings:
    def test_converts(self) -> None:
        from textual.binding import Binding

        bindings = to_textual_bindings(DEFAULT_BINDINGS)
        assert len(bindings) == len(DEFAULT_BINDINGS)
        assert all(isinstance(b, Binding) for b in bindings)

    def test_none_uses_resolve(self) -> None:
        bindings = to_textual_bindings(None)
        assert len(bindings) == len(DEFAULT_BINDINGS)


# --- TestFormatKeybindingsHelp ---


class TestFormatKeybindingsHelp:
    def test_format(self) -> None:
        help_text = format_keybindings_help(DEFAULT_BINDINGS)
        assert "Keyboard shortcuts" in help_text
        assert "Quit" in help_text
        assert "Ctrl+C" in help_text
        assert "Ctrl+D" in help_text

    def test_none_uses_resolve(self) -> None:
        help_text = format_keybindings_help(None)
        assert "Keyboard shortcuts" in help_text


# --- TestDefaultBindings ---


class TestDefaultBindings:
    def test_has_required_actions(self) -> None:
        action_names = {a.action for a in DEFAULT_BINDINGS}
        assert "quit" in action_names
        assert "refresh" in action_names
        assert "hard_stop" in action_names
        assert "toggle_action_bar" in action_names
        assert "spawn_now" in action_names
        assert "prioritize" in action_names
        assert "kill_agent" in action_names
        assert "cancel_task" in action_names
        assert "retry_task" in action_names
        assert "toggle_timeline" in action_names
        assert "show_help" in action_names

    def test_no_reserved_key_conflicts(self) -> None:
        for action in DEFAULT_BINDINGS:
            assert action.key.lower().strip() not in RESERVED_KEYS
