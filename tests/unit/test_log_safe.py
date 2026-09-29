"""``for_log``, one test per way an untrusted value could reach a log record unsafe.

Every test below pins a way a request field, webhook payload, or database row
could otherwise forge a log line, blow past a bounded log record, or take the
logging call down with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.log_safe import for_log


def test_a_newline_is_escaped() -> None:
    """The core of the vulnerability: LF starts a new log record."""
    assert for_log("a\nb") == "a\\nb"


def test_a_carriage_return_is_escaped() -> None:
    assert for_log("a\rb") == "a\\rb"


def test_crlf_together_is_escaped() -> None:
    """The common forging payload - a Windows-style line ending."""
    assert for_log("a\r\nb") == "a\\r\\nb"


def test_other_control_characters_are_hex_escaped() -> None:
    """VT, FF and a C1 control (NEL) all start a record for some reader.

    ``str.splitlines`` and most log-shipping pipelines break on more than CR
    and LF; a value that only had ``\\r``/``\\n`` stripped could still forge a
    record through one of these.
    """
    assert for_log("a\x0bb") == "a\\x0bb"
    assert for_log("a\x0cb") == "a\\x0cb"
    assert for_log("a\x85b") == "a\\x85b"


def test_the_escape_character_is_escaped() -> None:
    """ESC drives a terminal, not just a log parser.

    An operator tailing a log file renders ANSI sequences, so an unescaped
    ESC lets a value repaint the terminal -- overwriting the real record or
    hiding itself -- without ever crossing a record boundary.
    """
    assert for_log("a\x1b[31mb") == "a\\x1b[31mb"


def test_the_unicode_line_separators_are_escaped() -> None:
    """U+2028/U+2029 break lines for readers that `str.splitlines` agrees with.

    A pipeline that splits records with ``splitlines`` (or a chat/web renderer
    downstream of the log) treats these as record boundaries even though CR
    and LF are absent, so stripping only CR/LF would leave the forgery open.
    """
    assert for_log("line\u2028sep") == "line\\u2028sep"
    assert for_log("line\u2029sep") == "line\\u2029sep"


def test_a_tab_is_escaped_too() -> None:
    """Tab is a C0 control character; this helper escapes it like the rest.

    Nothing downstream needs a literal tab in a single-line log token, and
    escaping it uniformly means the safe character set stays one simple rule
    rather than a tab-shaped exception to it.
    """
    assert for_log("a\tb") == "a\\tb"


def test_safe_text_passes_through_unchanged() -> None:
    """Sanitizing must never alter the meaning of an already-safe value."""
    assert for_log("task-42") == "task-42"


def test_a_value_at_the_limit_is_not_truncated() -> None:
    value = "a" * 10
    assert for_log(value, limit=10) == value


def test_a_value_over_the_limit_is_cut_with_a_marker() -> None:
    """The boundary the flood protection depends on.

    One character past the limit is enough to trigger truncation, and the
    marker makes a cut value visually distinguishable from one that just
    happened to be exactly that long.
    """
    value = "a" * 11
    assert for_log(value, limit=10) == "a" * 10 + "...(truncated)"


def test_truncation_uses_the_default_limit_of_512() -> None:
    value = "a" * 600
    result = for_log(value)
    assert result == "a" * 512 + "...(truncated)"


def test_a_non_string_input_is_converted() -> None:
    """Half of what reaches a log call is not already a string."""
    assert for_log(42) == "42"
    assert for_log(Path("a/b")) == str(Path("a/b"))
    assert for_log(None) == "None"


def test_an_object_whose_str_raises_returns_a_placeholder() -> None:
    """The value least worth trusting must not take the log call down with it."""

    class _Hostile:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert for_log(_Hostile()) == "<unprintable _Hostile>"


@pytest.mark.parametrize("bad", [object(), [1, 2], {"a": 1}])
def test_never_raises_regardless_of_input_shape(bad: object) -> None:
    """The one hard guarantee: whatever is handed in, this returns a string."""
    assert isinstance(for_log(bad), str)
