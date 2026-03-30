"""Secrets manager integration: load API keys from external providers.

Supports AWS Secrets Manager, HashiCorp Vault, and 1Password CLI.
Falls back to environment variables when the secrets manager is unavailable.

Usage in bernstein.yaml::

    secrets:
      provider: vault
      path: "secret/bernstein"
      ttl: 300  # refresh every 5 minutes
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Supported provider names.
SecretsProviderType = Literal["vault", "aws", "1password"]

_VALID_PROVIDERS: frozenset[str] = frozenset({"vault", "aws", "1password"})


class SecretsError(Exception):
    """Raised when a secrets manager operation fails."""


class SecretsRefresher:
    """Background thread that refreshes secrets before they expire.

    Ensures that ``load_secrets`` always has a fresh entry in its module-level
    cache, preventing latencies when agents are spawned.
    """

    def __init__(self, config: SecretsConfig) -> None:
        self.config = config
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Launch the background refresh thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="secrets-refresher",
            daemon=True,
        )
        self._thread.start()
        logger.info("Secrets background refresher started (ttl=%ds)", self.config.ttl)

    def stop(self) -> None:
        """Signal the background thread to exit."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        logger.info("Secrets background refresher stopped")

    def _run(self) -> None:
        """Core refresh loop."""
        # Refresh interval: 80% of TTL to stay ahead of expiry
        interval = max(30.0, self.config.ttl * 0.8)
        while not self._stop_event.is_set():
            try:
                # Direct call to fetch() bypasses cache logic
                provider = _create_provider(self.config.provider)
                raw = provider.fetch(self.config.path)

                # Update module-level cache
                key = _cache_key(self.config)
                _cache[key] = _CachedSecrets(
                    values=raw,
                    fetched_at=time.monotonic(),
                    ttl=self.config.ttl,
                )
                logger.debug("Background secrets refresh successful for %s", key)
            except Exception as exc:
                logger.warning("Background secrets refresh failed for %s: %s", self.config.path, exc)

            # Wait for next interval or stop signal
            self._stop_event.wait(timeout=interval)


@dataclass(frozen=True)
class SecretsConfig:
    """Configuration for an external secrets provider.

    Attributes:
        provider: Which secrets backend to use.
        path: Path/ARN/vault reference for the secret.
        ttl: Seconds before cached secrets are refreshed (0 = no cache).
        field_map: Optional mapping from secret field names to env var names.
            E.g. ``{"anthropic_key": "ANTHROPIC_API_KEY"}``.
    """

    provider: SecretsProviderType
    path: str
    ttl: int = 300
    field_map: dict[str, str] = field(default_factory=dict)


class SecretsProvider(ABC):
    """Abstract interface for secrets backends."""

    @abstractmethod
    def fetch(self, path: str) -> dict[str, str]:
        """Fetch secret key-value pairs from the backend.

        Args:
            path: Provider-specific secret location (ARN, Vault path, etc.).

        Returns:
            Dict mapping field names to secret values.

        Raises:
            SecretsError: On connectivity or auth failures.
        """

    @abstractmethod
    def check_connectivity(self) -> tuple[bool, str]:
        """Verify the provider is reachable and authenticated.

        Returns:
            Tuple of (ok, detail_message).
        """


# ---------------------------------------------------------------------------
# AWS Secrets Manager
# ---------------------------------------------------------------------------


class AwsSecretsProvider(SecretsProvider):
    """Load secrets from AWS Secrets Manager via boto3."""

    def fetch(self, path: str) -> dict[str, str]:
        """Fetch a secret by ARN or name from AWS Secrets Manager.

        Args:
            path: Secret ARN or name.

        Returns:
            Parsed JSON key-value pairs from the secret string.
        """
        try:
            import boto3  # type: ignore[import-untyped]
        except ImportError as exc:
            raise SecretsError("boto3 is required for AWS Secrets Manager: pip install boto3") from exc

        try:
            client = boto3.client("secretsmanager")  # type: ignore[reportUnknownMemberType]
            response = client.get_secret_value(SecretId=path)  # type: ignore[reportUnknownMemberType]
            secret_string: str = response["SecretString"]
            parsed: object = json.loads(secret_string)
            if not isinstance(parsed, dict):
                raise SecretsError(f"AWS secret {path!r} is not a JSON object")
            return {str(k): str(v) for k, v in parsed.items()}
        except SecretsError:
            raise
        except Exception as exc:
            raise SecretsError(f"AWS Secrets Manager fetch failed: {exc}") from exc

    def check_connectivity(self) -> tuple[bool, str]:
        """Check AWS credentials and Secrets Manager access."""
        try:
            import boto3  # type: ignore[import-untyped]

            client = boto3.client("sts")  # type: ignore[reportUnknownMemberType]
            identity = client.get_caller_identity()  # type: ignore[reportUnknownMemberType]
            account: str = identity.get("Account", "unknown")  # type: ignore[reportUnknownMemberType]
            return True, f"AWS authenticated (account {account})"
        except ImportError:
            return False, "boto3 not installed (pip install boto3)"
        except Exception as exc:
            return False, f"AWS auth failed: {exc}"


