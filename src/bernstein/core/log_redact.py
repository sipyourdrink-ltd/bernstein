"""Backward-compat shim — re-exports from bernstein.core.observability.log_redact."""

from bernstein.core.observability.log_redact import (
    PiiRedactingFilter,
    install_pii_filter,
    redact_pii,
)

__all__ = [
    "PiiRedactingFilter",
    "install_pii_filter",
    "redact_pii",
]
