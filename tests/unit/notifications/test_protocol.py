"""Tests for the notification protocol/event dataclass."""

from __future__ import annotations

from bernstein.core.notifications.protocol import (
    NotificationDeliveryError,
    NotificationEvent,
    NotificationEventKind,
    NotificationOutcome,
    NotificationPermanentError,
    NotificationSink,
)


def test_event_kind_values_align_with_lifecycle_events() -> None:
    # The dispatcher relies on the string values matching exactly.
    expected = {"pre_task", "post_task", "pre_merge", "post_merge", "pre_spawn", "post_spawn", "synthetic"}
    assert {k.value for k in NotificationEventKind} == expected


def test_event_to_payload_round_trip() -> None:
    event = NotificationEvent(
        event_id="evt-1",
        kind=NotificationEventKind.POST_TASK,
        title="Task done",
        body="ok",
        severity="info",
        task_id="t-42",
        labels={"channel": "#ops"},
    )
    payload = event.to_payload()
    assert payload["event_id"] == "evt-1"
    assert payload["kind"] == "post_task"
    assert payload["labels"] == {"channel": "#ops"}
    # Ensure the labels dict was copied so callers can't mutate the
    # frozen event through the payload.
    payload["labels"]["channel"] = "#muted"
    assert event.labels == {"channel": "#ops"}


def test_outcome_values() -> None:
    assert NotificationOutcome.DELIVERED.value == "delivered"
    assert NotificationOutcome.DEDUPLICATED.value == "deduplicated"
    assert NotificationOutcome.FAILED_RETRYING.value == "failed_retrying"
    assert NotificationOutcome.FAILED_PERMANENT.value == "failed_permanent"


def test_permanent_error_is_subclass_of_delivery_error() -> None:
    assert issubclass(NotificationPermanentError, NotificationDeliveryError)


class _StubSink:
    sink_id = "stub"
    kind = "stub"

    async def deliver(self, event: NotificationEvent) -> None:  # pragma: no cover - protocol shape only
        return None

    async def close(self) -> None:  # pragma: no cover
        return None


def test_protocol_runtime_check_accepts_duck_typed_class() -> None:
    assert isinstance(_StubSink(), NotificationSink)
