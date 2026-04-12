"""Backward-compat shim — re-exports from bernstein.core.observability.log_context."""

from bernstein.core.observability.log_context import (
    ErrorContext,
    LogContext,
    build_error_context,
    error_context,
    get_current_context,
    log_with_context,
    logger,
)

__all__ = [
    "ErrorContext",
    "LogContext",
    "build_error_context",
    "error_context",
    "get_current_context",
    "log_with_context",
    "logger",
]
