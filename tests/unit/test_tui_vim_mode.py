"""Tests for TUI-014: Vim-mode keybindings for TUI navigation."""

from __future__ import annotations

from bernstein.tui.vim_mode import (
    VimAction,
    VimActionKind,
    VimMode,
    VimState,
)

# --- VimMode enum ---


class TestVimMode:
    def test_has_three_modes(self) -> None:
        """VimMode exposes normal, command, and search modes."""
        assert VimMode.NORMAL is not None
        assert VimMode.COMMAND is not None
        assert VimMode.SEARCH is not None

    def test_modes_are_distinct(self) -> None:
        assert VimMode.NORMAL != VimMode.COMMAND
        assert VimMode.COMMAND != VimMode.SEARCH


# --- VimAction frozen dataclass ---


class TestVimAction:
    def test_defaults(self) -> None:
        action = VimAction(VimActionKind.NONE)
        assert action.kind == VimActionKind.NONE
        assert action.payload == ""

    def test_with_payload(self) -> None:
        action = VimAction(VimActionKind.SCROLL_DOWN, "3")
        assert action.payload == "3"

    def test_is_frozen(self) -> None:
        action = VimAction(VimActionKind.NONE)
        try:
            action.kind = VimActionKind.SCROLL_UP  # type: ignore[misc]
            raise AssertionError("Should have raised")
        except AttributeError:
            pass


# --- VimState: normal mode basics ---


class TestVimStateNormalNavigation:
    def test_initial_mode_is_normal(self) -> None:
        state = VimState()
        assert state.mode == VimMode.NORMAL

    def test_j_scrolls_down(self) -> None:
        state = VimState()
        action = state.handle_key("j")
        assert action.kind == VimActionKind.SCROLL_DOWN
        assert action.payload == "1"

    def test_k_scrolls_up(self) -> None:
        state = VimState()
        action = state.handle_key("k")
        assert action.kind == VimActionKind.SCROLL_UP

    def test_h_scrolls_left(self) -> None:
        state = VimState()
        action = state.handle_key("h")
        assert action.kind == VimActionKind.SCROLL_LEFT

    def test_l_scrolls_right(self) -> None:
        state = VimState()
        action = state.handle_key("l")
        assert action.kind == VimActionKind.SCROLL_RIGHT

    def test_arrow_down(self) -> None:
        state = VimState()
        action = state.handle_key("down")
        assert action.kind == VimActionKind.SCROLL_DOWN

    def test_arrow_up(self) -> None:
        state = VimState()
        action = state.handle_key("up")
        assert action.kind == VimActionKind.SCROLL_UP

    def test_arrow_left(self) -> None:
        state = VimState()
        action = state.handle_key("left")
        assert action.kind == VimActionKind.SCROLL_LEFT

    def test_arrow_right(self) -> None:
        state = VimState()
        action = state.handle_key("right")
        assert action.kind == VimActionKind.SCROLL_RIGHT

    def test_G_goto_bottom(self) -> None:
        state = VimState()
        action = state.handle_key("G")
        assert action.kind == VimActionKind.GOTO_BOTTOM

    def test_gg_goto_top(self) -> None:
        state = VimState()
        first = state.handle_key("g")
        assert first.kind == VimActionKind.NONE
        second = state.handle_key("g")
        assert second.kind == VimActionKind.GOTO_TOP

    def test_g_then_non_g_clears_pending(self) -> None:
        """Pressing 'g' then a non-'g' key does not produce gg."""
        state = VimState()
        state.handle_key("g")
        assert state.pending == "g"
        action = state.handle_key("j")
        # 'g' was consumed; 'j' is dispatched independently
        assert action.kind == VimActionKind.SCROLL_DOWN
        assert state.pending == ""

    def test_ctrl_u_half_page_up(self) -> None:
        state = VimState()
        action = state.handle_key("ctrl+u")
        assert action.kind == VimActionKind.HALF_PAGE_UP

    def test_ctrl_d_half_page_down(self) -> None:
        state = VimState()
        action = state.handle_key("ctrl+d")
        assert action.kind == VimActionKind.HALF_PAGE_DOWN

    def test_unknown_key_returns_none(self) -> None:
        state = VimState()
        action = state.handle_key("F12")
        assert action.kind == VimActionKind.NONE


# --- VimState: numeric count prefix ---


class TestVimStateCountPrefix:
    def test_count_then_j(self) -> None:
        """5j should scroll down with count=5."""
        state = VimState()
        state.handle_key("5")
        action = state.handle_key("j")
        assert action.kind == VimActionKind.SCROLL_DOWN
        assert action.payload == "5"

    def test_multi_digit_count(self) -> None:
        """12k should scroll up with count=12."""
        state = VimState()
        state.handle_key("1")
        state.handle_key("2")
        action = state.handle_key("k")
        assert action.kind == VimActionKind.SCROLL_UP
        assert action.payload == "12"

    def test_zero_alone_is_not_count(self) -> None:
        """Leading 0 is not accumulated as a count prefix."""
        state = VimState()
        action = state.handle_key("0")
        # 0 by itself is not a count prefix, so dispatched normally
        assert action.kind != VimActionKind.SCROLL_DOWN

    def test_count_resets_after_dispatch(self) -> None:
        """Count prefix resets after dispatching the movement."""
        state = VimState()
        state.handle_key("3")
        state.handle_key("j")
        action = state.handle_key("j")
        assert action.payload == "1"


