"""Unit tests for the custom-vault-token-store example plugin.

These tests substitute a fake :class:`VaultTransport` so they need no
network access. ``test_live_vault.py`` in this directory exercises the
same store against a real Vault dev server and is skipped by default.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from custom_vault_token_store import VaultHttpError, VaultTokenRoleStore, VaultTokenStorePlugin

from bernstein.core.security.external_secret_store import ExternalStoreError


class _FakeTransport:
    """Records calls and returns scripted responses keyed by (method, path)."""

    def __init__(self, responses: dict[tuple[str, str], dict[str, Any] | None]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
        self.calls.append((method, path, body))
        key = (method, path)
        if key not in self._responses:
            raise ExternalStoreError(f"unscripted call: {method} {path}")
        return self._responses[key]


class TestResolve:
    def test_existing_role_resolves(self) -> None:
        transport = _FakeTransport(
            {
                ("GET", "auth/token/roles/bernstein-agent"): {
                    "data": {"name": "bernstein-agent", "token_explicit_max_ttl": 600},
                },
            }
        )
        store = VaultTokenRoleStore(transport=transport)
        descriptor = store.resolve("bernstein-agent")
        assert descriptor.store_id == "vault"
        assert descriptor.upstream_id == "bernstein-agent"
        assert descriptor.revoked is False
        assert descriptor.expires_at > time.time()

    def test_missing_role_raises(self) -> None:
        """A real 404 from Vault takes the not-found branch, not a bare re-raise.

        The message is the branch's own literal ("vault token role ... not
        found"), not the transport's raw HTTP-404 text, so a mutation that
        breaks the ``exc.status == 404`` check (making this fall through to
        the bare ``raise``) fails this assertion instead of passing it by
        coincidence.
        """

        def _raise(_method: str, _path: str, _body: dict[str, Any] | None = None) -> dict[str, Any] | None:
            raise VaultHttpError("vault GET auth/token/roles/bernstein-agent -> HTTP 404: role not found", status=404)

        store = VaultTokenRoleStore(transport=_raise)
        with pytest.raises(ExternalStoreError, match=r"vault token role 'bernstein-agent' not found"):
            store.resolve("bernstein-agent")

    def test_non_404_role_error_reraises_unwrapped(self) -> None:
        """A 403/5xx on resolve() must not be flattened into the not-found message."""

        def _raise(_method: str, _path: str, _body: dict[str, Any] | None = None) -> dict[str, Any] | None:
            raise VaultHttpError("vault GET auth/token/roles/bernstein-agent -> HTTP 500: internal error", status=500)

        store = VaultTokenRoleStore(transport=_raise)
        with pytest.raises(VaultHttpError, match="HTTP 500"):
            store.resolve("bernstein-agent")


class TestRoleNameValidation:
    """A role name goes straight into the Vault request path unescaped.

    ``vault:../../sys/policy``, or a role containing ``?``/``#``, changes
    which Vault endpoint the request hits and carries the operator's
    token, so every entry point that builds a path from it must reject
    it before any transport call, not just the ones a happy-path test
    would exercise.
    """

    @pytest.mark.parametrize(
        "path",
        ["../sys/policy", "a/../b", "role?extra=1", "role#frag", "role/with/slash", ".."],
    )
    def test_resolve_rejects_traversal_shaped_path(self, path: str) -> None:
        def _unreachable(_method: str, _path: str, _body: dict[str, Any] | None = None) -> dict[str, Any] | None:
            raise AssertionError("transport must not be called for an invalid role name")

        store = VaultTokenRoleStore(transport=_unreachable)
        with pytest.raises(ExternalStoreError, match="invalid vault role name"):
            store.resolve(path)

    def test_mint_credential_rejects_traversal_shaped_path(self) -> None:
        def _unreachable(_method: str, _path: str, _body: dict[str, Any] | None = None) -> dict[str, Any] | None:
            raise AssertionError("transport must not be called for an invalid role name")

        store = VaultTokenRoleStore(transport=_unreachable)
        with pytest.raises(ExternalStoreError, match="invalid vault role name"):
            store.mint_credential("../sys/policy", audience="agent", ttl_seconds=60)

    def test_report_revocation_rejects_traversal_shaped_path(self) -> None:
        def _unreachable(_method: str, _path: str, _body: dict[str, Any] | None = None) -> dict[str, Any] | None:
            raise AssertionError("transport must not be called for an invalid role name")

        store = VaultTokenRoleStore(transport=_unreachable)
        with pytest.raises(ExternalStoreError, match="invalid vault role name"):
            store.report_revocation("../sys/policy", upstream_id="../sys/policy")

    def test_ordinary_role_name_is_accepted(self) -> None:
        transport = _FakeTransport(
            {
                ("GET", "auth/token/roles/bernstein-agent.v2"): {
                    "data": {"name": "bernstein-agent.v2", "token_explicit_max_ttl": 600},
                },
            }
        )
        store = VaultTokenRoleStore(transport=transport)
        descriptor = store.resolve("bernstein-agent.v2")
        assert descriptor.upstream_id == "bernstein-agent.v2"


class TestMintCredential:
    def test_mint_returns_client_token_capped_to_requested_ttl(self) -> None:
        transport = _FakeTransport(
            {
                ("POST", "auth/token/create/bernstein-agent"): {
                    "auth": {
                        "client_token": "s.abc123",
                        "accessor": "acc-1",
                        "lease_duration": 3600,
                    },
                },
            }
        )
        store = VaultTokenRoleStore(transport=transport)
        before = time.time()
        credential = store.mint_credential("bernstein-agent", audience="qa-task-1", ttl_seconds=60)
        assert credential.value == "s.abc123"
        assert credential.upstream_id == "acc-1"
        assert credential.audience == "qa-task-1"
        # lease_duration (3600) is wider than the requested ttl (60); the
        # store must cap to the narrower of the two, never widen it.
        assert before + 60 <= credential.expires_at <= before + 61

    def test_mint_caps_to_vault_lease_when_shorter_than_request(self) -> None:
        transport = _FakeTransport(
            {
                ("POST", "auth/token/create/bernstein-agent"): {
                    "auth": {"client_token": "s.abc123", "accessor": "acc-1", "lease_duration": 30},
                },
            }
        )
        store = VaultTokenRoleStore(transport=transport)
        before = time.time()
        credential = store.mint_credential("bernstein-agent", audience="", ttl_seconds=600)
        assert before + 30 <= credential.expires_at <= before + 31

    def test_mint_caps_to_requested_ttl_when_vault_reports_no_lease(self) -> None:
        transport = _FakeTransport(
            {
                ("POST", "auth/token/create/bernstein-agent"): {
                    "auth": {"client_token": "s.abc123", "accessor": "acc-1"},
                },
            }
        )
        store = VaultTokenRoleStore(transport=transport)
        before = time.time()
        credential = store.mint_credential("bernstein-agent", audience="", ttl_seconds=60)
        assert before + 60 <= credential.expires_at <= before + 61

    def test_mint_raises_when_vault_omits_accessor(self) -> None:
        transport = _FakeTransport(
            {
                ("POST", "auth/token/create/bernstein-agent"): {
                    "auth": {"client_token": "s.abc123", "lease_duration": 60},
                },
            }
        )
        store = VaultTokenRoleStore(transport=transport)
        with pytest.raises(ExternalStoreError, match="no accessor"):
            store.mint_credential("bernstein-agent", audience="", ttl_seconds=60)

    def test_mint_sanitizes_display_name(self) -> None:
        transport = _FakeTransport(
            {
                ("POST", "auth/token/create/bernstein-agent"): {
                    "auth": {"client_token": "s.abc123", "accessor": "acc-1", "lease_duration": 60},
                },
            }
        )
        store = VaultTokenRoleStore(transport=transport)
        store.mint_credential("bernstein-agent", audience="task/qa:1#weird chars", ttl_seconds=60)
        _, _, body = transport.calls[-1]
        assert body is not None
        assert body["display_name"] == "task-qa-1-weird-chars"

    def test_missing_auth_block_raises(self) -> None:
        transport = _FakeTransport({("POST", "auth/token/create/bernstein-agent"): {}})
        store = VaultTokenRoleStore(transport=transport)
        with pytest.raises(ExternalStoreError, match="no auth block"):
            store.mint_credential("bernstein-agent", audience="", ttl_seconds=60)


class TestReportRevocation:
    def test_live_accessor_with_positive_ttl_is_not_revoked(self) -> None:
        transport = _FakeTransport(
            {
                ("POST", "auth/token/lookup-accessor"): {"data": {"ttl": 55}},
            }
        )
        store = VaultTokenRoleStore(transport=transport)
        assert store.report_revocation("bernstein-agent", upstream_id="acc-1") is False

    def test_expired_ttl_is_revoked(self) -> None:
        transport = _FakeTransport(
            {
                ("POST", "auth/token/lookup-accessor"): {"data": {"ttl": 0}},
            }
        )
        store = VaultTokenRoleStore(transport=transport)
        assert store.report_revocation("bernstein-agent", upstream_id="acc-1") is True

    def test_invalid_accessor_is_revoked(self) -> None:
        """Vault answers 400 invalid accessor for an accessor it no longer knows."""

        def _raise(_method: str, _path: str, _body: dict[str, Any] | None = None) -> dict[str, Any] | None:
            raise VaultHttpError("vault POST auth/token/lookup-accessor -> HTTP 400: invalid accessor", status=400)

        store = VaultTokenRoleStore(transport=_raise)
        assert store.report_revocation("bernstein-agent", upstream_id="acc-gone") is True

    def test_forbidden_accessor_lookup_is_not_revoked(self) -> None:
        """A 403 on the broker's own token is a caller failure, not revocation."""

        def _raise(_method: str, _path: str, _body: dict[str, Any] | None = None) -> dict[str, Any] | None:
            raise VaultHttpError("vault POST auth/token/lookup-accessor -> HTTP 403: permission denied", status=403)

        store = VaultTokenRoleStore(transport=_raise)
        with pytest.raises(ExternalStoreError):
            store.report_revocation("bernstein-agent", upstream_id="acc-gone")

    def test_no_upstream_id_fails_closed(self) -> None:
        store = VaultTokenRoleStore(transport=_FakeTransport({}))
        assert store.report_revocation("bernstein-agent", upstream_id="") is True

    def test_role_gone_is_revoked(self) -> None:
        """upstream_id == path takes the role-check branch (_role_revoked), not accessor lookup."""

        def _raise(_method: str, _path: str, _body: dict[str, Any] | None = None) -> dict[str, Any] | None:
            raise VaultHttpError("vault GET auth/token/roles/bernstein-agent -> HTTP 404: role not found", status=404)

        store = VaultTokenRoleStore(transport=_raise)
        assert store.report_revocation("bernstein-agent", upstream_id="bernstein-agent") is True

    def test_role_lookup_failure_reraises(self) -> None:
        """A 403/5xx on the role-check branch is a caller/transport failure, not revocation."""

        def _raise(_method: str, _path: str, _body: dict[str, Any] | None = None) -> dict[str, Any] | None:
            raise VaultHttpError("vault GET auth/token/roles/bernstein-agent -> HTTP 500: internal error", status=500)

        store = VaultTokenRoleStore(transport=_raise)
        with pytest.raises(VaultHttpError):
            store.report_revocation("bernstein-agent", upstream_id="bernstein-agent")


