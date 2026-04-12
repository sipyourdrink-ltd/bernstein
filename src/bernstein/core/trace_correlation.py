"""Backward-compat shim — re-exports from bernstein.core.observability.trace_correlation."""

from bernstein.core.observability.trace_correlation import (
    CorrelationRecord,
    TraceContext,
    build_correlation_env,
    create_correlation_record,
    format_traceparent,
    generate_trace_context,
    parse_traceparent,
    save_correlation,
)

__all__ = [
    "CorrelationRecord",
    "TraceContext",
    "build_correlation_env",
    "create_correlation_record",
    "format_traceparent",
    "generate_trace_context",
    "parse_traceparent",
    "save_correlation",
]
