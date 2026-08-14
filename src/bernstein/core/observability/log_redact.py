"""PII and credential redaction for logging and persisted traces.

Installs a ``logging.Filter`` on the root logger that automatically replaces
email addresses, phone numbers, SSNs, credit card numbers, and credential
shapes with
``[REDACTED]`` before log records are emitted.

Usage::

    from bernstein.core.observability.log_redact import install_pii_filter

    install_pii_filter()          # attaches to root logger
    install_pii_filter(logger)    # attaches to a specific logger

The filter mutates ``record.msg`` and ``record.args`` in-place so that
downstream handlers (file, console, structured JSON) all receive sanitised
text - no PII is ever written to disk or stdout.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from typing import Any

# ---------------------------------------------------------------------------
# PII patterns - kept in sync with memory_sanitizer._PII_RULES
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

# Credential matches deliberately require either a known prefix/header/block or
# a sensitive field name.  Entropy alone is not a signal: content hashes,
# UUIDs, and base64 payloads are legitimate trace data and must remain stable.
_AUTHORIZATION_PATTERN = re.compile(
    r"((?<![A-Za-z0-9_.-])[\"']?authorization[\"']?\s*:\s*[\"']?[A-Za-z][A-Za-z0-9._~-]*\s+)"
    r"[^\s\"'\\,}]+",
    re.IGNORECASE,
)
_PREFIXED_CREDENTIAL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})(?![A-Za-z0-9])"
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_NAMED_VALUE_PATTERN = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_.-])(?P<quote>[\"']?)(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)"
    r"(?P=quote)\s*(?:=|:)\s*[\"']?)(?P<value>[^\s\"',}\]]{16,})(?P<suffix>[\"']?)"
)


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


def _is_sensitive_name(name: str) -> bool:
    """Return whether *name* explicitly denotes credential material."""
    snake_name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    parts = {part for part in re.split(r"[_.-]+", snake_name.casefold()) if part}
    return bool(parts & {"token", "secret", "password", "key"}) or {"api", "key"} <= parts


def _looks_high_entropy(value: str) -> bool:
    """Conservatively identify random-looking assigned credential values."""
    if len(value) < 16 or len(set(value)) < 8:
        return False
    counts = Counter(value)
    entropy = -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())
    return entropy >= 3.5


def _redact_named_value(match: re.Match[str]) -> str:
    name = match.group("name")
    value = match.group("value")
    if not _is_sensitive_name(name) or not _looks_high_entropy(value):
        return match.group(0)
    return f"{match.group('prefix')}{_REDACTED}{match.group('suffix')}"


def redact_sensitive_text(text: str) -> str:
    """Redact PII and credential-shaped material from arbitrary text.

    Known credential prefixes, authorization headers, and private-key blocks
    are always removed. Random-looking values are removed only when assigned
    to an explicitly sensitive name, preventing entropy-only false positives
    on hashes, UUIDs, and ordinary base64 trace data.
    """
    result = redact_pii(text)
    result = _PRIVATE_KEY_PATTERN.sub(_REDACTED, result)
    result = _AUTHORIZATION_PATTERN.sub(rf"\1{_REDACTED}", result)
    result = _PREFIXED_CREDENTIAL_PATTERN.sub(_REDACTED, result)
    return _NAMED_VALUE_PATTERN.sub(_redact_named_value, result)


def redact_sensitive_bytes(data: bytes) -> bytes:
    """Redact textual credential shapes while preserving non-UTF-8 bytes."""
    text = data.decode("utf-8", errors="surrogateescape")
    return redact_sensitive_text(text).encode("utf-8", errors="surrogateescape")


def _redact_arg(value: Any) -> Any:
    """Redact a single log-record format argument if it's a string."""
    if isinstance(value, str):
        return redact_sensitive_text(value)
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
            record.msg = redact_sensitive_text(record.msg)

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

    Safe to call multiple times - subsequent calls are no-ops that return the
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