class TestPluginRegistration:
    def test_opts_out_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_VAULT_ADDR", raising=False)
        monkeypatch.delenv("BERNSTEIN_VAULT_TOKEN", raising=False)
        assert VaultTokenStorePlugin().provide_secret_store() is None

    def test_registers_when_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_VAULT_ADDR", "http://127.0.0.1:8200")
        monkeypatch.setenv("BERNSTEIN_VAULT_TOKEN", "roottoken")
        registration = VaultTokenStorePlugin().provide_secret_store()
        assert registration is not None
        assert registration.name == "vault"
        store = registration.factory()
        assert isinstance(store, VaultTokenRoleStore)

    def test_plugin_class_has_stable_name(self) -> None:
        assert VaultTokenStorePlugin.plugin_name == "custom-vault-token-store"
        assert VaultTokenStorePlugin.hook_target == "provide_secret_store"

    def test_rejects_non_loopback_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_VAULT_ADDR", "http://vault.internal.example.com:8200")
        monkeypatch.setenv("BERNSTEIN_VAULT_TOKEN", "roottoken")
        with pytest.raises(ValueError, match="cleartext"):
            VaultTokenStorePlugin().provide_secret_store()

    def test_allows_https_to_a_remote_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_VAULT_ADDR", "https://vault.internal.example.com:8200")
        monkeypatch.setenv("BERNSTEIN_VAULT_TOKEN", "roottoken")
        registration = VaultTokenStorePlugin().provide_secret_store()
        assert registration is not None


