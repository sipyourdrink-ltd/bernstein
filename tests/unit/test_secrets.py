"""Unit tests for the secrets manager integration."""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.secrets import (
    AwsSecretsProvider,
    OnePasswordSecretsProvider,
    SecretsConfig,
    SecretsError,
    VaultSecretsProvider,
    _apply_field_map,
    _cache,
    _create_provider,
    _fallback_from_env,
    check_provider_connectivity,
    invalidate_cache,
    load_secrets,
)

# ---------------------------------------------------------------------------
# SecretsConfig
# ---------------------------------------------------------------------------


class TestSecretsConfig:
    def test_defaults(self) -> None:
        cfg = SecretsConfig(provider="vault", path="secret/bernstein")
        assert cfg.ttl == 300
        assert cfg.field_map == {}

    def test_custom_field_map(self) -> None:
        cfg = SecretsConfig(
            provider="aws",
            path="arn:aws:secretsmanager:us-east-1:123:secret:keys",
            ttl=60,
            field_map={"api_key": "ANTHROPIC_API_KEY"},
        )
        assert cfg.provider == "aws"
        assert cfg.field_map["api_key"] == "ANTHROPIC_API_KEY"


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


class TestProviderFactory:
    def test_creates_vault(self) -> None:
        p = _create_provider("vault")
        assert isinstance(p, VaultSecretsProvider)

    def test_creates_aws(self) -> None:
        p = _create_provider("aws")
        assert isinstance(p, AwsSecretsProvider)

    def test_creates_onepassword(self) -> None:
        p = _create_provider("1password")
        assert isinstance(p, OnePasswordSecretsProvider)


# ---------------------------------------------------------------------------
# _apply_field_map
# ---------------------------------------------------------------------------


class TestApplyFieldMap:
    def test_no_map_returns_raw(self) -> None:
        raw = {"ANTHROPIC_API_KEY": "sk-abc"}
        result = _apply_field_map(raw, {})
        assert result == raw

    def test_remap_fields(self) -> None:
        raw = {"anthropic_key": "sk-abc", "openai_key": "sk-xyz"}
        field_map = {"anthropic_key": "ANTHROPIC_API_KEY", "openai_key": "OPENAI_API_KEY"}
        result = _apply_field_map(raw, field_map)
        assert result == {"ANTHROPIC_API_KEY": "sk-abc", "OPENAI_API_KEY": "sk-xyz"}

    def test_missing_field_skipped(self) -> None:
        raw = {"anthropic_key": "sk-abc"}
        field_map = {"anthropic_key": "ANTHROPIC_API_KEY", "missing": "GONE"}
        result = _apply_field_map(raw, field_map)
        assert result == {"ANTHROPIC_API_KEY": "sk-abc"}


# ---------------------------------------------------------------------------
# _fallback_from_env
# ---------------------------------------------------------------------------


class TestFallbackFromEnv:
    def test_pulls_from_environ(self) -> None:
        cfg = SecretsConfig(
            provider="vault",
            path="secret/x",
            field_map={"k": "ANTHROPIC_API_KEY"},
        )
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-env"}, clear=False):
            result = _fallback_from_env(cfg)
        assert result == {"ANTHROPIC_API_KEY": "sk-env"}

    def test_missing_env_var_omitted(self) -> None:
        cfg = SecretsConfig(
            provider="vault",
            path="secret/x",
            field_map={"k": "NONEXISTENT_VAR_12345"},
        )
        result = _fallback_from_env(cfg)
        assert result == {}

    def test_no_field_map_returns_empty(self) -> None:
        cfg = SecretsConfig(provider="vault", path="secret/x")
        result = _fallback_from_env(cfg)
        assert result == {}


# ---------------------------------------------------------------------------
# VaultSecretsProvider
# ---------------------------------------------------------------------------


