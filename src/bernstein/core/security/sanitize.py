"""Sanitize untrusted input for safe logging."""

from __future__ import annotations

import re

# Every character a log reader can treat as a record boundary, plus every
# character that drives a terminal rendering the log.
#
# ``str.splitlines`` (and most log-shipping pipelines) break on more than CR
# and LF: VT, FF, FS, GS, RS, NEL, LINE SEPARATOR and PARAGRAPH SEPARATOR all
# start a new line.  URL parsing strips only TAB/CR/LF, so the rest of that
# set reaches a log sink intact from a percent-encoded request path.  ESC and
# the other C0/C1 bytes do not split a record but do let a caller repaint a
# terminal or hide text from an operator reading the file.
_UNSAFE_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")

_NAMED_ESCAPES: dict[str, str] = {
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}

_TRANSLATION: dict[int, str] = {
    **{codepoint: f"\\x{codepoint:02x}" for codepoint in (*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0))},
    **{ord(char): escape for char, escape in _NAMED_ESCAPES.items()},
    0x2028: "\\u2028",
    0x2029: "\\u2029",
}


def sanitize_log(value: str) -> str:
    """Escape record-boundary and control characters before logging.

    Prevents log injection, where caller-controlled input forges additional
    log entries by embedding a character the log reader treats as the start
    of a new record.

    Printable text is returned unchanged, so sanitizing never alters the
    meaning of a benign value.

    Args:
        value: Untrusted string bound for a ``logger.*`` call.

    Returns:
        *value* with every C0 control, DEL, C1 control, U+2028 and U+2029
        replaced by a printable escape sequence.
    """
    if not _UNSAFE_RE.search(value):
        return value
    return value.translate(_TRANSLATION)


__all__ = ["sanitize_log"]
