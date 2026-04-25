"""End-to-end test: lifecycle hook fires → notification reaches sink.

Wires :class:`HookRegistry` to :class:`NotifyLifecycleBridge` with a
minimal in-memory sink, fires a real ``post_task`` event, and asserts
the synthetic delivery landed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.lifecycle.hooks import (
    HookRegistry,
    LifecycleContext,
    LifecycleEvent,
)
from bernstein.core.lifecycle.notify_bridge import (
    NotifyLifecycleBridge,
    build_bridge_from_config,
)
from bernstein.core.notifications import registry as notif_registry
from bernstein.core.notifications.config import NotificationsConfig

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.notifications.protocol import NotificationEvent


@pytest.fixture(autouse=True)
def _reset_registry() -> Any:
    notif_registry._reset_for_tests()
    yield
    notif_registry._reset_for_tests()


class _RecordingSink:
    """Sink that records every delivery on an event-loop-safe list."""

    kind = "memory"

    def __init__(self, config: dict[str, Any]) -> None:
        self.sink_id = str(config["id"])
        self.events: list[NotificationEvent] = []
        self.delivered = threading.Event()

    async def deliver(self, event: NotificationEvent) -> None:
        self.events.append(event)
        self.delivered.set()

    async def close(self) -> None:
        return None


def test_post_task_hook_fires_notification(tmp_path: Path) -> None:
    notif_registry.default_registry().register_driver_factory("memory", _RecordingSink)

    raw = {
        "enabled": True,
        "sinks": [
            {"id": "ops", "kind": "memory", "events": ["post_task"]},
        ],
    }
    config = NotificationsConfig.from_raw(raw)
    bridge = build_bridge_from_config(
        config,
        runtime_dir=tmp_path / ".sdd" / "runtime",
        register_in_registry=False,
    )
    try:
        # Sanity: the bridge built one sink.
        assert len(bridge.sinks) == 1
        sink = bridge.sinks[0]
        assert isinstance(sink, _RecordingSink)

        registry = HookRegistry()
        bridge.attach_to_registry(registry)

        ctx = LifecycleContext(
            event=LifecycleEvent.POST_TASK,
            task="t-99",
            session_id="s-1",
            workdir=tmp_path,
            timestamp=time.time(),
        )
        registry.run(LifecycleEvent.POST_TASK, ctx)

        # Delivery is async; wait for the bridge's loop to flush it.
        assert sink.delivered.wait(timeout=5.0), "delivery did not complete"
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.kind.value == "post_task"
        assert event.task_id == "t-99"
        assert event.session_id == "s-1"
        assert event.title.startswith("Task finished")
    finally:
        asyncio.run(bridge.aclose())


def test_event_filter_skips_non_subscribed_kinds(tmp_path: Path) -> None:
    notif_registry.default_registry().register_driver_factory("memory", _RecordingSink)

    raw = {
        "enabled": True,
        "sinks": [
            # Only subscribed to post_merge.
            {"id": "merges", "kind": "memory", "events": ["post_merge"]},
        ],
    }
    config = NotificationsConfig.from_raw(raw)
    bridge = build_bridge_from_config(
        config,
        runtime_dir=tmp_path / ".sdd" / "runtime",
        register_in_registry=False,
    )
    try:
        sink = bridge.sinks[0]
        assert isinstance(sink, _RecordingSink)

        registry = HookRegistry()
        bridge.attach_to_registry(registry)

        registry.run(
            LifecycleEvent.POST_TASK,
            LifecycleContext(event=LifecycleEvent.POST_TASK, task="t-1"),
        )
        # Give the loop a moment in case it tried to deliver.
        time.sleep(0.5)
        assert sink.events == []
    finally:
        asyncio.run(bridge.aclose())


def test_dead_letter_file_created_on_construction(tmp_path: Path) -> None:
    """The verification block in the ticket runs `test -f .sdd/runtime/notifications/dead_letter.jsonl`.

    Building the dispatcher must touch the file so that ``test -f``
    succeeds even before the first failed delivery.
    """
    runtime = tmp_path / ".sdd" / "runtime"
    config = NotificationsConfig.from_raw({"enabled": True, "sinks": []})
    bridge = build_bridge_from_config(
        config,
        runtime_dir=runtime,
        register_in_registry=False,
    )
    try:
        assert (runtime / "notifications" / "dead_letter.jsonl").exists()
    finally:
        asyncio.run(bridge.aclose())


def test_attach_to_registry_subscribes_default_events(tmp_path: Path) -> None:
    notif_registry.default_registry().register_driver_factory("memory", _RecordingSink)
    config = NotificationsConfig.from_raw(
        {"enabled": True, "sinks": [{"id": "all", "kind": "memory"}]},
    )
    bridge = build_bridge_from_config(
        config,
        runtime_dir=tmp_path / ".sdd" / "runtime",
        register_in_registry=False,
    )
    try:
        registry = HookRegistry()
        bridge.attach_to_registry(registry)
        for event in NotifyLifecycleBridge.DEFAULT_EVENTS:
            assert any("NotifyLifecycleBridge" in label for label in registry.registered(event)), (
                f"missing subscription for {event.value}"
            )
    finally:
        asyncio.run(bridge.aclose())