class TestVaultProvider:
    def test_fetch_parses_kv2_response(self) -> None:
        kv2_body = json.dumps(
            {
                "data": {
                    "data": {"ANTHROPIC_API_KEY": "sk-vault", "OTHER": "val"},
                    "metadata": {"version": 1},
                }
            }
        ).encode()

        provider = VaultSecretsProvider()
        provider._addr = "http://vault.test:8200"
        provider._token = "test-token"  # NOSONAR — test fixture

        mock_response = MagicMock()
        mock_response.read.return_value = kv2_body
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = provider.fetch("secret/bernstein")

        assert result == {"ANTHROPIC_API_KEY": "sk-vault", "OTHER": "val"}  # NOSONAR — test fixture

    def test_check_connectivity_ok(self) -> None:
        health_body = json.dumps({"sealed": False, "initialized": True}).encode()

        provider = VaultSecretsProvider()
        provider._addr = "http://vault.test:8200"
        provider._token = "test-token"  # NOSONAR — test fixture

        mock_response = MagicMock()
        mock_response.read.return_value = health_body
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            ok, detail = provider.check_connectivity()

        assert ok is True
        assert "unsealed" in detail

    def test_check_connectivity_no_token(self) -> None:
        provider = VaultSecretsProvider()
        provider._token = ""
        ok, detail = provider.check_connectivity()
        assert ok is False
        assert "VAULT_TOKEN" in detail

    def test_check_connectivity_sealed(self) -> None:
        health_body = json.dumps({"sealed": True}).encode()

        provider = VaultSecretsProvider()
        provider._addr = "http://vault.test:8200"
        provider._token = "test-token"  # NOSONAR — test fixture

        mock_response = MagicMock()
        mock_response.read.return_value = health_body
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            ok, detail = provider.check_connectivity()

        assert ok is False
        assert "sealed" in detail


# ---------------------------------------------------------------------------
# AwsSecretsProvider
# ---------------------------------------------------------------------------


class TestAwsProvider:
    def test_fetch_no_boto3(self) -> None:
        provider = AwsSecretsProvider()
        with patch.dict("sys.modules", {"boto3": None}), pytest.raises(SecretsError, match="boto3"):
            provider.fetch("my-secret")

    def test_fetch_parses_json_secret(self) -> None:
        provider = AwsSecretsProvider()
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"API_KEY": "sk-aws"}),
        }
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            result = provider.fetch("my-secret")

        assert result == {"API_KEY": "sk-aws"}

    def test_check_connectivity_ok(self) -> None:
        provider = AwsSecretsProvider()
        mock_client = MagicMock()
        mock_client.get_caller_identity.return_value = {"Account": "123456789"}
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            ok, detail = provider.check_connectivity()

        assert ok is True
        assert "123456789" in detail


# ---------------------------------------------------------------------------
# OnePasswordSecretsProvider
# ---------------------------------------------------------------------------


class TestOnePasswordProvider:
    def test_fetch_parses_fields(self) -> None:
        item_json = json.dumps(
            {
                "id": "abc123",
                "fields": [
                    {"label": "API_KEY", "value": "sk-1p"},
                    {"label": "SECRET", "value": "s3cret"},
                    {"label": "", "value": "ignored"},
                ],
            }
        )

        provider = OnePasswordSecretsProvider()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=item_json,
                stderr="",
            )
            result = provider.fetch("vault/item")

        assert result == {"API_KEY": "sk-1p", "SECRET": "s3cret"}

    def test_fetch_not_installed(self) -> None:
        provider = OnePasswordSecretsProvider()
        with patch("subprocess.run", side_effect=FileNotFoundError), pytest.raises(SecretsError, match="not found"):
            provider.fetch("vault/item")

    def test_fetch_cli_failure(self) -> None:
        provider = OnePasswordSecretsProvider()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="not signed in",
            )
            with pytest.raises(SecretsError, match="not signed in"):
                provider.fetch("vault/item")

    def test_check_connectivity_ok(self) -> None:
        provider = OnePasswordSecretsProvider()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"email": "user@example.com"}),
                stderr="",
            )
            ok, detail = provider.check_connectivity()

        assert ok is True
        assert "user@example.com" in detail

    def test_check_connectivity_not_installed(self) -> None:
        provider = OnePasswordSecretsProvider()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            ok, detail = provider.check_connectivity()
        assert ok is False
        assert "not installed" in detail


# ---------------------------------------------------------------------------
# load_secrets — caching and fallback
# ---------------------------------------------------------------------------


