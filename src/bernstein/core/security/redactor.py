"""File-level redaction wrapper for debug-bundle text artefacts.

This is a thin orchestration layer over the lower-level pattern set in
:mod:`bernstein.core.observability.debug_bundle`. It exposes a stable,
text-only API that:

- Blanks API keys, tokens, secrets, passwords, bearer headers, JWTs, SSH
  keys, and URL-embedded credentials.
- Collapses absolute paths under ``$HOME`` to ``~``.
- Removes values of environment variables whose names contain
  ``KEY``/``TOKEN``/``SECRET``/``PASSWORD`` from text-style dumps
  (``NAME=value`` and ``NAME: value``).

The wrapper is intentionally text-only and idempotent so callers can
feed it any UTF-8 file content without bespoke parsing.

Usage::

    from bernstein.core.security.redactor import redact_text, redact_file, mask

    cleaned = redact_text(raw)
    cleaned, count = redact_file(Path("bernstein.yaml"))
    safe = mask(api_key)  # short-value helper for logger.info("got %s", mask(token))
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

from bernstein.core.observability.debug_bundle import redact_secrets

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["collapse_home", "mask", "redact_file", "redact_text"]

#: Cached ``(home value, compiled pattern)``. Keyed on the home value rather
#: than compiled once and kept forever: ``$HOME`` is read from the environment,
#: and a process that changes it would otherwise keep collapsing against the
#: path it started with.
_HOME_CACHE: tuple[str, re.Pattern[str] | None] | None = None


def _home_prefix() -> str:
    """Return the current home directory, without a trailing separator."""
    home = os.environ.get("HOME") or os.path.expanduser("~")
    # Both separators: a value can arrive from a Windows environment
    # (``C:\\Users\\x\\``) as readily as a POSIX one, and only the POSIX
    # spelling was being trimmed.
    return home.rstrip("/\\")


def _home_pattern() -> re.Pattern[str] | None:
    """Return a compiled regex matching the current ``$HOME`` prefix.

    Compiled lazily because ``$HOME`` can be unset in CI runners; we
    return ``None`` in that case and skip the collapse step.

    ``None`` is also returned when the home directory is a filesystem root -
    ``/`` on POSIX, ``C:\\`` on Windows. Trimming the trailing separator
    leaves such a value empty (or a bare drive letter), and an empty pattern
    matches at *every* position: ``re.compile("").sub("~", "abc")`` is
    ``"~a~b~c~"``. That is not a redaction failing to fire, it is every string
    that passes through the redactor being rewritten character by character.

    ``HOME=/`` is the ordinary configuration for a container running as root,
    and :func:`redact_text` runs on the write path of the work ledger *before*
    each entry is hashed - so the corruption would be sealed into the chain,
    not merely displayed. Collapsing the root to ``~`` would be wrong even if
    it worked, since every absolute path on the host begins with it.
    """
    global _HOME_CACHE
    home = _home_prefix()
    if _HOME_CACHE is not None and _HOME_CACHE[0] == home:
        return _HOME_CACHE[1]
    # ``expanduser`` returns "~" unchanged when it cannot resolve one.
    # ``_is_filesystem_root`` covers the emptied-by-trimming cases above.
    pattern = None if not home or home == "~" or _is_filesystem_root(home) else re.compile(re.escape(home))
    _HOME_CACHE = (home, pattern)
    return pattern


def _is_filesystem_root(home: str) -> bool:
    """Is *home* a filesystem root rather than a directory inside one?

    Reached with the trailing separator already trimmed, so ``/`` arrives as
    ``""`` and ``C:\\`` as ``"C:"``. A UNC share root (``\\\\server\\share``)
    trims to ``\\\\server\\share`` and is left alone - it is a real directory
    with a real prefix, unlike the two above.
    """
    if not home:
        return True
    # A bare drive designator: "C:" is what "C:\\" trims down to.
    return len(home) == 2 and home[1] == ":" and home[0].isalpha()


def collapse_home(text: str) -> str:
    """Replace occurrences of ``$HOME`` with ``~`` in *text*."""
    pattern = _home_pattern()
    if pattern is None:
        return text
    return pattern.sub("~", text)


def redact_text(text: str) -> tuple[str, int]:
    """Redact secrets and collapse ``$HOME`` references in *text*.

    Args:
        text: Arbitrary UTF-8 string that may contain secrets or
            absolute paths.

    Returns:
        A 2-tuple of (redacted text, count of secret redactions
        applied). The home-collapse step is not counted because it is
        cosmetic, not a security action.
    """
    cleaned, count = redact_secrets(text)
    cleaned, broker_count = _scrub_broker_registry(cleaned)
    cleaned = collapse_home(cleaned)
    return cleaned, count + broker_count


def _scrub_broker_registry(text: str) -> tuple[str, int]:
    """Replace any value registered with the secrets broker with ``***``.

    The registry is consulted lazily and tolerantly: import failures or an
    empty registry are no-ops so this function never breaks the broader
    redaction pipeline.
    """
    try:
        from bernstein.core.security.secrets_broker import get_redactable_values
    except Exception:
        return text, 0
    values = get_redactable_values()
    if not values:
        return text, 0
    out = text
    count = 0
    # Replace longest values first so prefixes never mask longer matches.
    for value in sorted(values, key=len, reverse=True):
        if value and value in out:
            count += out.count(value)
            out = out.replace(value, "***")
    return out, count


def mask(value: Any, *, keep: int = 0) -> str:
    """Mask an individual short value for safe inclusion in a log line.

    Use this for credential-shaped scalars (API keys, bearer tokens, OAuth
    response bodies, JWT signatures) where the file-level
    :func:`redact_text` pipeline is overkill but you still want a
    one-shot, hard-to-misuse helper at the call site::

        logger.info("token issued: %s", mask(token))
        logger.error("OAuth failed: %s %s", status, mask(resp.text))

    Args:
        value: Anything stringifiable. ``None`` becomes ``"<none>"`` so
            log lines stay scannable without leaking type info.
        keep: Number of trailing characters to keep visible (default
            ``0``). Use sparingly for correlation; values above ``4``
            risk re-exposing short secrets and are clamped.

    Returns:
        A redacted string of the form ``"***"`` (default) or
        ``"***abcd"`` when ``keep > 0`` AND the input is longer than
        ``keep`` characters. Inputs that are ``<= keep`` characters
        long are fully masked to ``"***"`` rather than exposing their
        entire value (otherwise a 4-character secret with ``keep=4``
        would be printed verbatim). Empty strings render as
        ``"<empty>"`` so a missing secret is visually distinct from a
        masked one.
    """
    if value is None:
        return "<none>"
    text = str(value)
    if not text:
        return "<empty>"
    keep = max(0, min(keep, 4))
    if keep == 0 or len(text) <= keep:
        return "***"
    return f"***{text[-keep:]}"


def redact_file(path: Path) -> tuple[str, int]:
    """Read *path* as UTF-8 and run :func:`redact_text` over its body.

    Args:
        path: File to read. Missing or unreadable files yield an empty
            string and zero redactions; callers decide how to surface
            the absence.

    Returns:
        A 2-tuple of (redacted text, count of secret redactions).
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", 0
    return redact_text(raw)
