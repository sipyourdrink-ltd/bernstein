"""Canonical stream-signal protocol for adapter stdout.

Defines a tiny text-line vocabulary that any wrapped CLI can emit to
participate in the same lifecycle that stream-json adapters already
expose (completion, question-asking, plan handoff, blocked state).

The signal grammar is intentionally small and trivially producible from
a shell wrapper script:

* Every signal lives on a single line.
* Every signal starts with the literal prefix ``BERNSTEIN:``.
* The next token is the signal kind (e.g. ``COMPLETED``, ``FAILED``,
  ``QUESTION``, ``PLAN_DRAFT``, ``PLAN_READY``, ``BLOCKED``).
* An optional trailing JSON object carries a payload, e.g.
  ``BERNSTEIN:QUESTION {"question": "Proceed?", "options": ["y", "n"]}``.

The parser is robust against malformed lines: it never raises on bad
input - it returns ``None`` so the caller can simply continue scanning
the stream.

The vocabulary is **additive**: stream-json adapters (Claude, Codex)
keep their native protocol. The canonical signals are an optional
overlay so non-stream-json CLIs can join the same event bus.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


#: Literal prefix every canonical signal line starts with. The colon is
#: part of the prefix so a stray ``BERNSTEIN`` mention in normal output
#: cannot be mistaken for a signal.
SIGNAL_PREFIX: str = "BERNSTEIN:"


class SignalKind(StrEnum):
    """Canonical signal kinds emitted by adapter stdout.

    The string values are the wire tokens that follow the
    ``BERNSTEIN:`` prefix on the line. Operators read these directly in
    log tails, so the spelling is fixed and ALL-CAPS.
    """

    COMPLETED = "COMPLETED"
    """Terminal signal: the adapter finished its task successfully."""

    FAILED = "FAILED"
    """Terminal signal: the adapter ran to completion but failed."""

    QUESTION = "QUESTION"
    """The adapter is asking the operator a clarifying question.

    Payload shape: ``{"question": str, "options": list[str] | None,
    "id": str | None}``. The optional ``id`` lets the orchestrator route
    a reply back to the correct in-flight question when multiple are in
    flight on the same adapter (rare, but possible).
    """

    PLAN_DRAFT = "PLAN_DRAFT"
    """A draft plan is being staged; not yet authoritative.

    Payload shape: ``{"markdown": str, "path": str | None}``. When
    ``path`` is given it should be a relative path under ``.sdd/``.
    """

    PLAN_READY = "PLAN_READY"
    """A plan is finalised and ready to drive downstream phases.

    Payload shape: ``{"path": str}``, with ``path`` pointing at the
    markdown artefact under ``.sdd/``.
    """

    BLOCKED = "BLOCKED"
    """The adapter is alive but cannot make progress without help.

    Payload shape: ``{"reason": str, "hint": str | None}``. This is
    explicitly *not* terminal: an orchestrator may resolve the block
    (deliver missing credentials, unblock a peer) and resume the
    adapter.
    """


#: Signal kinds that end a session. Conformance reports flag any adapter
#: run that did not emit at least one of these.
TERMINAL_SIGNAL_KINDS: frozenset[SignalKind] = frozenset({SignalKind.COMPLETED, SignalKind.FAILED})


# ---------------------------------------------------------------------------
# Parsed event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamSignal:
    """One parsed canonical signal.

    Attributes:
        kind: The :class:`SignalKind` member identified on the line.
        payload: Decoded JSON object, or empty dict when the line carried
            no payload. Always a dict - payloads that decode to lists,
            strings, or numbers are rejected at parse time and return
            ``None`` from :func:`parse_signal` since the canonical shape
            is always an object.
        raw_line: The original line text (with trailing newline stripped),
            preserved for log/debug output.
    """

    kind: SignalKind
    payload: dict[str, Any] = field(default_factory=dict)
    raw_line: str = ""

    @property
    def is_terminal(self) -> bool:
        """True when this signal ends the adapter's session."""
        return self.kind in TERMINAL_SIGNAL_KINDS


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_signal(line: str) -> StreamSignal | None:
    """Decode one line of adapter stdout into a :class:`StreamSignal`.

    The parser is deliberately tolerant. It returns ``None`` for any
    line that is not a well-formed canonical signal so the caller can
    treat the stream as a mix of free-form output and structured
    signals without special-casing every variant.

    Args:
        line: One line of adapter stdout. May include or omit the
            trailing newline; leading and trailing whitespace are
            stripped before parsing.

    Returns:
        A :class:`StreamSignal` when the line matches the grammar,
        otherwise ``None``. Lines that match the prefix but carry an
        unknown kind or a malformed JSON payload also yield ``None`` -
        with a debug log entry so operators investigating wrapper
        bugs can find them.
    """
    if not isinstance(line, str):
        return None

    stripped = line.strip()
    if not stripped or not stripped.startswith(SIGNAL_PREFIX):
        return None

    # Strip prefix, then peel off the kind token.
    body = stripped[len(SIGNAL_PREFIX) :].lstrip()
    if not body:
        return None

    # Split into "<KIND>" and optional "<json...>".
    parts = body.split(None, 1)
    kind_token = parts[0]
    payload_text = parts[1].strip() if len(parts) == 2 else ""

    try:
        kind = SignalKind(kind_token)
    except ValueError:
        logger.debug("stream_signals: unknown signal kind %r in line %r", kind_token, stripped)
        return None

    payload: dict[str, Any] = {}
    if payload_text:
        try:
            decoded = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            logger.debug("stream_signals: malformed JSON payload for %s: %s (line=%r)", kind, exc, stripped)
            return None
        if not isinstance(decoded, dict):
            logger.debug(
                "stream_signals: payload for %s is %s, expected object (line=%r)",
                kind,
                type(decoded).__name__,
                stripped,
            )
            return None
        payload = decoded

    return StreamSignal(kind=kind, payload=payload, raw_line=stripped)


