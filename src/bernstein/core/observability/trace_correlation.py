"""W3C Trace Context correlation for linking Bernstein traces to agent traces.

Generates and parses W3C ``traceparent`` headers so that Bernstein's
orchestrator spans and the spans emitted by spawned CLI agents (e.g. Claude
Code, Codex) can be correlated in a single distributed trace.

See https://www.w3.org/TR/trace-context/ for the specification.

Usage::

    from bernstein.core.trace_correlation import (
        generate_trace_context,
        format_traceparent,
        build_correlation_env,
    )

    ctx = generate_trace_context()
    env = build_correlation_env(ctx)
    # Pass *env* into subprocess.Popen(..., env={**os.environ, **env})
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# W3C traceparent: version-traceid-parentid-traceflags
# version   = 2HEXDIG (currently "00")
# trace-id  = 32HEXDIG
# parent-id = 16HEXDIG  (a.k.a. span-id)
# flags     = 2HEXDIG   ("01" = sampled)
_TRACEPARENT_RE = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")

#: Current W3C traceparent version.
_VERSION = "00"

#: Default trace flags — sampled.
_DEFAULT_FLAGS = "01"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceContext:
    """Immutable W3C-compatible trace context.

    Attributes:
        trace_id: 32 lowercase hex characters identifying the trace.
        span_id: 16 lowercase hex characters identifying the current span.
        trace_flags: 2 lowercase hex characters (``"01"`` = sampled).
    """

    trace_id: str
    span_id: str
    trace_flags: str = _DEFAULT_FLAGS


@dataclass(frozen=True)
class CorrelationRecord:
    """Immutable record linking a Bernstein trace to an agent session.

    Attributes:
        bernstein_trace_id: Trace ID from the Bernstein orchestrator.
        agent_session_id: Session ID reported by the spawned agent.
        task_id: Bernstein task ID the agent was working on.
        traceparent: Full W3C traceparent header string.
        created_at: ISO-8601 timestamp when the record was created.
    """

    bernstein_trace_id: str
    agent_session_id: str
    task_id: str
    traceparent: str
    created_at: str


# ---------------------------------------------------------------------------
# Context generation and formatting
# ---------------------------------------------------------------------------


def generate_trace_context() -> TraceContext:
    """Generate a new random :class:`TraceContext`.

    Uses :func:`secrets.token_hex` for cryptographically-strong random IDs.

    Returns:
        A freshly generated :class:`TraceContext` with sampled flag set.
    """
    return TraceContext(
        trace_id=secrets.token_hex(16),  # 16 bytes -> 32 hex chars
        span_id=secrets.token_hex(8),  # 8 bytes -> 16 hex chars
        trace_flags=_DEFAULT_FLAGS,
    )


def format_traceparent(ctx: TraceContext) -> str:
    """Format a :class:`TraceContext` as a W3C ``traceparent`` header value.

    Args:
        ctx: The trace context to format.

    Returns:
        A string like ``"00-<trace_id>-<span_id>-<trace_flags>"``.
    """
    return f"{_VERSION}-{ctx.trace_id}-{ctx.span_id}-{ctx.trace_flags}"


def parse_traceparent(header: str) -> TraceContext | None:
    """Parse a W3C ``traceparent`` header into a :class:`TraceContext`.

    Args:
        header: The raw header value (e.g.
            ``"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"``).

    Returns:
        A :class:`TraceContext` on success, or ``None`` if the header is
        malformed or uses an unsupported version.
    """
    match = _TRACEPARENT_RE.match(header.strip().lower())
    if match is None:
        return None

    version, trace_id, span_id, trace_flags = match.groups()

    # Reject all-zero trace-id or span-id (invalid per spec).
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None

    # Only accept version "00" — future versions may change the format.
    if version != _VERSION:
        return None

    return TraceContext(
        trace_id=trace_id,
        span_id=span_id,
        trace_flags=trace_flags,
    )


# ---------------------------------------------------------------------------
# Environment variable injection
# ---------------------------------------------------------------------------


def build_correlation_env(ctx: TraceContext) -> dict[str, str]:
    """Build environment variables to inject into a spawned agent subprocess.

    The returned dict contains:

    - ``TRACEPARENT`` — standard W3C header consumed by OpenTelemetry SDKs.
    - ``BERNSTEIN_TRACE_ID`` — bare 32-hex trace ID for non-OTel consumers.
    - ``BERNSTEIN_SPAN_ID`` — bare 16-hex span ID for non-OTel consumers.

    Args:
        ctx: The trace context to encode.

    Returns:
        A ``dict[str, str]`` suitable for merging into ``os.environ``.
    """
    return {
        "TRACEPARENT": format_traceparent(ctx),
        "BERNSTEIN_TRACE_ID": ctx.trace_id,
        "BERNSTEIN_SPAN_ID": ctx.span_id,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_correlation(record: CorrelationRecord, output_dir: Path) -> Path:
    """Append a :class:`CorrelationRecord` to a JSONL file.

    The file is named ``trace_correlations.jsonl`` inside *output_dir*.
    Each line is a self-contained JSON object for easy streaming ingestion.

    Args:
        record: The correlation record to persist.
        output_dir: Directory where the JSONL file will be created/appended.

    Returns:
        The :class:`Path` to the JSONL file that was written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "trace_correlations.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(record), separators=(",", ":")) + "\n")
    return path


def create_correlation_record(
    ctx: TraceContext,
    agent_session_id: str,
    task_id: str,
) -> CorrelationRecord:
    """Convenience factory for :class:`CorrelationRecord`.

    Args:
        ctx: The trace context associated with the agent spawn.
        agent_session_id: The agent's session identifier.
        task_id: The Bernstein task ID.

    Returns:
        A fully-populated :class:`CorrelationRecord`.
    """
    return CorrelationRecord(
        bernstein_trace_id=ctx.trace_id,
        agent_session_id=agent_session_id,
        task_id=task_id,
        traceparent=format_traceparent(ctx),
        created_at=datetime.now(UTC).isoformat(),
    )
