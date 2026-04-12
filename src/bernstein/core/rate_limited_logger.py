"""Backward-compat shim — re-exports from bernstein.core.observability.rate_limited_logger."""

from bernstein.core.observability.rate_limited_logger import (
    LogDeduplicator,
    RateLimitedLogFilter,
    install_rate_limited_filter,
)

__all__ = [
    "LogDeduplicator",
    "RateLimitedLogFilter",
    "install_rate_limited_filter",
]