class _BrokerFakeTransport:
    """Scripted transport that records every call, for the broker path."""

    def __init__(self, responses: dict[tuple[str, str], dict[str, Any] | None]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
        self.calls.append((method, path, body))
        key = (method, path)
        if key not in self._responses:
            raise VaultHttpError(f"unscripted call: {method} {path}", status=404)
        return self._responses[key]


class TestBrokerMint:
    def test_mint_checks_role_not_accessor(self) -> None:
        from bernstein.core.security.secrets_broker import (
            BrokerConfig,
            ExternalStoreBackend,
            SecretsBroker,
        )

        transport = _BrokerFakeTransport(
            {
                ("GET", "auth/token/roles/bernstein-agent"): {
                    "data": {"name": "bernstein-agent", "token_explicit_max_ttl": 600},
                },
                ("POST", "auth/token/create/bernstein-agent"): {
                    "auth": {"client_token": "s.abc123", "accessor": "acc-1", "lease_duration": 60},
                },
            }
        )
        store = VaultTokenRoleStore(transport=transport)
        backend = ExternalStoreBackend(store=store, store_name="vault")
        broker = SecretsBroker(backend=backend, config=BrokerConfig(backend="external"))

        token = broker.mint(secret_name="vault:bernstein-agent", task_id="task-1", ttl_seconds=60)

        assert token.secret_name == "vault:bernstein-agent"
        assert ("GET", "auth/token/roles/bernstein-agent") in [(m, p) for m, p, _ in transport.calls]
        assert not any(p == "auth/token/lookup-accessor" for _, p, _ in transport.calls)