class TestLoadSecrets:
    def setup_method(self) -> None:
        _cache.clear()

    def test_caches_result(self) -> None:
        cfg = SecretsConfig(provider="vault", path="secret/test", ttl=300)

        mock_provider = MagicMock()
        mock_provider.fetch.return_value = {"KEY": "val"}

        with patch("bernstein.core.secrets._create_provider", return_value=mock_provider):
            r1 = load_secrets(cfg)
            r2 = load_secrets(cfg)

        assert r1 == {"KEY": "val"}
        assert r2 == {"KEY": "val"}
        # Only one actual fetch call — second was from cache.
        assert mock_provider.fetch.call_count == 1

    def test_cache_expiry(self) -> None:
        cfg = SecretsConfig(provider="vault", path="secret/test", ttl=1)

        mock_provider = MagicMock()
        mock_provider.fetch.return_value = {"KEY": "val"}

        with patch("bernstein.core.secrets._create_provider", return_value=mock_provider):
            load_secrets(cfg)
            # Manually expire cache
            _cache["vault:secret/test"].fetched_at = time.monotonic() - 10
            load_secrets(cfg)

        assert mock_provider.fetch.call_count == 2

    def test_fallback_on_error(self) -> None:
        cfg = SecretsConfig(
            provider="vault",
            path="secret/test",
            field_map={"k": "ANTHROPIC_API_KEY"},
        )

        mock_provider = MagicMock()
        mock_provider.fetch.side_effect = SecretsError("unreachable")

        with (
            patch("bernstein.core.secrets._create_provider", return_value=mock_provider),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-fallback"}, clear=False),
        ):
            result = load_secrets(cfg)

        assert result == {"ANTHROPIC_API_KEY": "sk-fallback"}

    def test_field_map_applied(self) -> None:
        cfg = SecretsConfig(
            provider="vault",
            path="secret/test",
            field_map={"api_key": "ANTHROPIC_API_KEY"},
        )

        mock_provider = MagicMock()
        mock_provider.fetch.return_value = {"api_key": "sk-mapped"}

        with patch("bernstein.core.secrets._create_provider", return_value=mock_provider):
            result = load_secrets(cfg)

        assert result == {"ANTHROPIC_API_KEY": "sk-mapped"}


# ---------------------------------------------------------------------------
# invalidate_cache
# ---------------------------------------------------------------------------


class TestInvalidateCache:
    def setup_method(self) -> None:
        _cache.clear()

    def test_clear_all(self) -> None:
        cfg = SecretsConfig(provider="vault", path="secret/test")
        mock_provider = MagicMock()
        mock_provider.fetch.return_value = {"K": "V"}

        with patch("bernstein.core.secrets._create_provider", return_value=mock_provider):
            load_secrets(cfg)

        assert len(_cache) == 1
        invalidate_cache()
        assert len(_cache) == 0

    def test_clear_specific(self) -> None:
        cfg1 = SecretsConfig(provider="vault", path="secret/a")
        cfg2 = SecretsConfig(provider="aws", path="secret/b")

        mock_provider = MagicMock()
        mock_provider.fetch.return_value = {"K": "V"}

        with patch("bernstein.core.secrets._create_provider", return_value=mock_provider):
            load_secrets(cfg1)
            load_secrets(cfg2)

        assert len(_cache) == 2
        invalidate_cache(cfg1)
        assert len(_cache) == 1
        assert "aws:secret/b" in _cache


# ---------------------------------------------------------------------------
# check_provider_connectivity
# ---------------------------------------------------------------------------


class TestCheckProviderConnectivity:
    def test_delegates_to_provider(self) -> None:
        cfg = SecretsConfig(provider="vault", path="secret/test")
        mock_provider = MagicMock()
        mock_provider.check_connectivity.return_value = (True, "OK")

        with patch("bernstein.core.secrets._create_provider", return_value=mock_provider):
            ok, detail = check_provider_connectivity(cfg)

        assert ok is True
        assert detail == "OK"


# ---------------------------------------------------------------------------
# build_filtered_env with secrets_config
# ---------------------------------------------------------------------------


