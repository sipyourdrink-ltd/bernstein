"""Tests for bernstein.cli.text_effects — TDD RED phase.

Tests cover:
- Non-TTY fallback: instant print without animation
- TTE-unavailable fallback: plain print when lib is missing
- Color conversion utility
- Empty inputs
- Default parameter contracts
"""

from __future__ import annotations

import sys

import pytest

from bernstein.cli.text_effects import (
    DEFAULT_COLORS,
    _strip_hash,
    _tte_available,
    logo_reveal,
    typing_effect,
)

# ---------------------------------------------------------------------------
# _strip_hash
# ---------------------------------------------------------------------------


def test_strip_hash_removes_prefix() -> None:
    assert _strip_hash("#00ff41") == "00ff41"


def test_strip_hash_noop_without_prefix() -> None:
    assert _strip_hash("00ff41") == "00ff41"


def test_strip_hash_empty() -> None:
    assert _strip_hash("") == ""


# ---------------------------------------------------------------------------
# DEFAULT_COLORS contract
# ---------------------------------------------------------------------------


def test_default_colors_has_two_entries() -> None:
    assert len(DEFAULT_COLORS) == 2


def test_default_colors_are_hex_strings() -> None:
    for color in DEFAULT_COLORS:
        assert color.startswith("#"), f"Expected hex color starting with #, got {color!r}"
        assert len(color) == 7, f"Expected 7-char hex color, got {color!r}"


# ---------------------------------------------------------------------------
# logo_reveal — non-TTY path (no animation, just print)
# ---------------------------------------------------------------------------


def test_logo_reveal_non_tty_prints_text(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    logo_reveal("BERNSTEIN")
    captured = capsys.readouterr()
    assert "BERNSTEIN" in captured.out


def test_logo_reveal_non_tty_does_not_animate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """In non-TTY, output is the text itself — not partial frames."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    logo_reveal("HELLO")
    captured = capsys.readouterr()
    # Should print the full text, not partial/empty frames
    assert captured.out.strip() == "HELLO"


def test_logo_reveal_non_tty_custom_effect_ignored(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unknown effect names don't raise in non-TTY mode."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    logo_reveal("TEST", effect="unknown_effect_xyz")
    captured = capsys.readouterr()
    assert "TEST" in captured.out


# ---------------------------------------------------------------------------
# logo_reveal — TTE unavailable path
# ---------------------------------------------------------------------------


def test_logo_reveal_tte_unavailable_prints_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("bernstein.cli.text_effects._tte_available", lambda: False)
    logo_reveal("BERNSTEIN")
    captured = capsys.readouterr()
    assert "BERNSTEIN" in captured.out


def test_logo_reveal_custom_colors_accepted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Custom colors don't raise even when TTE is unavailable."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    logo_reveal("X", colors=["#ff0000", "#0000ff"])
    captured = capsys.readouterr()
    assert "X" in captured.out


def test_logo_reveal_default_colors_used_when_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """colors=None uses DEFAULT_COLORS without error."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    logo_reveal("ABC", colors=None)
    captured = capsys.readouterr()
    assert "ABC" in captured.out


# ---------------------------------------------------------------------------
# typing_effect — non-TTY path
# ---------------------------------------------------------------------------


def test_typing_effect_non_tty_prints_all_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    typing_effect(["line one", "line two"])
    captured = capsys.readouterr()
    assert "line one" in captured.out
    assert "line two" in captured.out


def test_typing_effect_non_tty_preserves_order(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    typing_effect(["first", "second", "third"])
    captured = capsys.readouterr()
    lines = [l for l in captured.out.splitlines() if l]
    assert lines == ["first", "second", "third"]


def test_typing_effect_empty_list_no_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    typing_effect([])
    captured = capsys.readouterr()
    assert captured.out == ""


def test_typing_effect_single_line(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    typing_effect(["only one"])
    captured = capsys.readouterr()
    assert captured.out.strip() == "only one"


# ---------------------------------------------------------------------------
# _tte_available — smoke test (not mocked, checks real import)
# ---------------------------------------------------------------------------


def test_tte_available_returns_bool() -> None:
    result = _tte_available()
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# logo_reveal / typing_effect — function signatures
# ---------------------------------------------------------------------------


def test_logo_reveal_signature_defaults(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """logo_reveal(text) works with only the required argument."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    logo_reveal("BERNSTEIN")  # should not raise


def test_typing_effect_signature_defaults(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """typing_effect(lines) works with only the required argument."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    typing_effect(["boot"])  # should not raise
