"""Tests for src/bernstein/core/vault_injector.py — JIT credential injection."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.vault_injector import (
    CredentialLease,
    InjectionConfig,
    VaultInjectionError,
    VaultInjector,
    _apply_env_map,
    inject_agent_credentials,
    revoke_agent_credentials,
)


# ---------------------------------------------------------------------------
# _apply_env_map
# ---------------------------------------------------------------------------


class TestApplyEnvMap:
    def test_empty_map_returns_raw(self) -> None:
        raw = {"username": "alice", "password": "s3cr3t"}
        result = _apply_env_map(raw, {})
        assert result == raw

    def test_renames_fields(self) -> None:
        raw = {"username": "alice", "password": "s3cr3t"}
        env_map = {"username": "DB_USER", "password": "DB_PASS"}
        result = _apply_env_map(raw, env_map)
        assert result == {"DB_USER": "alice", "DB_PASS": "s3cr3t"}

    def test_missing_field_skipped(self) -> None:
        raw = {"username": "alice"}
        env_map = {"username": "DB_USER", "password": "DB_PASS"}
        result = _apply_env_map(raw, env_map)
        assert result == {"DB_USER": "alice"}
        assert "DB_PASS" not in result


# ---------------------------------------------------------------------------
# InjectionConfig validation
# ---------------------------------------------------------------------------


class TestInjectionConfig:
    def test_default_ttl(self) -> None:
        config = InjectionConfig(provider="vault", path="secret/foo")
        assert config.ttl == 900

    def test_env_map_defaults_empty(self) -> None:
        config = InjectionConfig(provider="vault", path="secret/foo")
        assert config.env_map == {}

    def test_invalid_provider_raises_on_init(self) -> None:
        config = InjectionConfig(provider="vault", path="x")
        # Provider validation happens on VaultInjector, not InjectionConfig
        bad_config = InjectionConfig(provider="invalid", path="x")  # type: ignore[arg-type]
        with pytest.raises(VaultInjectionError, match="Unknown provider"):
            VaultInjector(bad_config)


# ---------------------------------------------------------------------------
# CredentialLease
# ---------------------------------------------------------------------------


class TestCredentialLease:
    def test_construction(self) -> None:
        lease = CredentialLease(
            provider="vault",
            lease_id="lease-abc-123",
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
            revocable=True,
        )
        assert lease.provider == "vault"
        assert lease.revocable is True


# ---------------------------------------------------------------------------
# VaultInjector — Vault provider (mocked HTTP)
# ---------------------------------------------------------------------------


class TestVaultProvider:
    def _make_vault_response(self, data: dict[str, str], lease_id: str = "vault-lease-xyz") -> bytes:
        payload: dict[str, Any] = {
            "lease_id": lease_id,
            "lease_duration": 300,
            "data": data,
        }
        return json.dumps(payload).encode()

    def test_inject_returns_env_vars(self) -> None:
        config = InjectionConfig(
            provider="vault",
            path="database/creds",
            vault_role="agent-role",
            env_map={"username": "DB_USER", "password": "DB_PASS"},
            ttl=300,
        )
        mock_response = MagicMock()
        mock_response.read.return_value = self._make_vault_response({"username": "alice", "password": "secret"})
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            with patch.dict("os.environ", {"VAULT_TOKEN": "test-token", "VAULT_ADDR": "http://localhost:8200"}):
                injector = VaultInjector(config)
                env_vars, lease = injector.inject()

        assert env_vars["DB_USER"] == "alice"
        assert env_vars["DB_PASS"] == "secret"
        assert lease.provider == "vault"
        assert lease.lease_id == "vault-lease-xyz"
        assert lease.revocable is True

    def test_inject_without_role_uses_get(self) -> None:
        config = InjectionConfig(
            provider="vault",
            path="secret/data/myapp",
            env_map={},
            ttl=300,
        )
        kv_response: dict[str, Any] = {
            "lease_id": "",
            "lease_duration": 0,
            "data": {"data": {"API_KEY": "abc123"}},
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(kv_response).encode()
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            with patch.dict("os.environ", {"VAULT_TOKEN": "test-token"}):
                injector = VaultInjector(config)
                env_vars, lease = injector.inject()

        assert env_vars["API_KEY"] == "abc123"
        assert lease.revocable is False  # no lease_id

    def test_inject_no_token_raises(self) -> None:
        config = InjectionConfig(provider="vault", path="secret/foo")
        with patch.dict("os.environ", {}, clear=True):
            injector = VaultInjector(config)
            with pytest.raises(VaultInjectionError, match="VAULT_TOKEN"):
                injector.inject()

    def test_revoke_calls_vault_api(self) -> None:
        config = InjectionConfig(provider="vault", path="database/creds")
        lease = CredentialLease(
            provider="vault",
            lease_id="lease-to-revoke",
            expires_at=datetime.now(tz=UTC) + timedelta(minutes=5),
            revocable=True,
        )
        mock_response = MagicMock()
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            with patch.dict("os.environ", {"VAULT_TOKEN": "tok"}):
                injector = VaultInjector(config)
                injector.revoke(lease)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "leases/revoke" in req.full_url

    def test_revoke_no_lease_id_is_noop(self) -> None:
        config = InjectionConfig(provider="vault", path="kv/secret")
        lease = CredentialLease(
            provider="vault",
            lease_id="",
            expires_at=datetime.now(tz=UTC) + timedelta(minutes=5),
            revocable=False,
        )
        with patch("urllib.request.urlopen") as mock_urlopen:
            with patch.dict("os.environ", {"VAULT_TOKEN": "tok"}):
                VaultInjector(config).revoke(lease)
        mock_urlopen.assert_not_called()

    def test_context_manager_revokes_on_exit(self) -> None:
        config = InjectionConfig(
            provider="vault",
            path="database/creds",
            vault_role="role",
            env_map={"username": "U", "password": "P"},
        )

        inject_response = MagicMock()
        inject_response.read.return_value = self._make_vault_response({"username": "bob", "password": "pass"})
        inject_response.__enter__ = lambda self: self
        inject_response.__exit__ = MagicMock(return_value=False)

        revoke_response = MagicMock()
        revoke_response.__enter__ = lambda self: self
        revoke_response.__exit__ = MagicMock(return_value=False)

        responses = [inject_response, revoke_response]

        with patch("urllib.request.urlopen", side_effect=responses):
            with patch.dict("os.environ", {"VAULT_TOKEN": "tok"}):
                with VaultInjector(config) as env_vars:
                    assert env_vars["U"] == "bob"
                # revoke should have been called


# ---------------------------------------------------------------------------
# VaultInjector — AWS provider (mocked boto3)
# ---------------------------------------------------------------------------


class TestAwsProvider:
    def _mock_sts(self, *, role: bool = False) -> MagicMock:
        creds = {
            "AccessKeyId": "AKIATESTKEY12345678",
            "SecretAccessKey": "secretaccesskey",
            "SessionToken": "sessiontoken",
            "Expiration": datetime.now(tz=UTC) + timedelta(hours=1),
        }
        client_mock = MagicMock()
        if role:
            client_mock.assume_role.return_value = {"Credentials": creds}
        else:
            client_mock.get_session_token.return_value = {"Credentials": creds}
        boto3_mock = MagicMock()
        boto3_mock.client.return_value = client_mock
        return boto3_mock

    def test_inject_get_session_token(self) -> None:
        config = InjectionConfig(provider="aws", path="", ttl=3600)
        boto3_mock = self._mock_sts(role=False)

        with patch.dict("sys.modules", {"boto3": boto3_mock}):
            injector = VaultInjector(config)
            env_vars, lease = injector.inject()

        assert "AWS_ACCESS_KEY_ID" in env_vars
        assert "AWS_SECRET_ACCESS_KEY" in env_vars
        assert "AWS_SESSION_TOKEN" in env_vars
        assert env_vars["AWS_ACCESS_KEY_ID"].startswith("AKIAT")
        assert lease.provider == "aws"
        assert lease.revocable is False

    def test_inject_assume_role(self) -> None:
        config = InjectionConfig(
            provider="aws",
            path="",
            aws_role_arn="arn:aws:iam::123456789012:role/BernsteinAgentRole",
            aws_session_name="bernstein-test",
            ttl=3600,
        )
        boto3_mock = self._mock_sts(role=True)

        with patch.dict("sys.modules", {"boto3": boto3_mock}):
            injector = VaultInjector(config)
            env_vars, lease = injector.inject()

        assert "AWS_ACCESS_KEY_ID" in env_vars
        boto3_mock.client.return_value.assume_role.assert_called_once()

    def test_revoke_is_noop(self) -> None:
        config = InjectionConfig(provider="aws", path="")
        lease = CredentialLease(
            provider="aws",
            lease_id="AKIATESTKEY",
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
            revocable=False,
        )
        # Should not raise or call any external service
        with patch.dict("sys.modules", {"boto3": MagicMock()}):
            VaultInjector(config).revoke(lease)

    def test_custom_env_map_applied(self) -> None:
        config = InjectionConfig(
            provider="aws",
            path="",
            env_map={
                "aws_access_key_id": "MY_KEY",
                "aws_secret_access_key": "MY_SECRET",
                "aws_session_token": "MY_TOKEN",
            },
        )
        boto3_mock = self._mock_sts()

        with patch.dict("sys.modules", {"boto3": boto3_mock}):
            injector = VaultInjector(config)
            env_vars, _ = injector.inject()

        assert "MY_KEY" in env_vars
        assert "AWS_ACCESS_KEY_ID" not in env_vars


# ---------------------------------------------------------------------------
# VaultInjector — 1Password provider (mocked subprocess)
# ---------------------------------------------------------------------------


class TestOnePasswordProvider:
    def _op_item_json(self, fields: dict[str, str]) -> str:
        return json.dumps(
            {
                "fields": [{"label": k, "value": v} for k, v in fields.items()],
            }
        )

    def test_inject_reads_item(self) -> None:
        config = InjectionConfig(
            provider="1password",
            path="My Vault/API Keys",
            env_map={"anthropic_key": "ANTHROPIC_API_KEY"},
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = self._op_item_json({"anthropic_key": "sk-ant-test123"})

        with patch("subprocess.run", return_value=mock_result):
            injector = VaultInjector(config)
            env_vars, lease = injector.inject()

        assert env_vars["ANTHROPIC_API_KEY"] == "sk-ant-test123"
        assert lease.provider == "1password"
        assert lease.revocable is False

    def test_inject_op_not_found(self) -> None:
        config = InjectionConfig(provider="1password", path="item")
        with patch("subprocess.run", side_effect=FileNotFoundError):
            injector = VaultInjector(config)
            with pytest.raises(VaultInjectionError, match="not found"):
                injector.inject()

    def test_inject_op_returns_nonzero(self) -> None:
        config = InjectionConfig(provider="1password", path="item")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "item not found"

        with patch("subprocess.run", return_value=mock_result):
            injector = VaultInjector(config)
            with pytest.raises(VaultInjectionError, match="item not found"):
                injector.inject()

    def test_revoke_is_noop(self) -> None:
        config = InjectionConfig(provider="1password", path="item")
        lease = CredentialLease(
            provider="1password",
            lease_id="",
            expires_at=datetime.now(tz=UTC) + timedelta(minutes=5),
            revocable=False,
        )
        # Should not raise or call subprocess
        with patch("subprocess.run") as mock_run:
            VaultInjector(config).revoke(lease)
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Context manager and convenience functions
# ---------------------------------------------------------------------------


class TestContextManagerAndConvenience:
    def test_inject_agent_credentials_convenience(self) -> None:
        config = InjectionConfig(
            provider="1password",
            path="item",
            env_map={"key": "MY_KEY"},
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"fields": [{"label": "key", "value": "val"}]})

        with patch("subprocess.run", return_value=mock_result):
            env_vars, lease = inject_agent_credentials(config)

        assert env_vars["MY_KEY"] == "val"
        assert lease.provider == "1password"

    def test_revoke_agent_credentials_convenience(self) -> None:
        config = InjectionConfig(provider="1password", path="item")
        lease = CredentialLease(
            provider="1password",
            lease_id="",
            expires_at=datetime.now(tz=UTC) + timedelta(minutes=5),
            revocable=False,
        )
        # Should not raise
        with patch("subprocess.run") as mock_run:
            revoke_agent_credentials(config, lease)
        mock_run.assert_not_called()

    def test_active_lease_cleared_after_revoke(self) -> None:
        config = InjectionConfig(
            provider="1password",
            path="item",
            env_map={"key": "K"},
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"fields": [{"label": "key", "value": "v"}]})

        with patch("subprocess.run", return_value=mock_result):
            injector = VaultInjector(config)
            _, lease = injector.inject()
            assert injector._active_lease is lease
            injector.revoke()
            assert injector._active_lease is None

    def test_revoke_without_inject_is_noop(self) -> None:
        config = InjectionConfig(provider="vault", path="x")
        with patch.dict("os.environ", {"VAULT_TOKEN": "tok"}):
            injector = VaultInjector(config)
            # No inject() called; should not raise
            injector.revoke()