# ---------------------------------------------------------------------------
# HashiCorp Vault
# ---------------------------------------------------------------------------


class VaultSecretsProvider(SecretsProvider):
    """Load secrets from HashiCorp Vault via HTTP API or CLI."""

    def __init__(self) -> None:
        self._addr = os.environ.get("VAULT_ADDR", "http://127.0.0.1:8200")
        self._token = os.environ.get("VAULT_TOKEN", "")

    def fetch(self, path: str) -> dict[str, str]:
        """Fetch a secret from Vault's KV v2 engine.

        Args:
            path: Vault secret path (e.g. ``secret/bernstein``).

        Returns:
            Key-value pairs from the secret data.
        """
        import urllib.error
        import urllib.request

        # KV v2 API: GET /v1/{mount}/data/{path}
        # For "secret/bernstein" -> mount=secret, secret_path=bernstein
        parts = path.split("/", 1)
        if len(parts) == 2:
            mount, secret_path = parts
        else:
            mount, secret_path = "secret", parts[0]

        url = f"{self._addr}/v1/{mount}/data/{secret_path}"
        req = urllib.request.Request(url)
        req.add_header("X-Vault-Token", self._token)

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode())
            data: object = body.get("data", {}).get("data", {})
            if not isinstance(data, dict):
                raise SecretsError(f"Vault secret {path!r} has unexpected structure")
            return {str(k): str(v) for k, v in data.items()}
        except SecretsError:
            raise
        except urllib.error.HTTPError as exc:
            raise SecretsError(f"Vault HTTP {exc.code}: {exc.reason}") from exc
        except Exception as exc:
            raise SecretsError(f"Vault fetch failed for {path!r}: {exc}") from exc

    def check_connectivity(self) -> tuple[bool, str]:
        """Check Vault seal status and token validity."""
        import urllib.error
        import urllib.request

        if not self._token:
            return False, "VAULT_TOKEN not set"

        url = f"{self._addr}/v1/sys/health"
        req = urllib.request.Request(url)
        req.add_header("X-Vault-Token", self._token)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode())
            if body.get("sealed"):
                return False, f"Vault at {self._addr} is sealed"
            return True, f"Vault at {self._addr} is unsealed and authenticated"
        except urllib.error.HTTPError as exc:
            return False, f"Vault HTTP {exc.code}: {exc.reason}"
        except Exception as exc:
            return False, f"Vault unreachable at {self._addr}: {exc}"


# ---------------------------------------------------------------------------
# 1Password CLI
# ---------------------------------------------------------------------------


