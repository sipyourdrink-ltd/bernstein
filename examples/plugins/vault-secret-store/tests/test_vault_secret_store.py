"""Unit tests for the Vault secret-store example plugin."""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from vault_secret_store import VaultConfig, VaultSecretStore, VaultSecretStorePlugin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    def __init__(self, body: dict[str, Any], status: int = 200) -> None:
        self._data = json.dumps(body).encode()
        self.status = status

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *_: object) -> None:
        pass


def _patched_urlopen(response_body: dict[str, Any], status: int = 200):
    return patch(
        "vault_secret_store.urlopen",
        return_value=_FakeHTTPResponse(response_body, status),
    )


# ---------------------------------------------------------------------------
# VaultConfig
# ---------------------------------------------------------------------------


class TestVaultConfig:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in ("BERNSTEIN_VAULT_ADDR", "BERNSTEIN_VAULT_TOKEN", "BERNSTEIN_VAULT_ROLE", "BERNSTEIN_VAULT_MOUNT"):
            monkeypatch.delenv(k, raising=False)
        cfg = VaultConfig.from_env()
        assert cfg.addr == "http://127.0.0.1:8200"
        assert cfg.token == ""
        assert cfg.role == "bernstein-agent"
        assert cfg.mount == "auth/token"

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_VAULT_ADDR", "https://vault.prod:8200/")
        monkeypatch.setenv("BERNSTEIN_VAULT_TOKEN", "s.root")
        monkeypatch.setenv("BERNSTEIN_VAULT_ROLE", "ci-agent")
        monkeypatch.setenv("BERNSTEIN_VAULT_MOUNT", "auth/token")
        cfg = VaultConfig.from_env()
        assert cfg.addr == "https://vault.prod:8200"  # trailing slash stripped
        assert cfg.token == "s.root"
        assert cfg.role == "ci-agent"


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


class TestResolve:
    def test_resolve_returns_descriptor(self) -> None:
        store = VaultSecretStore(VaultConfig(token="root", role="bernstein-agent"))
        role_data = {"data": {"explicit_max_ttl": 600}}
        with _patched_urlopen(role_data):
            desc = store.resolve("bernstein-agent")
        assert desc.store_id == "vault"
        assert desc.upstream_id == "bernstein-agent"
        assert desc.revoked is False
        assert desc.expires_at > 0

    def test_resolve_path_uses_last_segment(self) -> None:
        store = VaultSecretStore(VaultConfig(token="root"))
        role_data = {"data": {"explicit_max_ttl": 0}}
        with _patched_urlopen(role_data):
            desc = store.resolve("some/nested/role-name")
        assert desc.upstream_id == "role-name"
        assert desc.expires_at == 0.0

    def test_resolve_raises_on_http_error(self) -> None:
        from urllib.error import HTTPError

        store = VaultSecretStore(VaultConfig(token="root"))
        with patch("vault_secret_store.urlopen", side_effect=HTTPError("url", 403, "Forbidden", {}, None)):
            from bernstein.core.security.external_secret_store import ExternalStoreError

            with pytest.raises(ExternalStoreError, match="not found"):
                store.resolve("missing-role")


# ---------------------------------------------------------------------------
# mint_credential()
# ---------------------------------------------------------------------------


class TestMintCredential:
    def test_mint_returns_credential(self) -> None:
        store = VaultSecretStore(VaultConfig(token="root", role="bernstein-agent"))
        vault_response = {
            "auth": {
                "client_token": "s.abc123",
                "accessor": "acc-xyz",
                "lease_duration": 300,
            }
        }
        with _patched_urlopen(vault_response):
            cred = store.mint_credential("bernstein-agent", audience="task-t1", ttl_seconds=300)
        assert cred.value == "s.abc123"
        assert cred.upstream_id == "acc-xyz"
        assert cred.audience == "task-t1"
        assert cred.expires_at > 0

    def test_mint_raises_when_no_token_in_response(self) -> None:
        store = VaultSecretStore(VaultConfig(token="root"))
        with _patched_urlopen({"auth": {}}):
            from bernstein.core.security.external_secret_store import ExternalStoreError

            with pytest.raises(ExternalStoreError, match="no client_token"):
                store.mint_credential("some-role", audience="x", ttl_seconds=60)

    def test_credential_repr_redacts_value(self) -> None:
        store = VaultSecretStore(VaultConfig(token="root"))
        vault_response = {"auth": {"client_token": "s.secret", "accessor": "acc", "lease_duration": 60}}
        with _patched_urlopen(vault_response):
            cred = store.mint_credential("role", audience="a", ttl_seconds=60)
        assert "s.secret" not in repr(cred)
        assert "redacted" in repr(cred)


# ---------------------------------------------------------------------------
# report_revocation()
# ---------------------------------------------------------------------------


class TestReportRevocation:
    def test_empty_accessor_is_not_revoked(self) -> None:
        store = VaultSecretStore(VaultConfig(token="root"))
        assert store.report_revocation("") is False

    def test_404_on_lookup_means_revoked(self) -> None:
        from urllib.error import HTTPError

        store = VaultSecretStore(VaultConfig(token="root"))
        with patch("vault_secret_store.urlopen", side_effect=HTTPError("url", 404, "Not Found", {}, None)):
            assert store.report_revocation("acc-gone") is True

    def test_live_accessor_is_not_revoked(self) -> None:
        store = VaultSecretStore(VaultConfig(token="root"))
        data = {"data": {"expire_time": "2099-01-01T00:00:00Z"}}
        with _patched_urlopen(data):
            assert store.report_revocation("acc-live") is False


# ---------------------------------------------------------------------------
# Plugin hookimpl
# ---------------------------------------------------------------------------


class TestPlugin:
    def test_provide_secret_store_returns_vault_factory(self) -> None:
        plugin = VaultSecretStorePlugin()
        name, factory = plugin.provide_secret_store()
        assert name == "vault"
        store = factory()
        assert isinstance(store, VaultSecretStore)
        assert store.store_id == "vault"
