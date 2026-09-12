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
# sla_store._single_line and spawner_core/spawner_merge._sanitise_for_log
# used to implement their own character classes independently of this one.
# Both now delegate to sanitize_log; this pins that the three entry points
# agree on every character that actually matters for log injection, so a
# future change to the boundary character set only has to change it here.


def test_every_entry_point_escapes_the_same_boundary_characters_identically() -> None:
    from bernstein.core.agents.spawner_core import _sanitise_for_log as spawner_core_sanitise
    from bernstein.core.agents.spawner_merge import _sanitise_for_log as spawner_merge_sanitise
    from bernstein.core.planning.sla_store import _single_line

    # CR, LF, U+2028 LINE SEPARATOR, and U+0085 NEL (a C1 control). Built
    # with chr() rather than an embedded literal so the boundary character
    # stays a visible codepoint reference in source, not an invisible byte.
    raw = "a\rb\nc" + chr(0x2028) + "d" + chr(0x85) + "z"
    expected = sanitize_log(raw)

    assert spawner_core_sanitise(raw) == expected
    assert spawner_merge_sanitise(raw) == expected
    assert _single_line(raw) == expected
