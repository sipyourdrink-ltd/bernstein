"""PII redaction filter for Python logging.

Installs a ``logging.Filter`` on the root logger that automatically replaces
email addresses, phone numbers, SSNs, and credit card numbers with
``[REDACTED]`` before log records are emitted.

Usage::

    from bernstein.core.log_redact import install_pii_filter

    install_pii_filter()          # attaches to root logger
    install_pii_filter(logger)    # attaches to a specific logger

The filter mutates ``record.msg`` and ``record.args`` in-place so that
downstream handlers (file, console, structured JSON) all receive sanitised
text — no PII is ever written to disk or stdout.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# ---------------------------------------------------------------------------
# PII patterns — kept in sync with memory_sanitizer._PII_RULES
# ---------------------------------------------------------------------------

_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "email",
        re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"),
    ),
    (
        "phone",
        re.compile(r"(?:\+\d{1,3}[\s\-])?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"),
    ),
    (
        "ssn",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    ),
    (
        "credit_card",
        re.compile(r"\b(?:\d{4}[\s\-]?){3}\d{4}\b"),
    ),
]

_REDACTED = "[REDACTED]"


# ---------------------------------------------------------------------------
# Core redaction
# ---------------------------------------------------------------------------


def redact_pii(text: str) -> str:
    """Replace all PII matches in *text* with ``[REDACTED]``.

    Args:
        text: Arbitrary string that may contain PII.

    Returns:
        Sanitised copy with PII spans replaced.
    """
    result = text
    for _label, pattern in _PII_PATTERNS:
        result = pattern.sub(_REDACTED, result)
    return result


def _redact_arg(value: Any) -> Any:
    """Redact a single log-record format argument if it's a string."""
    if isinstance(value, str):
        return redact_pii(value)
    return value


# ---------------------------------------------------------------------------
# Logging filter
# ---------------------------------------------------------------------------


class PiiRedactingFilter(logging.Filter):
    """``logging.Filter`` that scrubs PII from every log record.

    Handles both eager-formatted messages (``record.msg`` is already a
    string with no ``record.args``) and lazy ``%-format`` messages where
    PII may hide inside ``record.args``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_pii(record.msg)

        if record.args is not None:
            if isinstance(record.args, dict):
                record.args = {k: _redact_arg(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(_redact_arg(a) for a in record.args)

        return True


# ---------------------------------------------------------------------------
# Convenience installer
# ---------------------------------------------------------------------------

_FILTER_ATTR = "_bernstein_pii_filter"


def install_pii_filter(
    target: logging.Logger | None = None,
) -> PiiRedactingFilter:
    """Attach a ``PiiRedactingFilter`` to *target* (default: root logger).

    Safe to call multiple times — subsequent calls are no-ops that return the
    existing filter instance.

    Args:
        target: Logger to protect. ``None`` means the root logger.

    Returns:
        The installed (or already-installed) filter instance.
    """
    if target is None:
        target = logging.getLogger()

    existing = getattr(target, _FILTER_ATTR, None)
    if isinstance(existing, PiiRedactingFilter):
        return existing

    pii_filter = PiiRedactingFilter()
    target.addFilter(pii_filter)
    setattr(target, _FILTER_ATTR, pii_filter)
    return pii_filter
