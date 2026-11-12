"""Check the mapping from orchestrator hook names to notification kinds."""

import pytest

from bernstein.core.lifecycle.hooks import LifecycleEvent
from bernstein.core.lifecycle.notify_bridge import lifecycle_event_to_kind
from bernstein.core.notifications.protocol import NotificationEventKind


@pytest.mark.parametrize(
    ("event", "kind"),
    [
        (LifecycleEvent.PRE_TASK, NotificationEventKind.PRE_TASK),
        (LifecycleEvent.POST_TASK, NotificationEventKind.POST_TASK),
        (LifecycleEvent.PRE_MERGE, NotificationEventKind.PRE_MERGE),
        (LifecycleEvent.POST_MERGE, NotificationEventKind.POST_MERGE),
        (LifecycleEvent.PRE_SPAWN, NotificationEventKind.PRE_SPAWN),
        (LifecycleEvent.POST_SPAWN, NotificationEventKind.POST_SPAWN),
    ],
)
def test_lifecycle_notification_mapping(event: LifecycleEvent, kind: NotificationEventKind) -> None:
    assert lifecycle_event_to_kind(event) is kind


def test_unmapped_event_does_not_get_a_default_kind() -> None:
    with pytest.raises(KeyError):
        lifecycle_event_to_kind(LifecycleEvent.PRE_ARCHIVE)
