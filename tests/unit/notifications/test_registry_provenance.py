"""Notification registration conflicts identify both registering modules."""

from __future__ import annotations

import importlib.util
import sys
from importlib.metadata import EntryPoint
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from bernstein.core.notifications import registry as notifications

if TYPE_CHECKING:
    from bernstein.core.notifications.protocol import NotificationEvent


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> notifications.Registry:
    registry = notifications.Registry()
    monkeypatch.setattr(notifications, "_default_registry_instance", registry)
    return registry


def _import_registrant(tmp_path: Path, name: str, source: str, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


_DRIVER = """
from bernstein.core.notifications.registry import register_driver_factory
class Factory:
    pass
register_driver_factory('conflicting-driver', Factory)
"""

_SINK = """
from bernstein.core.notifications.registry import register_sink
class Sink:
    sink_id = 'conflicting-sink'
    kind = 'fixture'
    async def deliver(self, event):
        pass
    async def close(self):
        pass
instance = Sink()
register_sink(instance)
"""


def test_duplicate_notification_driver_names_both_registration_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: notifications.Registry
) -> None:
    first = _import_registrant(tmp_path, "driver_first", _DRIVER, monkeypatch)

    with pytest.raises(ValueError) as exc_info:
        _import_registrant(tmp_path, "driver_second", _DRIVER, monkeypatch)

    assert "driver_first" in str(exc_info.value)
    assert "driver_second" in str(exc_info.value)
    assert registry.get_driver_factory("conflicting-driver") is first.Factory


def test_duplicate_notification_sink_names_both_registration_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: notifications.Registry
) -> None:
    first = _import_registrant(tmp_path, "sink_first", _SINK, monkeypatch)

    with pytest.raises(ValueError) as exc_info:
        _import_registrant(tmp_path, "sink_second", _SINK, monkeypatch)

    assert "sink_first" in str(exc_info.value)
    assert "sink_second" in str(exc_info.value)
    assert registry.get_sink("conflicting-sink") is first.instance


class _Sink:
    sink_id = "shared"
    kind = "fixture"

    async def deliver(self, event: NotificationEvent) -> None:
        pass

    async def close(self) -> None:
        pass


def test_notification_driver_and_sink_namespaces_are_independent(registry: notifications.Registry) -> None:
    registry.register_driver_factory("shared", _Sink)
    sink = _Sink()
    registry.register_sink(sink)

    assert registry.get_driver_factory("shared") is _Sink
    assert registry.get_sink("shared") is sink


def test_notification_sink_can_be_registered_after_unregister(registry: notifications.Registry) -> None:
    first = _Sink()
    registry.register_sink(first)
    registry.unregister_sink(first.sink_id)
    replacement = _Sink()
    registry.register_sink(replacement)

    assert registry.get_sink("shared") is replacement


def test_builtin_notification_driver_keeps_registration_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: notifications.Registry
) -> None:
    original = registry.get_driver_factory("slack")

    with pytest.raises(ValueError) as exc_info:
        _import_registrant(tmp_path, "builtin_replacement", _DRIVER.replace("conflicting-driver", "slack"), monkeypatch)

    assert "bernstein.core.notifications.sinks.slack" in str(exc_info.value)
    assert "builtin_replacement" in str(exc_info.value)
    assert registry.get_driver_factory("slack") is original


def test_plugin_notification_driver_keeps_registration_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: notifications.Registry
) -> None:
    plugin = _import_registrant(tmp_path, "notification_plugin", "class Factory:\n    pass\n", monkeypatch)
    entry_point = EntryPoint(
        name="conflicting-driver", value="notification_plugin:Factory", group=notifications.ENTRY_POINT_GROUP
    )
    monkeypatch.setattr(notifications, "entry_points", lambda **_kwargs: (entry_point,))
    original = registry.get_driver_factory("conflicting-driver")
    assert original is plugin.Factory

    with pytest.raises(ValueError) as exc_info:
        _import_registrant(tmp_path, "plugin_replacement", _DRIVER, monkeypatch)

    assert "notification_plugin" in str(exc_info.value)
    assert "plugin_replacement" in str(exc_info.value)
    assert registry.get_driver_factory("conflicting-driver") is original


def test_programmatic_notification_driver_survives_lazy_loading(registry: notifications.Registry) -> None:
    registry.register_driver_factory("slack", _Sink)

    assert registry.get_driver_factory("slack") is _Sink
    assert "telegram" in registry.list_driver_kinds()


def test_shadowed_notification_plugin_keeps_first_registration(
    monkeypatch: pytest.MonkeyPatch, registry: notifications.Registry
) -> None:
    registry.register_driver_factory("shared", _Sink)
    entry_point = EntryPoint(name="shared", value="unimportable_plugin:Factory", group=notifications.ENTRY_POINT_GROUP)
    monkeypatch.setattr(notifications, "entry_points", lambda **_kwargs: (entry_point,))

    assert registry.get_driver_factory("shared") is _Sink
