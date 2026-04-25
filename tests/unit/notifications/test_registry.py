"""Tests for the notification driver registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.notifications import registry as notif_registry

if TYPE_CHECKING:
    from bernstein.core.notifications.protocol import NotificationEvent


@pytest.fixture(autouse=True)
def fresh_registry() -> Any:
    notif_registry._reset_for_tests()
    yield
    notif_registry._reset_for_tests()


def test_default_registry_loads_builtins() -> None:
    kinds = notif_registry.list_driver_kinds()
    assert {"telegram", "slack", "discord", "email_smtp", "webhook", "shell"} <= set(kinds)


def test_register_sink_round_trip() -> None:
    class _Stub:
        sink_id = "alpha"
        kind = "stub"

        async def deliver(self, event: NotificationEvent) -> None:
            return None

        async def close(self) -> None:
            return None

    instance = _Stub()
    notif_registry.register_sink(instance)
    assert notif_registry.get_sink("alpha") is instance


def test_duplicate_sink_id_rejected() -> None:
    class _Stub:
        sink_id = "alpha"
        kind = "stub"

        async def deliver(self, event: NotificationEvent) -> None:
            return None

        async def close(self) -> None:
            return None

    notif_registry.register_sink(_Stub())
    with pytest.raises(ValueError, match="Duplicate"):
        notif_registry.register_sink(_Stub())


def test_unknown_kind_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="Available"):
        notif_registry.default_registry().get_driver_factory("nonexistent")


def test_build_sink_uses_registry() -> None:
    captured: dict[str, Any] = {}

    class _Driver:
        sink_id = "captured"
        kind = "stubdrv"

        def __init__(self, config: dict[str, Any]) -> None:
            captured.update(config)
            self.sink_id = config["id"]

        async def deliver(self, event: NotificationEvent) -> None:  # pragma: no cover
            return None

        async def close(self) -> None:  # pragma: no cover
            return None

    notif_registry.register_driver_factory("stubdrv", _Driver)
    sink = notif_registry.build_sink({"id": "captured", "kind": "stubdrv", "extra": 1})
    assert sink.sink_id == "captured"
    assert captured == {"id": "captured", "kind": "stubdrv", "extra": 1}


def test_build_sink_requires_id() -> None:
    with pytest.raises(ValueError, match="non-empty 'id'"):
        notif_registry.build_sink({"kind": "slack"})


def test_build_sink_requires_kind() -> None:
    with pytest.raises(ValueError, match="non-empty 'kind'"):
        notif_registry.build_sink({"id": "x"})
