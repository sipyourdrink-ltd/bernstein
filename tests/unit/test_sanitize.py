"""Unit tests for log sanitization."""

from __future__ import annotations

from bernstein.core.sanitize import sanitize_log


def test_sanitize_replaces_newline() -> None:
    assert sanitize_log("a\nb") == "a\\nb"


def test_sanitize_replaces_carriage_return() -> None:
    assert sanitize_log("a\rb") == "a\\rb"


def test_sanitize_replaces_both() -> None:
    assert sanitize_log("a\r\nb") == "a\\r\\nb"


def test_sanitize_passthrough_for_safe_content() -> None:
    value = "safe content 123"
    assert sanitize_log(value) == value


# ---------------------------------------------------------------------------
# One escaper, not three (#5749)
# ---------------------------------------------------------------------------
#
# Three independent character classes used to escape log-bound values:
# sla_store._single_line, and one copy each in spawner_core and
# spawner_merge under the name _sanitise_for_log. All three are gone: the
# spawner call sites call sanitize_log directly, and sla_store's call sites
# call log_safe.for_log(..., escape_nonprintable=True), which is sanitize_log,
# then an escape of every character str.isprintable() rejects, then a length
# cap. That second pass restores what the original _single_line escaped
# with its own str.isprintable() check. This pins both halves: the SLA store's log tokens
# agree with sanitize_log on the record boundaries, and additionally escape
# the bidi and zero-width characters sanitize_log passes through. The set
# below is a sample of each class, not an exhaustive list.


def test_sla_store_log_token_agrees_with_sanitize_log_on_record_boundaries() -> None:
    from bernstein.core.log_safe import for_log

    # CR, LF, U+2028 LINE SEPARATOR, and U+0085 NEL (a C1 control). Built
    # with chr() rather than an embedded literal so the boundary character
    # stays a visible codepoint reference in source, not an invisible byte.
    raw = "a\rb\nc" + chr(0x2028) + "d" + chr(0x85) + "z"
    assert for_log(raw, limit=256, escape_nonprintable=True) == sanitize_log(raw)


def test_sla_store_log_token_escapes_bidi_and_zero_width_characters() -> None:
    from bernstein.core.log_safe import for_log

    # U+202E RIGHT-TO-LEFT OVERRIDE and U+200B ZERO WIDTH SPACE: sanitize_log
    # passes both through; the SLA store's log token must not.
    raw = "a" + chr(0x202E) + "b" + chr(0x200B) + "c"
    assert sanitize_log(raw) == raw
    assert for_log(raw, limit=256, escape_nonprintable=True) == "a\\u202eb\\u200bc"