def iter_signals(lines: list[str] | tuple[str, ...]) -> list[StreamSignal]:
    """Parse a batch of lines, dropping non-signal noise.

    Convenience helper for callers that want to scan a buffered stdout
    chunk (e.g. a log tail) for canonical signals without writing the
    filter loop themselves. Lines that don't parse are silently
    dropped - that's the whole point of the canonical signal having a
    unique prefix.

    Args:
        lines: An iterable of stdout lines.

    Returns:
        Ordered list of parsed signals in the order they appeared.
    """
    out: list[StreamSignal] = []
    for line in lines:
        signal = parse_signal(line)
        if signal is not None:
            out.append(signal)
    return out


# ---------------------------------------------------------------------------
# Producer-side helpers (for adapter wrappers written in Python)
# ---------------------------------------------------------------------------


def format_signal(kind: SignalKind, payload: dict[str, Any] | None = None) -> str:
    """Render a signal as a wire-format line (no trailing newline).

    Adapter wrappers written in Python can use this helper to emit
    canonical signals without hand-formatting the prefix and JSON.
    Shell wrappers do the same job with a literal ``printf`` - the
    grammar is intentionally small enough that both producers stay in
    sync without sharing a library.

    Args:
        kind: The signal kind.
        payload: Optional JSON-serialisable payload object. Pass
            ``None`` (or an empty dict) to emit a payload-less signal.

    Returns:
        A single line of text suitable for ``print()``.

    Raises:
        TypeError: If ``payload`` is not a dict.
        ValueError: If ``payload`` contains values that are not JSON
            serialisable.
    """
    if payload is None:
        return f"{SIGNAL_PREFIX}{kind.value}"
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be a dict, got {type(payload).__name__}")
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"payload for {kind.value} is not JSON serialisable: {exc}") from exc
    return f"{SIGNAL_PREFIX}{kind.value} {body}"


# ---------------------------------------------------------------------------
# Conformance support
# ---------------------------------------------------------------------------


class MissingTerminalSignal(RuntimeWarning):
    """Raised (or logged) when an adapter run produced no terminal signal.

    The conformance harness uses this as a soft warning class so a
    missing ``COMPLETED``/``FAILED`` line surfaces in the report
    without aborting the rest of the suite.
    """


def has_terminal_signal(signals: list[StreamSignal] | tuple[StreamSignal, ...]) -> bool:
    """Return True when ``signals`` contains at least one terminal kind.

    Args:
        signals: Iterable of parsed signals, typically the output of
            :func:`iter_signals` over an adapter's full stdout log.

    Returns:
        ``True`` if any signal is :attr:`SignalKind.COMPLETED` or
        :attr:`SignalKind.FAILED`, ``False`` otherwise.
    """
    return any(sig.is_terminal for sig in signals)
