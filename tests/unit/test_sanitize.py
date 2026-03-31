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
