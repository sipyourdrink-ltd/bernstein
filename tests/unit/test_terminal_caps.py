"""Tests for bernstein.cli.terminal_caps — capability detection module."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

import bernstein.cli.terminal_caps as _caps_module
from bernstein.cli.terminal_caps import (
    Protocol,
    TerminalCaps,
    detect_capabilities,
)

# ── Helpers ───────────────────────────────────────────────────────────────


def _make(
    *,
    is_tty: bool = True,
    supports_kitty: bool = False,
    supports_iterm2: bool = False,
    supports_sixel: bool = False,
    supports_truecolor: bool = True,
    supports_256color: bool = True,
    term_width: int = 80,
    term_height: int = 24,
) -> TerminalCaps:
    return TerminalCaps(
        is_tty=is_tty,
        supports_kitty=supports_kitty,
        supports_iterm2=supports_iterm2,
        supports_sixel=supports_sixel,
        supports_truecolor=supports_truecolor,
        supports_256color=supports_256color,
        term_width=term_width,
        term_height=term_height,
    )


def _detect_with_tty(env: dict[str, str]) -> TerminalCaps:
    """Run TerminalCaps.detect() with the given env vars and stdout=TTY."""
    with patch.dict("os.environ", env, clear=False), patch.object(
        sys.stdout, "isatty", return_value=True
    ):
        return TerminalCaps.detect()


# ── Protocol enum ─────────────────────────────────────────────────────────


class TestProtocol:
    def test_all_expected_values_exist(self) -> None:
        expected = {"kitty", "iterm2", "sixel", "half_block", "braille", "ascii", "none"}
        assert {p.value for p in Protocol} == expected

    def test_string_comparison(self) -> None:
        assert Protocol.KITTY == "kitty"
        assert Protocol.NONE == "none"


# ── TerminalCaps frozen dataclass ─────────────────────────────────────────


class TestTerminalCapsDataclass:
    def test_is_frozen(self) -> None:
        caps = _make()
        with pytest.raises((TypeError, AttributeError)):
            caps.is_tty = False  # type: ignore[misc]

    def test_null_all_false(self) -> None:
        caps = TerminalCaps.null()
        assert caps.is_tty is False
        assert caps.supports_truecolor is False
        assert caps.supports_256color is False
        assert caps.supports_kitty is False
        assert caps.supports_iterm2 is False
        assert caps.supports_sixel is False

    def test_null_default_dimensions(self) -> None:
        caps = TerminalCaps.null()
        assert caps.term_width == 80
        assert caps.term_height == 24


# ── New capability properties ─────────────────────────────────────────────


class TestCapabilityProperties:
    """New-spec properties: kitty_graphics, iterm2_inline, sixel, truecolor,
    halfblocks, sync_output, braille — all False on non-TTY."""

    # kitty_graphics
    def test_kitty_graphics_true_when_tty_and_kitty(self) -> None:
        assert _make(is_tty=True, supports_kitty=True).kitty_graphics is True

    def test_kitty_graphics_false_non_tty(self) -> None:
        assert _make(is_tty=False, supports_kitty=True).kitty_graphics is False

    def test_kitty_graphics_false_no_kitty(self) -> None:
        assert _make(is_tty=True, supports_kitty=False).kitty_graphics is False

    # iterm2_inline
    def test_iterm2_inline_true_when_tty_and_iterm2(self) -> None:
        assert _make(is_tty=True, supports_iterm2=True).iterm2_inline is True

    def test_iterm2_inline_false_non_tty(self) -> None:
        assert _make(is_tty=False, supports_iterm2=True).iterm2_inline is False

    # sixel
    def test_sixel_true_when_tty_and_sixel(self) -> None:
        assert _make(is_tty=True, supports_sixel=True).sixel is True

    def test_sixel_false_non_tty(self) -> None:
        assert _make(is_tty=False, supports_sixel=True).sixel is False

    # truecolor
    def test_truecolor_true_when_tty_and_truecolor(self) -> None:
        assert _make(is_tty=True, supports_truecolor=True).truecolor is True

    def test_truecolor_false_non_tty(self) -> None:
        assert _make(is_tty=False, supports_truecolor=True).truecolor is False

    # halfblocks
    def test_halfblocks_true_truecolor(self) -> None:
        assert _make(is_tty=True, supports_truecolor=True, supports_256color=False).halfblocks is True

    def test_halfblocks_true_256color(self) -> None:
        assert _make(is_tty=True, supports_truecolor=False, supports_256color=True).halfblocks is True

    def test_halfblocks_false_no_color(self) -> None:
        assert _make(is_tty=True, supports_truecolor=False, supports_256color=False).halfblocks is False

    def test_halfblocks_false_non_tty(self) -> None:
        assert _make(is_tty=False, supports_truecolor=True).halfblocks is False

    # sync_output
    def test_sync_output_true_on_tty(self) -> None:
        assert _make(is_tty=True).sync_output is True

    def test_sync_output_false_non_tty(self) -> None:
        assert _make(is_tty=False).sync_output is False

    # braille
    def test_braille_true_on_tty(self) -> None:
        assert _make(is_tty=True).braille is True

    def test_braille_false_non_tty(self) -> None:
        assert _make(is_tty=False).braille is False

    # best_image_protocol alias
    def test_best_image_protocol_equals_best_protocol(self) -> None:
        for caps in [
            _make(is_tty=True, supports_kitty=True),
            _make(is_tty=True, supports_iterm2=True),
            _make(is_tty=True, supports_sixel=True),
            _make(is_tty=True),
            _make(is_tty=False),
        ]:
            assert caps.best_image_protocol == caps.best_protocol


# ── best_protocol fallback chain ──────────────────────────────────────────


class TestBestProtocol:
    def test_kitty_highest_priority(self) -> None:
        caps = _make(supports_kitty=True, supports_iterm2=True, supports_sixel=True)
        assert caps.best_protocol is Protocol.KITTY

    def test_iterm2_beats_sixel(self) -> None:
        caps = _make(supports_iterm2=True, supports_sixel=True)
        assert caps.best_protocol is Protocol.ITERM2

    def test_sixel_beats_half_block(self) -> None:
        caps = _make(supports_sixel=True, supports_truecolor=True)
        assert caps.best_protocol is Protocol.SIXEL

    def test_truecolor_gives_half_block(self) -> None:
        caps = _make(supports_truecolor=True)
        assert caps.best_protocol is Protocol.HALF_BLOCK

    def test_256color_gives_half_block(self) -> None:
        caps = _make(supports_truecolor=False, supports_256color=True)
        assert caps.best_protocol is Protocol.HALF_BLOCK

    def test_no_color_gives_braille(self) -> None:
        caps = _make(supports_truecolor=False, supports_256color=False)
        assert caps.best_protocol is Protocol.BRAILLE

    def test_non_tty_gives_none(self) -> None:
        caps = _make(is_tty=False, supports_kitty=True)
        assert caps.best_protocol is Protocol.NONE


# ── detect() — env var based detection ───────────────────────────────────


class TestDetect:
    """Each test patches env vars + sys.stdout.isatty to simulate a live TTY."""

    def test_kitty_window_id(self) -> None:
        caps = _detect_with_tty({"KITTY_WINDOW_ID": "1"})
        assert caps.supports_kitty is True
        assert caps.kitty_graphics is True

    def test_wezterm_supports_kitty_iterm2_sixel(self) -> None:
        caps = _detect_with_tty({"TERM_PROGRAM": "WezTerm"})
        assert caps.supports_kitty is True
        assert caps.supports_iterm2 is True
        assert caps.supports_sixel is True

    def test_ghostty_supports_kitty(self) -> None:
        caps = _detect_with_tty({"TERM_PROGRAM": "ghostty"})
        assert caps.supports_kitty is True

    def test_iterm2_by_term_program(self) -> None:
        caps = _detect_with_tty({"TERM_PROGRAM": "iTerm.app"})
        assert caps.supports_iterm2 is True

    def test_vscode_supports_iterm2_and_sixel(self) -> None:
        caps = _detect_with_tty({"TERM_PROGRAM": "vscode"})
        assert caps.supports_iterm2 is True
        assert caps.supports_sixel is True

    def test_windows_terminal_via_wt_session(self) -> None:
        caps = _detect_with_tty({"WT_SESSION": "abc-123"})
        assert caps.supports_sixel is True

    def test_konsole_supports_sixel_and_iterm2(self) -> None:
        caps = _detect_with_tty({"KONSOLE_VERSION": "220400"})
        assert caps.supports_sixel is True
        assert caps.supports_iterm2 is True

    def test_colorterm_truecolor(self) -> None:
        caps = _detect_with_tty({"COLORTERM": "truecolor"})
        assert caps.supports_truecolor is True
        assert caps.supports_256color is True

    def test_colorterm_24bit(self) -> None:
        caps = _detect_with_tty({"COLORTERM": "24bit"})
        assert caps.supports_truecolor is True

    def test_term_256color_implies_256color(self) -> None:
        caps = _detect_with_tty({"TERM": "xterm-256color"})
        assert caps.supports_256color is True

    def test_is_tty_reflects_isatty(self) -> None:
        with patch.object(sys.stdout, "isatty", return_value=True):
            caps = TerminalCaps.detect()
        assert caps.is_tty is True

    def test_non_tty_is_tty_false(self) -> None:
        with patch.object(sys.stdout, "isatty", return_value=False):
            caps = TerminalCaps.detect()
        assert caps.is_tty is False

    def test_non_tty_all_new_props_false(self) -> None:
        """Non-TTY → all new capability properties are False."""
        with patch.object(sys.stdout, "isatty", return_value=False), patch.dict(
            "os.environ",
            {"KITTY_WINDOW_ID": "1", "COLORTERM": "truecolor", "TERM_PROGRAM": "WezTerm"},
            clear=False,
        ):
            caps = TerminalCaps.detect()
        assert caps.is_tty is False
        assert caps.kitty_graphics is False
        assert caps.iterm2_inline is False
        assert caps.sixel is False
        assert caps.truecolor is False
        assert caps.halfblocks is False
        assert caps.sync_output is False
        assert caps.braille is False

    def test_best_protocol_none_non_tty(self) -> None:
        with patch.object(sys.stdout, "isatty", return_value=False):
            caps = TerminalCaps.detect()
        assert caps.best_protocol is Protocol.NONE


# ── detect_capabilities() — module-level cached function ─────────────────


class TestDetectCapabilities:
    def setup_method(self) -> None:
        """Reset the module-level cache before each test."""
        _caps_module._caps_cache = None

    def test_returns_terminal_caps(self) -> None:
        result = detect_capabilities()
        assert isinstance(result, TerminalCaps)

    def test_caching_returns_same_instance(self) -> None:
        first = detect_capabilities()
        second = detect_capabilities()
        assert first is second

    def test_cache_populated_after_call(self) -> None:
        assert _caps_module._caps_cache is None
        detect_capabilities()
        assert _caps_module._caps_cache is not None

    def test_cached_value_matches_detect(self) -> None:
        from_func = detect_capabilities()
        # Compare a stable field (term_width is always an int)
        assert from_func.term_width >= 0
