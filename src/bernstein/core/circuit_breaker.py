"""Backward-compat shim — re-exports from bernstein.core.observability.circuit_breaker."""

from bernstein.core.observability.circuit_breaker import (
    check_budget_violations,
    check_guardrail_violations,
    check_scope_violations,
    enforce_kill_signal,
    log_kill_event,
    logger,
    write_quarantine_metadata,
)

__all__ = [
    "check_budget_violations",
    "check_guardrail_violations",
    "check_scope_violations",
    "enforce_kill_signal",
    "log_kill_event",
    "logger",
    "write_quarantine_metadata",
]
