"""Backward-compat shim — re-exports from bernstein.core.observability.rate_limit_tracker."""

from bernstein.core.observability.rate_limit_tracker import (
    RateLimitTracker,
    RequestPriority,
    ThrottleState,
    UnattendedRetryPolicy,
    is_unattended_mode,
    logger,
)

__all__ = [
    "RateLimitTracker",
    "RequestPriority",
    "ThrottleState",
    "UnattendedRetryPolicy",
    "is_unattended_mode",
    "logger",
]
