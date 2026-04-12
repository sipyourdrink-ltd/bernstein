"""Sanitize untrusted input for safe logging."""

from __future__ import annotations


def sanitize_log(value: str) -> str:
    """Remove newlines and carriage returns from a value before logging.

    Prevents log injection attacks where user-controlled input could forge
    log entries by embedding newline characters.
    """
    return value.replace("\n", "\\n").replace("\r", "\\r")
