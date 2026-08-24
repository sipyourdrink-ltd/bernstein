"""Parse Goose ``--output-format stream-json`` events.

Goose emits newline-delimited JSON under ``--output-format stream-json``. The
authoritative accounting rides on a terminal ``envelope`` event whose
``metadata`` carries the provider's own token breakdown (``tokens`` with
``total``/``input``/``output``/``cache_read``/``cache_write``) and a
``cost_usd`` figure. Errors surface as separate ``error`` events.

Upstream quirk (issue #3679): ``goose run`` returns ``Ok(())`` on every
terminal path once the agent started, and ``metadata.status`` is the literal
``"completed"`` in both the success and the error match arms. Under
``--output-format stream-json`` an ``error`` event is emitted but a
``complete`` event still follows and the exit status is still 0. So the
``error`` event is the authoritative failure signal - ``status: "completed"``
must NOT be treated as success.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

#: Envelope event type carrying the provider's token/cost accounting.
_EVENT_ENVELOPE = "envelope"
#: Error event type - the authoritative failure signal.
_EVENT_ERROR = "error"
#: The literal status value goose writes in both terminal match arms.
_STATUS_COMPLETED = "completed"


@dataclass(frozen=True)
class GooseStreamResult:
    """Parsed outcome of a Goose stream-json session log.

    Attributes:
        tokens_in: Input tokens from the envelope's ``tokens.input``.
        tokens_out: Output tokens from the envelope's ``tokens.output``.
        cost_usd: Provider-reported cost from the envelope's ``cost_usd``.
        error: Error message when an ``error`` event was seen, else None.
        completed: Whether an envelope with ``status == "completed"`` was seen.
    """

    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    completed: bool = False

    @property
    def is_success(self) -> bool:
        """Whether the run succeeded.

        Success is the ABSENCE of an ``error`` event, never the presence of a
        ``completed`` envelope - goose emits ``status: "completed"`` in both
        the success and error match arms, so ``completed`` alone is not a
        success signal.
        """
        return self.error is None


def _as_dict(value: Any) -> dict[str, Any] | None:
    """Return ``value`` as a ``dict[str, Any]`` when it is a mapping, else None."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _coerce_int(value: Any) -> int:
    """Return ``value`` as a non-negative int, or ``0`` when not coercible."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def _coerce_float(value: Any) -> float:
    """Return ``value`` as a non-negative float, or ``0.0`` when not coercible."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result > 0 else 0.0


def parse_goose_stream(text: str) -> GooseStreamResult:
    """Parse Goose stream-json NDJSON into a :class:`GooseStreamResult`.

    Args:
        text: Raw session-log text (line-delimited stream-json).

    Returns:
        A :class:`GooseStreamResult` with the envelope's token/cost
        accounting and the first ``error`` event message (if any).
    """
    tokens_in = 0
    tokens_out = 0
    cost_usd = 0.0
    error: str | None = None
    completed = False

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rec_dict = _as_dict(rec)
        if rec_dict is None:
            continue

        event_type = rec_dict.get("type")
        if event_type == _EVENT_ERROR:
            message = rec_dict.get("message") or rec_dict.get("error") or "goose error"
            error = str(message)
            continue
        if event_type != _EVENT_ENVELOPE:
            continue

        metadata = _as_dict(rec_dict.get("metadata"))
        if metadata is None:
            continue
        if metadata.get("status") == _STATUS_COMPLETED:
            completed = True
        tokens = _as_dict(metadata.get("tokens"))
        if tokens is not None:
            tokens_in = _coerce_int(tokens.get("input"))
            tokens_out = _coerce_int(tokens.get("output"))
        cost_usd = _coerce_float(metadata.get("cost_usd"))

    return GooseStreamResult(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        error=error,
        completed=completed,
    )