class TestBuildFilteredEnvWithSecrets:
    def test_secrets_injected_into_env(self) -> None:
        from bernstein.adapters.env_isolation import build_filtered_env

        cfg = SecretsConfig(provider="vault", path="secret/test")

        fake_env: dict[str, str] = {"PATH": "/bin", "HOME": "/home/u"}

        with (
            patch("bernstein.adapters.env_isolation.os.environ", fake_env),
            patch("bernstein.core.secrets.load_secrets", return_value={"ANTHROPIC_API_KEY": "sk-from-vault"}),
        ):
            result = build_filtered_env(secrets_config=cfg)

        assert result["PATH"] == "/bin"
        assert result["ANTHROPIC_API_KEY"] == "sk-from-vault"

    def test_secrets_override_env_vars(self) -> None:
        from bernstein.adapters.env_isolation import build_filtered_env

        cfg = SecretsConfig(provider="vault", path="secret/test")

        fake_env: dict[str, str] = {
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "sk-env-old",
        }

        with (
            patch("bernstein.adapters.env_isolation.os.environ", fake_env),
            patch("bernstein.core.secrets.load_secrets", return_value={"ANTHROPIC_API_KEY": "sk-from-vault"}),
        ):
            result = build_filtered_env(["ANTHROPIC_API_KEY"], secrets_config=cfg)

        # Secrets manager value takes precedence over env var.
        assert result["ANTHROPIC_API_KEY"] == "sk-from-vault"

    def test_no_secrets_config_unchanged(self) -> None:
        from bernstein.adapters.env_isolation import build_filtered_env

        fake_env: dict[str, str] = {"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-env"}

        with patch("bernstein.adapters.env_isolation.os.environ", fake_env):
            result = build_filtered_env(["ANTHROPIC_API_KEY"])

        assert result["ANTHROPIC_API_KEY"] == "sk-env"


# ---------------------------------------------------------------------------
# Seed config parsing
# ---------------------------------------------------------------------------


class TestSeedSecretsParsing:
    def test_parse_secrets_section(self, tmp_path: Any) -> None:
        from bernstein.core.seed import parse_seed

        seed_yaml = tmp_path / "bernstein.yaml"
        seed_yaml.write_text(
            "goal: test\n"
            "secrets:\n"
            "  provider: vault\n"
            "  path: secret/bernstein\n"
            "  ttl: 60\n"
            "  field_map:\n"
            "    api_key: ANTHROPIC_API_KEY\n"
        )

        cfg = parse_seed(seed_yaml)
        assert cfg.secrets is not None
        assert cfg.secrets.provider == "vault"
        assert cfg.secrets.path == "secret/bernstein"
        assert cfg.secrets.ttl == 60
        assert cfg.secrets.field_map == {"api_key": "ANTHROPIC_API_KEY"}

    def test_parse_secrets_minimal(self, tmp_path: Any) -> None:
        from bernstein.core.seed import parse_seed

        seed_yaml = tmp_path / "bernstein.yaml"
        seed_yaml.write_text(
            "goal: test\nsecrets:\n  provider: aws\n  path: arn:aws:secretsmanager:us-east-1:123:secret:keys\n"
        )

        cfg = parse_seed(seed_yaml)
        assert cfg.secrets is not None
        assert cfg.secrets.provider == "aws"
        assert cfg.secrets.ttl == 300  # default

    def test_parse_no_secrets(self, tmp_path: Any) -> None:
        from bernstein.core.seed import parse_seed

        seed_yaml = tmp_path / "bernstein.yaml"
        seed_yaml.write_text("goal: test\n")

        cfg = parse_seed(seed_yaml)
        assert cfg.secrets is None

    def test_parse_invalid_provider(self, tmp_path: Any) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        seed_yaml = tmp_path / "bernstein.yaml"
        seed_yaml.write_text("goal: test\nsecrets:\n  provider: invalid\n  path: foo\n")

        with pytest.raises(SeedError, match=r"secrets\.provider"):
            parse_seed(seed_yaml)

    def test_parse_missing_path(self, tmp_path: Any) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        seed_yaml = tmp_path / "bernstein.yaml"
        seed_yaml.write_text("goal: test\nsecrets:\n  provider: vault\n")

        with pytest.raises(SeedError, match=r"secrets\.path"):
            parse_seed(seed_yaml)
