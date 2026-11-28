"""Tests for diff_render -- word-level diff tokenization and Rich rendering."""

from __future__ import annotations

import pytest
from rich.console import Console
from rich.style import Style

from bernstein.tui.diff_render import render_word_diff, word_diff


class TestTokenize:
    """Helper _tokenize behaviour (tested indirectly via word_diff)."""

    def test_word_tokenize_produces_tokens(self) -> None:
        """Sanity check: tokenize splits on word + non-word boundaries."""
        from bernstein.tui.diff_render import _tokenize

        tokens = _tokenize("hello world")
        assert tokens == ["hello", " ", "world"]


class TestWordDiff:
    """Tests for bernstein.diff_render.word_diff."""

    def test_identical_lines_no_changes(self) -> None:
        old, new = word_diff("hello world", "hello world")
        assert all(not changed for _, changed in old)
        assert all(not changed for _, changed in new)

    def test_single_word_replaced(self) -> None:
        old, new = word_diff("hello world", "hello there")
        old_text = [t for t, _ in old]
        new_text = [t for t, _ in new]
        # "world" should be marked as removed, "there" as added
        assert "world" in old_text
        assert "there" in new_text
        # Check flags
        flags_old = [(t, c) for t, c in old if t in ("world",)]
        assert len(flags_old) == 1
        assert flags_old[0][1] is True  # changed

    def test_word_added_at_end(self) -> None:
        old, new = word_diff("hello", "hello world")
        # old should have no changed tokens
        assert all(not c for _, c in old)
        # new should have " world" (space+word) flagged
        changed = [t for t, c in new if c]
        assert changed  # at least "world" and its preceding space

    def test_word_deleted_from_start(self) -> None:
        old, _new = word_diff("hello world", "world")
        # "hello " removed
        assert any(t == "hello" and c for t, c in old)

    def test_empty_old_line(self) -> None:
        old, new = word_diff("", "hello")
        assert len(old) == 0
        assert all(c for _, c in new)  # all tokens are new

    def test_empty_new_line(self) -> None:
        old, new = word_diff("hello", "")
        assert all(c for _, c in old)  # all tokens removed
        assert len(new) == 0

    def test_both_empty(self) -> None:
        old, new = word_diff("", "")
        assert old == []
        assert new == []

    def test_multiple_replacements(self) -> None:
        old, new = word_diff("foo bar baz", "foo qux quux")
        # " bar baz" removed, " qux quux" added
        old_changed = [t for t, c in old if c]
        new_changed = [t for t, c in new if c]
        assert "bar" in old_changed
        assert "baz" in old_changed
        assert "qux" in new_changed
        assert "quux" in new_changed

    def test_unicode_tokens(self) -> None:
        old, new = word_diff("héllo wörld", "héllo café")
        # "wörld" should be marked changed in old
        assert any(t == "wörld" and c for t, c in old)
        # "café" should be marked changed in new
        assert any(t == "café" and c for t, c in new)

    def test_preserves_whitespace_tokens(self) -> None:
        """Whitespace should be tokenized as separate tokens."""
        old, _new = word_diff("a  b", "a  c")
        # Two-space token should be present and unchanged
        assert any(t == "  " and not c for t, c in old)


class TestRenderWordDiff:
    """Tests for bernstein.diff_render.render_word_diff."""

    def _styled_lines(self, old_line: str, new_line: str) -> list[str]:
        console = Console(width=80, color_system="standard", force_terminal=True, no_color=False, highlight=False)
        with console.capture() as capture:
            render_word_diff(console, old_line, new_line)
        return capture.get().splitlines()

    def test_render_identical_lines(self) -> None:
        old, new = self._styled_lines("identical", "identical")
        assert old.endswith(" identical")
        assert new.endswith(" identical")

    def test_render_replaced_word(self) -> None:
        _, new = self._styled_lines("hello world", "hello there")
        assert new.endswith("hello " + Style.parse("bold green").render("there"))

    def test_render_deleted_word(self) -> None:
        old, _ = self._styled_lines("hello world", "hello")
        assert old.endswith(Style.parse("strike green").render("world"))

    def test_render_prefix(self) -> None:
        """Each printed line should start with a dim - or + prefix."""
        old, new = self._styled_lines("before", "after")
        assert old.startswith("  " + Style(dim=True).render("-") + " ")
        assert new.startswith("  " + Style(dim=True).render("+") + " ")


@pytest.mark.parametrize(
    ("old_line", "new_line"),
    [("items[0]", "items[1]"), ("[old]", "[new]"), ("items[bold]", "values[bold]")],
    ids=["index", "bracketed-word", "literal-style-name"],
)
def test_word_diff_prints_literal_index_brackets(old_line: str, new_line: str) -> None:
    console = Console(width=80, color_system=None, highlight=False)
    with console.capture() as capture:
        render_word_diff(console, old_line, new_line)
    assert capture.get() == f"  - {old_line}\n  + {new_line}\n"