class OnePasswordSecretsProvider(SecretsProvider):
    """Load secrets from 1Password via the ``op`` CLI."""

    def fetch(self, path: str) -> dict[str, str]:
        """Fetch fields from a 1Password item.

        Args:
            path: 1Password item reference (e.g. ``vault/item`` or an item URI).

        Returns:
            Key-value pairs from the item's fields.
        """
        try:
            result = subprocess.run(
                ["op", "item", "get", path, "--format=json"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except FileNotFoundError as exc:
            raise SecretsError(
                "1Password CLI (op) not found — install from https://1password.com/downloads/command-line/"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SecretsError("1Password CLI timed out") from exc

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise SecretsError(f"1Password CLI failed: {stderr}")

        try:
            item: dict[str, Any] = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SecretsError(f"Failed to parse 1Password output: {exc}") from exc

        secrets: dict[str, str] = {}
        fields: list[dict[str, Any]] = item.get("fields", [])
        for f in fields:
            label: str = f.get("label", "")
            value: str = f.get("value", "")
            if label and value:
                secrets[label] = value
        return secrets

    def check_connectivity(self) -> tuple[bool, str]:
        """Check that the ``op`` CLI is installed and signed in."""
        try:
            result = subprocess.run(
                ["op", "whoami", "--format=json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if "not signed in" in stderr.lower() or "sign in" in stderr.lower():
                    return False, "1Password CLI not signed in (run: eval $(op signin))"
                return False, f"1Password CLI error: {stderr}"
            info: dict[str, Any] = json.loads(result.stdout)
            email: str = info.get("email", "unknown")
            return True, f"1Password authenticated ({email})"
        except FileNotFoundError:
            return False, "1Password CLI (op) not installed"
        except subprocess.TimeoutExpired:
            return False, "1Password CLI timed out"
        except Exception as exc:
            return False, f"1Password check failed: {exc}"


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

_PROVIDERS: dict[SecretsProviderType, type[SecretsProvider]] = {
    "aws": AwsSecretsProvider,
    "vault": VaultSecretsProvider,
    "1password": OnePasswordSecretsProvider,
}


def _create_provider(provider_type: SecretsProviderType) -> SecretsProvider:
    """Instantiate a secrets provider by type name.

    Args:
        provider_type: One of ``vault``, ``aws``, ``1password``.

    Returns:
        Provider instance.
    """
    cls = _PROVIDERS[provider_type]
    return cls()


# ---------------------------------------------------------------------------
# Cached secret store with TTL
# ---------------------------------------------------------------------------


@dataclass
class _CachedSecrets:
    """Internal cache entry for fetched secrets."""

    values: dict[str, str]
    fetched_at: float
    ttl: int


# Module-level cache keyed by provider+path.
_cache: dict[str, _CachedSecrets] = {}


def _cache_key(config: SecretsConfig) -> str:
    return f"{config.provider}:{config.path}"


def load_secrets(config: SecretsConfig) -> dict[str, str]:
    """Load secrets from the configured provider, with TTL caching.

    Applies ``field_map`` to rename fields to env var names.
    Falls back to environment variables on provider failure.

    Args:
        config: Secrets configuration from bernstein.yaml.

    Returns:
        Dict mapping env var names to secret values.
    """
    key = _cache_key(config)
    now = time.monotonic()

    # Check cache
    cached = _cache.get(key)
    if cached is not None and config.ttl > 0:
        age = now - cached.fetched_at
        if age < cached.ttl:
            logger.debug("Using cached secrets for %s (age=%.0fs, ttl=%ds)", key, age, config.ttl)
            return cached.values

    # Fetch from provider
    provider = _create_provider(config.provider)
    try:
        raw_secrets = provider.fetch(config.path)
    except SecretsError as exc:
        logger.warning("Secrets manager unavailable (%s), falling back to env vars: %s", config.provider, exc)
        return _fallback_from_env(config)

    # Apply field_map: remap raw field names to env var names
    mapped = _apply_field_map(raw_secrets, config.field_map)

    # Cache result
    _cache[key] = _CachedSecrets(values=mapped, fetched_at=now, ttl=config.ttl)
    logger.info("Loaded %d secret(s) from %s:%s", len(mapped), config.provider, config.path)
    return mapped


def _apply_field_map(raw: dict[str, str], field_map: dict[str, str]) -> dict[str, str]:
    """Remap secret field names using the configured field_map.

    If no field_map is provided, returns the raw secrets as-is
    (field names become env var names directly).

    Args:
        raw: Raw secrets from the provider.
        field_map: Mapping from secret field name to env var name.

    Returns:
        Remapped dict.
    """
    if not field_map:
        return dict(raw)

    result: dict[str, str] = {}
    for secret_field, env_var in field_map.items():
        if secret_field in raw:
            result[env_var] = raw[secret_field]
        else:
            logger.warning("Secret field %r not found in provider response", secret_field)
    return result


def _fallback_from_env(config: SecretsConfig) -> dict[str, str]:
    """Build a secrets dict from environment variables as fallback.

    When the secrets manager is unavailable, this pulls values from
    os.environ for any env var names in the field_map values.

    Args:
        config: Secrets configuration (used for field_map).

    Returns:
        Dict of env var name -> value for vars that are set.
    """
    result: dict[str, str] = {}
    if config.field_map:
        for env_var in config.field_map.values():
            val = os.environ.get(env_var)
            if val:
                result[env_var] = val
    return result


def invalidate_cache(config: SecretsConfig | None = None) -> None:
    """Clear the secrets cache.

    Args:
        config: If provided, only clear this config's cache entry.
            If None, clear all cached secrets.
    """
    if config is None:
        _cache.clear()
    else:
        _cache.pop(_cache_key(config), None)


def check_provider_connectivity(config: SecretsConfig) -> tuple[bool, str]:
    """Check whether the configured secrets provider is reachable.

    Args:
        config: Secrets configuration.

    Returns:
        Tuple of (ok, detail_message).
    """
    provider = _create_provider(config.provider)
    return provider.check_connectivity()
