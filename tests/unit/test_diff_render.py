"""Tests for diff_render -- word-level diff tokenization and Rich rendering."""

from __future__ import annotations

from unittest.mock import MagicMock

from bernstein.diff_render import render_word_diff, word_diff


class TestTokenize:
    """Helper _tokenize behaviour (tested indirectly via word_diff)."""

    def test_word_tokenize_produces_tokens(self) -> None:
        """Sanity check: tokenize splits on word + non-word boundaries."""
        from bernstein.diff_render import _tokenize

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

    def _mock_console(self) -> MagicMock:
        """Return a mock Rich Console."""
        return MagicMock()

    def _last_print_call(self, console: MagicMock) -> str:
        """Extract the string passed to the last console.print call."""
        assert console.print.called
        args, _kwargs = console.print.call_args
        return args[0]

    def test_render_identical_lines(self) -> None:
        console = self._mock_console()
        render_word_diff(console, "identical", "identical")
        assert console.print.call_count == 2
        # Both lines should NOT contain any markup for changed text
        last = self._last_print_call(console)
        assert "[bold green]" not in last
        assert "[strike green]" not in last

    def test_render_replaced_word(self) -> None:
        console = self._mock_console()
        render_word_diff(console, "hello world", "hello there")
        assert console.print.call_count == 2
        second_call = self._last_print_call(console)
        assert "[bold green]" in second_call  # "there" should be bold green

    def test_render_deleted_word(self) -> None:
        console = self._mock_console()
        render_word_diff(console, "hello world", "hello")
        calls = [args[0] for args, _ in console.print.call_args_list]
        # First line (old) should have strikethrough for "world"
        assert any("[strike green]" in call for call in calls)

    def test_render_escapes_brackets(self) -> None:
        """Brackets in text should be escaped so Rich doesn't crash."""
        console = self._mock_console()
        render_word_diff(console, "[old]", "[new]")
        assert console.print.call_count == 2
        # Ensure raw brackets are escaped
        calls = [args[0] for args, _ in console.print.call_args_list]
        for call in calls:
            # Should contain \[  or \]  somewhere in the markup
            assert "\\[" in call or "\\]" in call

    def test_render_prefix(self) -> None:
        """Each printed line should start with a dim - or + prefix."""
        console = self._mock_console()
        render_word_diff(console, "before", "after")
        calls = [args[0] for args, _ in console.print.call_args_list]
        assert "[dim]-[/]" in calls[0]
        assert "[dim]+[/]" in calls[1]