# --- VimState: command mode ---


class TestVimStateCommandMode:
    def test_colon_enters_command_mode(self) -> None:
        state = VimState()
        action = state.handle_key(":")
        assert action.kind == VimActionKind.ENTER_COMMAND
        assert state.mode == VimMode.COMMAND

    def test_type_command_and_submit(self) -> None:
        state = VimState()
        state.handle_key(":")
        state.handle_key("q")
        state.handle_key("u")
        state.handle_key("i")
        state.handle_key("t")
        action = state.handle_key("enter")
        assert action.kind == VimActionKind.SUBMIT_COMMAND
        assert action.payload == "quit"
        assert state.mode == VimMode.NORMAL

    def test_escape_cancels_command(self) -> None:
        state = VimState()
        state.handle_key(":")
        state.handle_key("q")
        action = state.handle_key("escape")
        assert action.kind == VimActionKind.CANCEL
        assert action.payload == "q"
        assert state.mode == VimMode.NORMAL

    def test_backspace_in_command(self) -> None:
        state = VimState()
        state.handle_key(":")
        state.handle_key("a")
        state.handle_key("b")
        action = state.handle_key("backspace")
        assert action.kind == VimActionKind.BACKSPACE
        assert state.buffer == "a"

    def test_backspace_on_empty_buffer(self) -> None:
        state = VimState()
        state.handle_key(":")
        action = state.handle_key("backspace")
        assert action.kind == VimActionKind.BACKSPACE
        assert state.buffer == ""

    def test_append_char_action(self) -> None:
        state = VimState()
        state.handle_key(":")
        action = state.handle_key("x")
        assert action.kind == VimActionKind.APPEND_CHAR
        assert action.payload == "x"


# --- VimState: search mode ---


class TestVimStateSearchMode:
    def test_slash_enters_search_mode(self) -> None:
        state = VimState()
        action = state.handle_key("/")
        assert action.kind == VimActionKind.ENTER_SEARCH
        assert state.mode == VimMode.SEARCH

    def test_type_search_and_submit(self) -> None:
        state = VimState()
        state.handle_key("/")
        state.handle_key("f")
        state.handle_key("o")
        state.handle_key("o")
        action = state.handle_key("enter")
        assert action.kind == VimActionKind.SUBMIT_SEARCH
        assert action.payload == "foo"
        assert state.mode == VimMode.NORMAL

    def test_escape_cancels_search(self) -> None:
        state = VimState()
        state.handle_key("/")
        state.handle_key("b")
        action = state.handle_key("escape")
        assert action.kind == VimActionKind.CANCEL
        assert state.mode == VimMode.NORMAL

    def test_backspace_in_search(self) -> None:
        state = VimState()
        state.handle_key("/")
        state.handle_key("a")
        state.handle_key("b")
        action = state.handle_key("backspace")
        assert action.kind == VimActionKind.BACKSPACE
        assert state.buffer == "a"


# --- VimState: enabled / disabled ---


class TestVimStateEnabled:
    def test_disabled_returns_none(self) -> None:
        state = VimState(enabled=False)
        action = state.handle_key("j")
        assert action.kind == VimActionKind.NONE

    def test_can_toggle_enabled(self) -> None:
        state = VimState(enabled=False)
        assert state.handle_key("j").kind == VimActionKind.NONE
        state.enabled = True
        assert state.handle_key("j").kind == VimActionKind.SCROLL_DOWN


# --- VimState: reset ---


class TestVimStateReset:
    def test_reset_clears_mode(self) -> None:
        state = VimState()
        state.handle_key(":")
        assert state.mode == VimMode.COMMAND
        state.reset()
        assert state.mode == VimMode.NORMAL

    def test_reset_clears_buffer(self) -> None:
        state = VimState()
        state.handle_key(":")
        state.handle_key("q")
        state.reset()
        assert state.buffer == ""

    def test_reset_clears_pending(self) -> None:
        state = VimState()
        state.handle_key("g")
        assert state.pending == "g"
        state.reset()
        assert state.pending == ""


# --- VimActionKind coverage ---


class TestVimActionKind:
    def test_all_kinds_listed(self) -> None:
        """Sanity check that the enum has the expected members."""
        expected = {
            "NONE",
            "SCROLL_UP",
            "SCROLL_DOWN",
            "SCROLL_LEFT",
            "SCROLL_RIGHT",
            "GOTO_TOP",
            "GOTO_BOTTOM",
            "HALF_PAGE_UP",
            "HALF_PAGE_DOWN",
            "ENTER_COMMAND",
            "ENTER_SEARCH",
            "SUBMIT_COMMAND",
            "SUBMIT_SEARCH",
            "CANCEL",
            "APPEND_CHAR",
            "BACKSPACE",
        }
        actual = {m.name for m in VimActionKind}
        assert expected == actual
