"""Secret-store backends are plugins, not core patches (issue #4984).

Adding a store must not require a change to ``bernstein.core``: a store is
registered through the same registry-plus-hook idiom the tracker adapters
already use, and the broker resolves it by name from configuration.
"""

from __future__ import annotations

import pytest

from bernstein.core.security import secret_store_registry as reg
from bernstein.core.security.external_secret_store import (
    ExternalCredential,
    ExternalSecretStore,
    SecretDescriptor,
)
from bernstein.core.security.secrets_broker import SecretsBrokerError, build_broker_from_config


class _PluginStore(ExternalSecretStore):
    store_id = "out-of-core"

    def resolve(self, path: str) -> SecretDescriptor:
        return SecretDescriptor(store_id=self.store_id, upstream_id=path)

    def mint_credential(self, path: str, *, audience: str, ttl_seconds: int) -> ExternalCredential:
        return ExternalCredential(value=f"cred-for-{path}", upstream_id=path, audience=audience)

    def report_revocation(self, path: str, *, upstream_id: str) -> bool:
        return False


class _FakePlugin:
    def provide_secret_store(self):
        return ("out-of-core", _PluginStore)


class _FakeInnerPm:
    def __init__(self, plugins):
        self._plugins = plugins

    def get_plugin(self, name):
        return self._plugins.get(name)


class _FakePm:
    def __init__(self, plugins):
        self._registered_names = list(plugins)
        self._pm = _FakeInnerPm(plugins)


@pytest.fixture(autouse=True)
def _clean_registry():
    reg.reset_registry_for_tests()
    yield
    reg.reset_registry_for_tests()


class TestRegistry:
    def test_plugin_registered_store_is_discovered_without_a_core_change(self) -> None:
        """13. A store contributed by a plugin becomes resolvable by name."""
        added = reg.discover_plugin_secret_stores(_FakePm({"acme-plugin": _FakePlugin()}))
        assert added == 1
        registration = reg.get_registry().get("out-of-core")
        assert registration.source == "plugin"
        assert registration.provenance == "acme-plugin"
        assert isinstance(registration.factory(), _PluginStore)

    def test_duplicate_store_name_is_refused(self) -> None:
        """14. Two stores cannot silently claim the same name."""
        reg.register_secret_store("dup", _PluginStore)
        with pytest.raises(reg.DuplicateSecretStoreError):
            reg.register_secret_store("dup", _PluginStore)

    def test_unknown_store_name_names_what_is_registered(self) -> None:
        """15. A misconfigured store name fails closed with a usable message."""
        with pytest.raises(KeyError, match="nope"):
            reg.get_registry().get("nope")


class TestBrokerConfigResolvesStoreByName:
    def test_broker_config_selects_a_registered_store(self) -> None:
        """16. ``backend: external`` plus a store name builds a working broker."""
        reg.register_secret_store("out-of-core", _PluginStore)
        broker = build_broker_from_config({"backend": "external", "backend_settings": {"store_name": "out-of-core"}})
        assert broker.backend_name == "external"
        token = broker.mint(secret_name="out-of-core:db/password", task_id="t-1")
        assert broker.resolve(token.value) == "cred-for-db/password"

    def test_unregistered_store_name_fails_at_mint(self) -> None:
        """17. A store that was never registered refuses rather than minting."""
        broker = build_broker_from_config({"backend": "external", "backend_settings": {"store_name": "absent"}})
        with pytest.raises(SecretsBrokerError, match="absent"):
            broker.mint(secret_name="absent:db/password", task_id="t-1")
