"""Backward-compat shim — re-exports from bernstein.core.observability.correlation."""

from bernstein.core.observability.correlation import (
    CorrelationContext,
    CorrelationFilter,
    create_context,
    generate_correlation_id,
    get_current_context,
    get_current_correlation_id,
    logger,
    set_correlation_id,
    set_current_context,
    setup_correlation_logging,
)

__all__ = [
    "CorrelationContext",
    "CorrelationFilter",
    "create_context",
    "generate_correlation_id",
    "get_current_context",
    "get_current_correlation_id",
    "logger",
    "set_correlation_id",
    "set_current_context",
    "setup_correlation_logging",
]
