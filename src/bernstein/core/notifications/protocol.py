"""NotificationSink protocol and event payload (release 1.9, op-005 follow-up).

Bernstein's chat-control bridges (Telegram, Slack, Discord) are inbound
channels: a human attaches to a TTY and drives a run interactively. There
is no symmetric outbound channel for an unattended run to push events
("task failed", "merge landed", "budget exceeded") back to the human.

This module defines the shape every notification driver implements. The
design mirrors :class:`~bernstein.core.sandbox.backend.SandboxBackend`:

  * a runtime-checkable :class:`Protocol` so third-party packages can
    register a driver without subclassing,
  * a frozen :class:`NotificationEvent` dataclass that carries everything
    a driver could conceivably want, and
  * a small enum of standard event kinds aligned with the v1.8.15
    lifecycle hooks (``pre_task``, ``post_task``, ``pre_merge``,
    ``post_merge``, ``pre_spawn``).

Drivers are registered via the ``bernstein.notification_sinks``
entry-point group. See :mod:`bernstein.core.notifications.registry` for
the lookup surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "NotificationDeliveryError",
    "NotificationEvent",
    "NotificationEventKind",
    "NotificationOutcome",
    "NotificationPermanentError",
    "NotificationSink",
]


class NotificationEventKind(StrEnum):
    """Canonical event kinds aligned with lifecycle hook names.

    Matches the values of
    :class:`bernstein.core.lifecycle.hooks.LifecycleEvent` plus a
    ``synthetic`` value used by ``bernstein notify test``.
    """

    PRE_TASK = "pre_task"
    POST_TASK = "post_task"
    PRE_MERGE = "pre_merge"
    POST_MERGE = "post_merge"
    PRE_SPAWN = "pre_spawn"
    POST_SPAWN = "post_spawn"
    SYNTHETIC = "synthetic"


class NotificationOutcome(StrEnum):
    """Terminal delivery outcome recorded in the audit chain.

    Values:
        DELIVERED: The driver returned without raising.
        DEDUPLICATED: The bridge suppressed delivery because the
            ``event_id`` matched a recent deduplication window entry.
        FAILED_RETRYING: The driver raised a transient error; the
            bridge will retry.
        FAILED_PERMANENT: The driver raised a permanent error or all
            retries were exhausted; the event was forwarded to the
            dead-letter file.
    """

    DELIVERED = "delivered"
    DEDUPLICATED = "deduplicated"
    FAILED_RETRYING = "failed_retrying"
    FAILED_PERMANENT = "failed_permanent"


class NotificationDeliveryError(RuntimeError):
    """Generic transient delivery failure.

    Drivers raise this when the underlying transport hit a recoverable
    error (network blip, rate limit, 5xx). The bridge will retry with
    exponential backoff up to the configured ``max_attempts``.
    """


class NotificationPermanentError(NotificationDeliveryError):
    """Non-retryable delivery failure.

    Drivers raise this when retrying cannot succeed (4xx with stable
    body, malformed config, missing dependency). The bridge skips
    backoff and routes the event straight to the dead-letter file.
    """


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """Immutable payload handed to every :class:`NotificationSink`.

    Attributes:
        event_id: Stable identifier used for dedup and audit. Callers
            SHOULD derive this deterministically from the underlying
            event so a restart-loop cannot spam a sink. The bridge
            falls back to a ``uuid4`` if missing — that path is
            intentionally non-deduplicated.
        kind: One of :class:`NotificationEventKind`.
        title: Short single-line headline (e.g. "Task t-42 failed").
        body: Multi-line description; drivers free to truncate.
        severity: One of ``"info"``, ``"warning"``, ``"error"``.
            Drivers map this to colours, icons, etc.
        task_id: Bernstein task id when the event is task-scoped.
        session_id: Agent session id when relevant.
        run_id: Run identifier (``$BERNSTEIN_RUN_ID``) so a sink can
            group bursts that belong to the same run.
        timestamp: Unix wall-clock time the event was minted.
        labels: Free-form key/value tags. Drivers use these as
            channel routing hints, e.g. ``{"channel": "#ops-alerts"}``.
        details: Arbitrary structured payload; drivers MAY include
            this verbatim or render it.
    """

    event_id: str
    kind: NotificationEventKind
    title: str
    body: str = ""
    severity: str = "info"
    task_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    timestamp: float = 0.0
    labels: dict[str, str] = field(default_factory=dict[str, str])
    details: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_payload(self) -> dict[str, Any]:
        """Serialise the event for transport / audit logging."""
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "title": self.title,
            "body": self.body,
            "severity": self.severity,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "labels": dict(self.labels),
            "details": dict(self.details),
        }


@runtime_checkable
class NotificationSink(Protocol):
    """Protocol every notification driver implements.

    Drivers MUST be idempotent under retry: the bridge may call
    :meth:`deliver` repeatedly for the same event after a transient
    failure. Drivers SHOULD raise :class:`NotificationPermanentError`
    when retrying is futile so the bridge can short-circuit to the
    dead-letter file instead of looping.

    Attributes:
        sink_id: Stable identifier referenced from
            ``bernstein.yaml::notifications.sinks[*].id``. Must equal
            the ``id`` value the user wrote so audit lines can be
            cross-referenced.
        kind: Driver kind (e.g. ``"slack"``, ``"telegram"``). Stable
            across versions.
    """

    sink_id: str
    kind: str

    async def deliver(self, event: NotificationEvent) -> None:
        """Deliver ``event`` over the underlying transport.

        Args:
            event: The notification to publish.

        Raises:
            NotificationPermanentError: When retrying cannot succeed.
            NotificationDeliveryError: For transient failures the
                bridge should retry.
        """
        ...

    async def close(self) -> None:
        """Release transport resources. Idempotent."""
        ...
