"""Per-call cost-meter and observability envelope for MCP tool responses.

Every Bernstein MCP tool returns a JSON string. This module wraps that raw
payload in a uniform envelope that carries observability metadata alongside
the tool result:

* ``result``  - the tool's original JSON payload, unchanged.
* ``_meter``  - a per-call cost / latency / trace record.

The envelope is opt-in per process via ``BERNSTEIN_MCP_COST_METER`` so an
operator who wants the bare tool payload (the historical shape) can disable
it without code changes. When disabled, :func:`wrap_envelope` returns the raw
payload string untouched, so existing clients keep working.

The meter record is deliberately small and self-describing so any MCP client
can surface it without a Bernstein-specific schema:

    {
      "tool": "bernstein_status",
      "call_id": "b1c2...",
      "latency_ms": 12.4,
      "cost_usd": 0.0,
      "ok": true,
      "ts": "2026-05-20T10:11:12.345Z"
    }

``cost_usd`` is best-effort: the MCP server proxies to the task server and
does not itself spend model tokens, so the per-call figure is ``0.0`` unless
a handler attaches a cost. The field exists so the envelope shape is stable
and a future cost-attributing handler does not change the contract.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Env var that toggles the per-call envelope. Defaults to ON; set to a
#: falsey value to return the bare tool payload (the historical shape).
COST_METER_ENV: str = "BERNSTEIN_MCP_COST_METER"

#: Values that disable the meter when set in :data:`COST_METER_ENV`.
_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})


def cost_meter_enabled() -> bool:
    """Return whether the per-call envelope should wrap tool responses.

    Reads :data:`COST_METER_ENV` at call time so an operator can toggle the
    envelope without restarting (the value is consulted on each tool call).
    Defaults to ``True`` when the var is unset or empty.
    """
    raw = os.environ.get(COST_METER_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _DISABLED_VALUES


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class CallMeter:
    """Mutable per-call observability record built during a tool call.

    A handler obtains one via :func:`measure_call`, may attach a cost with
    :meth:`add_cost`, and the context manager finalises latency + status on
    exit. The finalised record is serialised into the response envelope.
    """

    tool: str
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cost_usd: float = 0.0
    ok: bool = True
    error: str | None = None
    started_at: str = field(default_factory=_utc_now_iso)
    latency_ms: float = 0.0
    _start_perf: float = field(default_factory=time.perf_counter, repr=False)

    def add_cost(self, usd: float) -> None:
        """Accumulate an estimated USD cost for this call."""
        self.cost_usd += usd

    def finalise(self) -> None:
        """Record the elapsed wall-clock latency for this call in millis."""
        self.latency_ms = (time.perf_counter() - self._start_perf) * 1000.0

    def to_dict(self) -> dict[str, Any]:
        """Return the meter as a JSON-serialisable dict for the envelope."""
        record: dict[str, Any] = {
            "tool": self.tool,
            "call_id": self.call_id,
            "latency_ms": round(self.latency_ms, 3),
            "cost_usd": round(self.cost_usd, 6),
            "ok": self.ok,
            "ts": self.started_at,
        }
        if self.error is not None:
            record["error"] = self.error
        return record


@contextmanager
def measure_call(tool: str) -> Iterator[CallMeter]:
    """Context manager that times a tool call and records its outcome.

    On normal exit the latency is recorded and ``ok`` stays ``True``. If the
    block raises, ``ok`` is set ``False`` and the exception message is stored
    on the meter before the exception propagates, so the caller can still
    serialise a meter for a failed call.

    Args:
        tool: The MCP-advertised tool name being measured.

    Yields:
        A :class:`CallMeter` the handler may mutate (e.g. attach a cost).
    """
    meter = CallMeter(tool=tool)
    try:
        yield meter
    except Exception as exc:
        meter.ok = False
        meter.error = str(exc)
        meter.finalise()
        raise
    else:
        meter.finalise()


def meter_schema() -> dict[str, Any]:
    """JSON schema of the ``_meter`` record exactly as :meth:`CallMeter.to_dict` emits it."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["tool", "call_id", "latency_ms", "cost_usd", "ok", "ts"],
        "properties": {
            "tool": {"type": "string"},
            "call_id": {"type": "string"},
            "latency_ms": {"type": "number"},
            "cost_usd": {"type": "number"},
            "ok": {"type": "boolean"},
            "ts": {"type": "string"},
            "error": {"type": "string"},
        },
    }


def envelope_schema(payload_schema: dict[str, Any]) -> dict[str, Any]:
    """JSON schema of the meter envelope wrapping ``payload_schema``.

    Describes :func:`wrap_envelope`'s output when the meter is enabled: the
    tool's parsed payload under ``result`` plus the per-call ``_meter``
    record. Callers advertising an ``outputSchema`` use this when
    :func:`cost_meter_enabled` is true and the bare ``payload_schema``
    otherwise, so the declared schema always describes the envelope as it is
    actually emitted (#3086).

    Args:
        payload_schema: Schema of the tool's own JSON payload.

    Returns:
        The envelope schema.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["result", "_meter"],
        "properties": {
            "result": payload_schema,
            "_meter": meter_schema(),
        },
    }


def wrap_envelope(payload: str, meter: CallMeter) -> str:
    """Wrap a tool's raw JSON payload in the cost-meter envelope.

    When the meter is disabled (:func:`cost_meter_enabled` is ``False``) the
    payload is returned untouched so existing clients see the historical
    shape. Otherwise the result is a JSON object with two keys: ``result``
    (the parsed original payload) and ``_meter`` (the call record).

    The original payload is parsed so a JSON result nests as structured data
    rather than an escaped string; if it is not valid JSON it is carried
    verbatim under ``result`` as a string.

    Args:
        payload: The tool handler's JSON string result.
        meter: The finalised per-call meter.

    Returns:
        The enveloped JSON string, or ``payload`` unchanged when disabled.
    """
    if not cost_meter_enabled():
        return payload
    try:
        parsed: Any = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        parsed = payload
    return json.dumps({"result": parsed, "_meter": meter.to_dict()})
