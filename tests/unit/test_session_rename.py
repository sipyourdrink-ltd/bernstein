"""Tests for session_rename — validation, rename, and CLI integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.session_rename import rename_session, validate_session_name

# ---------------------------------------------------------------------------
# validate_session_name tests
# ---------------------------------------------------------------------------


class TestValidateSessionName:
    """Tests for :func:`validate_session_name`."""

    def test_empty_name_rejected(self) -> None:
        errors = validate_session_name("")
        assert "session name cannot be empty" in errors

    def test_name_with_spaces_rejected(self) -> None:
        errors = validate_session_name("my session name")
        assert any("alphanumeric" in e for e in errors)

    def test_name_leading_hyphen_rejected(self) -> None:
        errors = validate_session_name("-bad-name")
        assert any("alphanumeric" in e for e in errors)

    def test_valid_simple_name(self) -> None:
        assert validate_session_name("my-session") == []

    def test_valid_numeric_name(self) -> None:
        assert validate_session_name("session123") == []

    def test_max_length_valid(self) -> None:
        name = "a" * 60
        assert validate_session_name(name) == []

    def test_too_long_name_rejected(self) -> None:
        name = "a" * 61
        errors = validate_session_name(name)
        assert any("too long" in e for e in errors)

    def test_all_valid_chars(self) -> None:
        assert validate_session_name("Ab1-cd2-EF3") == []

    def test_special_characters_rejected(self) -> None:
        errors = validate_session_name("my_session!")
        assert any("alphanumeric" in e for e in errors)


# ---------------------------------------------------------------------------
# rename_session tests
# ---------------------------------------------------------------------------


class TestRenameSession:
    """Tests for :func:`rename_session`."""

    def test_empty_name_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="cannot be empty"):
            rename_session("", tmp_path)

    def test_rename_creates_session_file(self, tmp_path: Path) -> None:
        assert rename_session("my-new-session", tmp_path) is True
        session_file = tmp_path / ".sdd" / "runtime" / "session.json"
        assert session_file.exists()
        data = json.loads(session_file.read_text())
        assert data["goal"] == "my-new-session"
        assert data["name"] == "my-new-session"

    def test_rename_overwrites_existing(self, tmp_path: Path) -> None:
        # Write a pre-existing session state.
        session_file = tmp_path / ".sdd" / "runtime" / "session.json"
        session_file.parent.mkdir(parents=True)
        session_file.write_text(json.dumps({"saved_at": 100.0, "goal": "old-goal"}))
        assert rename_session("new-goal", tmp_path) is True
        data = json.loads(session_file.read_text())
        assert data["goal"] == "new-goal"
        assert data["name"] == "new-goal"

    def test_rename_creates_parent_dirs(self, tmp_path: Path) -> None:
        runtime = tmp_path / ".sdd" / "runtime"
        assert not runtime.exists()
        rename_session("fresh-session", tmp_path)
        assert runtime.exists()
