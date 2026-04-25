"""Outbound notification subsystem (release 1.9).

Exposes the :class:`~bernstein.core.notifications.protocol.NotificationSink`
protocol, the :class:`~bernstein.core.notifications.protocol.NotificationEvent`
payload, and the in-process registry. The lifecycle bridge that wires
sinks into the v1.8.15 hook surface lives at
:mod:`bernstein.core.lifecycle.notify_bridge`.

Backwards compatibility: prior versions exposed
``NotificationManager`` / ``NotificationPayload`` / ``NotificationTarget``
under ``bernstein.core.notifications`` via the legacy redirect map.
Those names are re-exported here so the orchestrator's existing
imports keep working alongside the new sink protocol.
"""

from __future__ import annotations

from bernstein.core.communication.notifications import (
    _PD_SEVERITY,
    NotificationManager,
    NotificationPayload,
    NotificationTarget,
    format_discord,
    format_pagerduty,
    format_slack,
    format_telegram,
    format_webhook,
)
from bernstein.core.notifications.bridge import (
    DeadLetter,
    DedupCache,
    NotificationDispatcher,
    RetryPolicy,
)
from bernstein.core.notifications.config import (
    NotificationsConfig,
    RetrySchema,
    SinkSchema,
)
from bernstein.core.notifications.protocol import (
    NotificationDeliveryError,
    NotificationEvent,
    NotificationEventKind,
    NotificationOutcome,
    NotificationPermanentError,
    NotificationSink,
)
from bernstein.core.notifications.registry import (
    Registry,
    build_sink,
    default_registry,
    get_sink,
    iter_sinks,
    list_driver_kinds,
    register_driver_factory,
    register_sink,
)

__all__ = [
    # Legacy private symbol re-exported for backwards compatibility.
    "_PD_SEVERITY",
    "DeadLetter",
    "DedupCache",
    "NotificationDeliveryError",
    "NotificationDispatcher",
    "NotificationEvent",
    "NotificationEventKind",
    # Legacy names retained for backwards compatibility.
    "NotificationManager",
    "NotificationOutcome",
    "NotificationPayload",
    "NotificationPermanentError",
    "NotificationSink",
    "NotificationTarget",
    "NotificationsConfig",
    "Registry",
    "RetryPolicy",
    "RetrySchema",
    "SinkSchema",
    "build_sink",
    "default_registry",
    "format_discord",
    "format_pagerduty",
    "format_slack",
    "format_telegram",
    "format_webhook",
    "get_sink",
    "iter_sinks",
    "list_driver_kinds",
    "register_driver_factory",
    "register_sink",
]
