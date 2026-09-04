"""Interactive tool-call approval (op-002).

Provides a file-backed queue of pending tool-call approvals that can be
resolved mid-run via the TUI dashboard, the HTTP ``/approvals`` API, or
``bernstein approve``/``bernstein reject`` on the command line.

The queue plugs into the existing security layer: whenever a tool
invocation misses the always-allow rules AND ``bernstein.yaml`` enables
``approvals.interactive``, the gate pushes a :class:`PendingApproval`
onto the queue and blocks the agent until an operator resolves it or the
TTL expires.

Backward compatibility
----------------------
Prior code imported the task-level ``ApprovalGate`` / ``ApprovalMode``
pair from ``bernstein.core.approval``. Those names are re-exported here
so existing call sites keep working while the tool-call queue lives
alongside them.
"""

from __future__ import annotations

from bernstein.core.approval.models import (
    ApprovalDecision,
    ApprovalNonceExpired,
    ApprovalNonceMismatch,
    ApprovalPrincipal,
    ApprovalPrincipalRequired,
    ApprovalTimeoutError,
    PendingApproval,
    PrincipalKind,
    ResolvedApproval,
    internal_principal,
    local_shell_principal,
)
from bernstein.core.approval.queue import (
    DEFAULT_TTL_SECONDS,
    ApprovalQueue,
    get_default_queue,
    promote_to_always_allow,
)

# Backwards-compatibility re-exports. The task-level approval gate pre-dates
# op-002 and lives under the security package; several callers (and a few
# tests) still import public and private helpers from
# ``bernstein.core.approval`` so we forward every top-level attribute of
# :mod:`bernstein.core.security.approval`.
from bernstein.core.security import approval as _legacy_approval
from bernstein.core.security.approval import (
    ApprovalGate,
    ApprovalMode,
    ApprovalResult,
)


def __getattr__(name: str) -> object:
    """Forward lookups for legacy symbols to the task-level approval module."""
    if hasattr(_legacy_approval, name):
        return getattr(_legacy_approval, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "ApprovalDecision",
    "ApprovalGate",
    "ApprovalMode",
    "ApprovalNonceExpired",
    "ApprovalNonceMismatch",
    "ApprovalPrincipal",
    "ApprovalPrincipalRequired",
    "ApprovalQueue",
    "ApprovalResult",
    "ApprovalTimeoutError",
    "PendingApproval",
    "PrincipalKind",
    "ResolvedApproval",
    "get_default_queue",
    "internal_principal",
    "local_shell_principal",
    "promote_to_always_allow",
]
