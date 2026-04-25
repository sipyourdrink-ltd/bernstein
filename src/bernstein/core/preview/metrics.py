"""Prometheus metrics for ``bernstein preview``.

Two metric families:

* ``preview_active_total{provider,sandbox}`` — gauge tracking how many
  previews are currently live, partitioned by tunnel provider and
  sandbox backend so an operator can see whether the cluster is shy of
  e.g. Cloudflared sessions or Modal sandboxes.
* ``preview_link_issued_total{auth_mode}`` — counter incremented every
  time a preview link is issued, partitioned by the auth mode (``basic``,
  ``token``, ``none``) so security teams can audit how many links are
  being shared with no authentication.

The metrics live on the shared :data:`bernstein.core.observability.prometheus.registry`
collector so they show up next to the rest of the bernstein metrics on
``/metrics``. Importing this module is safe even when prometheus_client
is unavailable — the underlying metric stubs are no-ops.
"""

from __future__ import annotations

import logging

from bernstein.core.observability.prometheus import (
    Counter,
    Gauge,
    registry,
)

logger = logging.getLogger(__name__)


preview_active_total: Gauge = Gauge(
    "preview_active_total",
    "Number of currently active bernstein previews.",
    labelnames=["provider", "sandbox"],
    registry=registry,
)

preview_link_issued_total: Counter = Counter(
    "preview_link_issued_total",
    "Number of preview tunnel links issued, partitioned by auth mode.",
    labelnames=["auth_mode"],
    registry=registry,
)


def record_preview_started(*, provider: str, sandbox: str) -> None:
    """Increment the active-preview gauge for *(provider, sandbox)*.

    Safe to call from hot paths — never raises and absorbs prometheus
    stub no-ops cleanly.

    Args:
        provider: Tunnel provider name (``"cloudflared"``, ``"ngrok"`` …).
        sandbox: Sandbox backend name (``"worktree"``, ``"docker"`` …).
    """
    try:
        preview_active_total.labels(provider=provider or "unknown", sandbox=sandbox or "unknown").inc()
    except Exception:  # pragma: no cover - prometheus stub
        logger.debug("preview_active_total inc failed", exc_info=True)


def record_preview_stopped(*, provider: str, sandbox: str) -> None:
    """Decrement the active-preview gauge for *(provider, sandbox)*.

    Args:
        provider: Tunnel provider that hosted the preview.
        sandbox: Sandbox backend that ran the dev server.
    """
    try:
        preview_active_total.labels(provider=provider or "unknown", sandbox=sandbox or "unknown").dec()
    except Exception:  # pragma: no cover - prometheus stub
        logger.debug("preview_active_total dec failed", exc_info=True)


def record_link_issued(*, auth_mode: str) -> None:
    """Increment the link-issued counter for *auth_mode*.

    Args:
        auth_mode: One of ``"basic"``, ``"token"``, ``"none"``. Unknown
            values are forwarded as-is so a future auth backend can
            label them without code changes here.
    """
    try:
        preview_link_issued_total.labels(auth_mode=auth_mode or "unknown").inc()
    except Exception:  # pragma: no cover - prometheus stub
        logger.debug("preview_link_issued_total inc failed", exc_info=True)


__all__ = [
    "preview_active_total",
    "preview_link_issued_total",
    "record_link_issued",
    "record_preview_started",
    "record_preview_stopped",
]
