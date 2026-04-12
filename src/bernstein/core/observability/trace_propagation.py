"""Distributed tracing context propagation across Bernstein components.

Implements W3C Trace Context (``traceparent``) propagation so that spans
created in different Bernstein components (task server, spawner, agents)
can be stitched into a single distributed trace.

See https://www.w3.org/TR/trace-context/ for the specification.

Usage::

    from bernstein.core.observability.trace_propagation import (
        TraceContext,
        new_trace,
        child_span,
        to_traceparent,
        from_traceparent,
        inject_headers,
        extract_context,
    )

    root = new_trace()
    child = child_span(root)
    headers: dict[str, str] = {}
    inject_headers(child, headers)
    # headers == {"traceparent": "00-<trace_id>-<span_id>-01"}

    recovered = extract_context(headers)
    assert recovered == child
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

# W3C traceparent: version-traceid-parentid-traceflags
# version   = 2HEXDIG (currently "00")
# trace-id  = 32HEXDIG
# parent-id = 16HEXDIG  (a.k.a. span-id)
# flags     = 2HEXDIG   ("01" = sampled)
_TRACEPARENT_RE = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")

#: Current W3C traceparent version.
_VERSION = "00"

#: Trace flags value when sampled.
_FLAGS_SAMPLED = "01"

#: Trace flags value when not sampled.
_FLAGS_NOT_SAMPLED = "00"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceContext:
    """Immutable W3C-compatible distributed trace context.

    Attributes:
        trace_id: 32 lowercase hex characters identifying the whole trace.
        span_id: 16 lowercase hex characters identifying the current span.
        parent_span_id: 16 lowercase hex characters of the parent span,
            or ``None`` for root spans.
        sampled: Whether this trace is sampled for recording.
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None
    sampled: bool


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------


def generate_trace_id() -> str:
    """Create a new W3C-compatible trace ID (32 lowercase hex characters).

    Uses :func:`secrets.token_hex` for cryptographically-strong randomness.

    Returns:
        A 32-character lowercase hex string.
    """
    return secrets.token_hex(16)


def generate_span_id() -> str:
    """Create a new span ID (16 lowercase hex characters).

    Uses :func:`secrets.token_hex` for cryptographically-strong randomness.

    Returns:
        A 16-character lowercase hex string.
    """
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# Context factories
# ---------------------------------------------------------------------------


def new_trace(*, sampled: bool = True) -> TraceContext:
    """Create a root :class:`TraceContext` with fresh trace and span IDs.

    Args:
        sampled: Whether the trace should be sampled. Defaults to ``True``.

    Returns:
        A new root :class:`TraceContext` with ``parent_span_id`` set to
        ``None``.
    """
    return TraceContext(
        trace_id=generate_trace_id(),
        span_id=generate_span_id(),
        parent_span_id=None,
        sampled=sampled,
    )


def child_span(parent: TraceContext) -> TraceContext:
    """Create a child span from *parent*, inheriting its trace ID.

    The new context shares the same ``trace_id`` and ``sampled`` flag.  Its
    ``span_id`` is freshly generated and its ``parent_span_id`` is the
    parent's ``span_id``.

    Args:
        parent: The parent trace context.

    Returns:
        A new :class:`TraceContext` representing a child span.
    """
    return TraceContext(
        trace_id=parent.trace_id,
        span_id=generate_span_id(),
        parent_span_id=parent.span_id,
        sampled=parent.sampled,
    )


# ---------------------------------------------------------------------------
# W3C traceparent serialisation / deserialisation
# ---------------------------------------------------------------------------


def to_traceparent(ctx: TraceContext) -> str:
    """Format a :class:`TraceContext` as a W3C ``traceparent`` header value.

    The output follows the format
    ``"00-{trace_id}-{span_id}-{flags}"`` where *flags* is ``"01"`` when
    sampled and ``"00"`` otherwise.

    Args:
        ctx: The trace context to serialise.

    Returns:
        A W3C traceparent header string.
    """
    flags = _FLAGS_SAMPLED if ctx.sampled else _FLAGS_NOT_SAMPLED
    return f"{_VERSION}-{ctx.trace_id}-{ctx.span_id}-{flags}"


def from_traceparent(header: str) -> TraceContext | None:
    """Parse a W3C ``traceparent`` header into a :class:`TraceContext`.

    Returns ``None`` when the header is malformed, uses an unsupported
    version, or contains all-zero trace/span IDs (invalid per spec).

    Note:
        The W3C ``traceparent`` header does not carry the parent span ID,
        so the returned context will have ``parent_span_id`` set to
        ``None``.

    Args:
        header: The raw header value, e.g.
            ``"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"``.

    Returns:
        A :class:`TraceContext` on success, or ``None`` if parsing fails.
    """
    match = _TRACEPARENT_RE.match(header.strip().lower())
    if match is None:
        return None

    version, trace_id, span_id, flags = match.groups()

    # Reject all-zero trace-id or span-id (invalid per spec).
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None

    # Only accept version "00" — future versions may change the format.
    if version != _VERSION:
        return None

    return TraceContext(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        sampled=flags == _FLAGS_SAMPLED,
    )


# ---------------------------------------------------------------------------
# HTTP header injection / extraction
# ---------------------------------------------------------------------------


def inject_headers(ctx: TraceContext, headers: dict[str, str]) -> None:
    """Add W3C ``traceparent`` to an HTTP headers dict (mutates in place).

    Args:
        ctx: The trace context to propagate.
        headers: A mutable ``dict[str, str]`` that will receive the
            ``"traceparent"`` key.
    """
    headers["traceparent"] = to_traceparent(ctx)


def extract_context(headers: dict[str, str]) -> TraceContext | None:
    """Extract a :class:`TraceContext` from HTTP headers.

    Looks for the ``traceparent`` key (case-insensitive) and delegates to
    :func:`from_traceparent`.

    Args:
        headers: An HTTP headers dict.

    Returns:
        A :class:`TraceContext` if a valid ``traceparent`` header is found,
        otherwise ``None``.
    """
    # Normalise header keys to lowercase for case-insensitive lookup.
    lower = {k.lower(): v for k, v in headers.items()}
    raw = lower.get("traceparent")
    if raw is None:
        return None
    return from_traceparent(raw)
