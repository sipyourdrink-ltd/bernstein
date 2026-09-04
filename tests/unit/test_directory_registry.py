"""Tests for directory-adapter discovery through the plugin system (issue #4970).

A directory adapter is vendor-specific by definition, so it must be addable
without a change to ``bernstein.core``. Discovery therefore rides the existing
pluggy hook surface rather than a second mechanism.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from bernstein.core.security.directory_bridge import (
    DirectoryPrincipal,
    DirectoryRevocation,
)
from bernstein.core.security.directory_registry import (
    DirectoryAdapterRegistration,
    DirectoryAdapterRegistry,
    DuplicateDirectoryAdapterError,
    discover_plugin_directory_adapters,
    get_registry,
    register_directory_adapter,
    reset_registry_for_tests,
)
from bernstein.plugins.hookspecs import BernsteinSpec


class _StubDirectory:
    """Adapter an out-of-tree plugin would ship."""

    name = "acme-directory"
    version = "9.9.9"

    def __init__(self, **kwargs: Any) -> None:
        self.config = kwargs

    def resolve_principal(self, principal_ref: str) -> DirectoryPrincipal | None:
        return DirectoryPrincipal(principal_id=principal_ref)

    def list_groups(self, principal_id: str) -> tuple[str, ...]:
        del principal_id
        return ()

    def revocation(self, principal_id: str) -> DirectoryRevocation:
        return DirectoryRevocation(principal_id=principal_id)


class _FakePlugin:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def provide_directory_adapter(self) -> Any:
        return self._payload


class _FakeInner:
    def __init__(self, plugins: dict[str, Any]) -> None:
        self._plugins = plugins

    def get_plugin(self, name: str) -> Any:
        return self._plugins.get(name)


class _FakePluginManager:
    def __init__(self, plugins: dict[str, Any]) -> None:
        self._registered_names = list(plugins)
        self._inner = _FakeInner(plugins)

    @property
    def _pm(self) -> _FakeInner:
        return self._inner


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


def test_plugin_registered_adapter_is_discovered_without_touching_core() -> None:
    """An adapter reaches the registry through the plugin hook alone."""
    payload = DirectoryAdapterRegistration(
        name="acme",
        factory=_StubDirectory,
        summary="Acme corporate directory.",
    )
    added = discover_plugin_directory_adapters(_FakePluginManager({"acme-plugin": _FakePlugin(payload)}))

    assert added == 1
    entry = get_registry().get("acme")
    assert entry.source == "plugin"
    assert entry.provenance == "acme-plugin"
    adapter = get_registry().create("acme", base_url="https://directory.example.com")
    assert adapter.config == {"base_url": "https://directory.example.com"}


def test_plugin_may_register_with_a_name_factory_tuple() -> None:
    """The tuple shape the tracker hook accepts is accepted here too."""
    added = discover_plugin_directory_adapters(
        _FakePluginManager({"acme-plugin": _FakePlugin(("acme", _StubDirectory))})
    )

    assert added == 1
    assert "acme" in get_registry()


def test_plugin_opting_out_registers_nothing() -> None:
    """A plugin that returns ``None`` contributes no adapter."""
    assert discover_plugin_directory_adapters(_FakePluginManager({"acme-plugin": _FakePlugin(None)})) == 0


def test_duplicate_adapter_name_is_refused(caplog: pytest.LogCaptureFixture) -> None:
    """Two adapters cannot silently claim the same directory name."""
    register_directory_adapter("acme", _StubDirectory)
    payload = DirectoryAdapterRegistration(name="acme", factory=_StubDirectory)

    with caplog.at_level("WARNING"):
        added = discover_plugin_directory_adapters(_FakePluginManager({"other": _FakePlugin(payload)}))

    assert added == 0
    assert any("duplicate directory adapter" in record.message.lower() for record in caplog.records)

    registry = DirectoryAdapterRegistry()
    registry.register("acme", _StubDirectory)
    with pytest.raises(DuplicateDirectoryAdapterError):
        registry.register("acme", _StubDirectory)


def test_directory_adapter_hookspec_is_declared_on_the_plugin_spec() -> None:
    """Discovery rides the existing hook surface, not a private mechanism."""
    assert hasattr(BernsteinSpec, "provide_directory_adapter")
