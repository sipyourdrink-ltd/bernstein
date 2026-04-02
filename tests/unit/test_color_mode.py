"""Tests for color_mode — terminal color detection."""

from __future__ import annotations

import pytest

from bernstein.color_mode import (
    ColorMode,
    color_mode_supports_256,
    color_mode_supports_truecolor,
    detect_color_mode,
)

# --- Fixtures ---


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip color-related env vars for isolated testing."""
    for var in ("COLORTERM", "TERM", "CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(var, raising=False)


# --- TestColorMode ---


class TestColorMode:
    def test_truecolor(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COLORTERM", "truecolor")
        assert detect_color_mode() == ColorMode.TRUECOLOR

    def test_24bit(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COLORTERM", "24bit")
        assert detect_color_mode() == ColorMode.TRUECOLOR

    def test_256color(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TERM", "xterm-256color")
        assert detect_color_mode() == ColorMode.COLOR_256

    def test_screen_256(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TERM", "screen-256color")
        assert detect_color_mode() == ColorMode.COLOR_256

    def test_generic_color(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TERM", "linux-color")
        assert detect_color_mode() == ColorMode.ANSI

    def test_dumb_terminal(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TERM", "dumb")
        assert detect_color_mode() == ColorMode.NONE

    def test_empty_term(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TERM", "")
        assert detect_color_mode() == ColorMode.NONE

    def test_unset_term_is_ansi(self, clean_env: None) -> None:
        # When TERM isn't set at all, default to ANSI (common in Docker)
        assert detect_color_mode() == ColorMode.ANSI

    def test_ci_environment(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI", "true")
        assert detect_color_mode() == ColorMode.ANSI

    def test_github_actions(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("TERM", "")
        assert detect_color_mode() == ColorMode.ANSI

    def test_default_no_env_is_ansi(self, clean_env: None) -> None:
        # Without any env vars, should default to ANSI
        assert detect_color_mode() == ColorMode.ANSI


# --- TestHelperFunctions ---


class TestHelperFunctions:
    def test_truecolor_supports_truecolor(self) -> None:
        assert color_mode_supports_truecolor(ColorMode.TRUECOLOR)
        assert not color_mode_supports_truecolor(ColorMode.COLOR_256)
        assert not color_mode_supports_truecolor(ColorMode.ANSI)
        assert not color_mode_supports_truecolor(ColorMode.NONE)

    def test_256_supports_truecolor_and_256(self) -> None:
        assert color_mode_supports_256(ColorMode.TRUECOLOR)
        assert color_mode_supports_256(ColorMode.COLOR_256)
        assert not color_mode_supports_256(ColorMode.ANSI)
        assert not color_mode_supports_256(ColorMode.NONE)
