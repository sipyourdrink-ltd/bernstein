"""Prometheus counters and gauges for the ACP bridge.

Metrics are registered against the same dedicated registry the rest of
Bernstein uses (``bernstein.core.observability.prometheus.registry``) so
that scraping ``/metrics`` on the existing task server picks them up
without further wiring.
"""

from __future__ import annotations

import contextlib
from typing import Final

from bernstein.core.observability.prometheus import (
    Counter,
    Gauge,
    registry,
)

# Allowed outcomes — closed set to avoid label cardinality explosions.
VALID_OUTCOMES: Final[frozenset[str]] = frozenset({"ok", "error", "rejected", "cancelled", "permission_denied"})

acp_messages_total: Counter = Counter(
    "bernstein_acp_messages_total",
    "ACP JSON-RPC messages handled, partitioned by method and outcome.",
    labelnames=["method", "outcome"],
    registry=registry,
)

acp_active_sessions: Gauge = Gauge(
    "bernstein_acp_active_sessions",
    "Number of currently open ACP sessions.",
    registry=registry,
)


def record_acp_message(method: str, outcome: str) -> None:
    """Increment :data:`acp_messages_total` with sanitised labels.

    Unknown outcomes are bucketed under ``"error"`` to avoid runaway
    label cardinality.

    Args:
        method: ACP method name (e.g. ``"prompt"``).
        outcome: One of :data:`VALID_OUTCOMES`; anything else is bucketed
            to ``"error"``.
    """
    sanitised_outcome = outcome if outcome in VALID_OUTCOMES else "error"
    sanitised_method = (method or "unknown").strip() or "unknown"
    # Metrics never break the request path; suppress all errors.
    with contextlib.suppress(Exception):
        acp_messages_total.labels(method=sanitised_method, outcome=sanitised_outcome).inc()


def set_active_sessions(count: int) -> None:
    """Set the :data:`acp_active_sessions` gauge.

    Args:
        count: Current number of open ACP sessions.
    """
    with contextlib.suppress(Exception):
        acp_active_sessions.set(max(0, int(count)))


__all__ = [
    "VALID_OUTCOMES",
    "acp_active_sessions",
    "acp_messages_total",
    "record_acp_message",
    "set_active_sessions",
]
