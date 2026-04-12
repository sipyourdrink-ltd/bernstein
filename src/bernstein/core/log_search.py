"""Backward-compat shim — re-exports from bernstein.core.observability.log_search."""

from bernstein.core.observability.log_search import (
    LogEntry,
    LogSearchIndex,
    LogSearchResult,
)

__all__ = [
    "LogEntry",
    "LogSearchIndex",
    "LogSearchResult",
]
