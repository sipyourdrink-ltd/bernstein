"""Runtime secrets vault integration — just-in-time credential injection.

Fetches credentials from HashiCorp Vault, AWS Secrets Manager, or 1Password
at agent spawn time, injects them as environment variables, and revokes /
expires them when the agent exits.

This is distinct from ``secrets.py`` (which handles persistent storage and
caching).  The injector is designed for ephemeral, per-agent credentials:

- **HashiCorp Vault dynamic secrets**: creates a short-lived lease; revokes via
  ``/v1/sys/leases/revoke`` on exit.
- **AWS STS temporary credentials**: requests ``assume_role`` or
  ``get_session_token`` with a short duration; credentials expire automatically.
- **1Password**: reads a static item; no lease to revoke, but the value is
  cleared from the returned env dict after injection so it cannot be re-read.

Usage::

    config = InjectionConfig(
        provider="vault",
        path="database/creds/agent-role",
        env_map={"username": "DB_USER", "password": "DB_PASSWORD"},
        ttl=300,
    )
    injector = VaultInjector(config)
    env_vars, lease = injector.inject()
    try:
        # spawn agent with env_vars merged into environment
        ...
    finally:
        injector.revoke(lease)

Or as a context manager::

    with VaultInjector(config) as env_vars:
        # credentials injected; automatically revoked on exit
        spawn_agent(extra_env=env_vars)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

logger = logging.getLogger(__name__)

InjectionProviderType = Literal["vault", "aws", "1password"]

_VALID_PROVIDERS: frozenset[str] = frozenset({"vault", "aws", "1password"})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InjectionConfig:
    """Configuration for just-in-time credential injection.

    Attributes:
        provider: Secrets backend to use.
        path: Provider-specific path/ARN/item reference for the credential.
        env_map: Mapping from secret field name to environment variable name.
            E.g. ``{"username": "DB_USER", "password": "DB_PASSWORD"}``.
            If empty, all fields are injected using their raw names as env var names.
        ttl: Credential lifetime in seconds (used for Vault leases and AWS STS).
            Providers will use the *minimum* of this value and their configured
            maximum.  Defaults to 900 (15 minutes).
        aws_role_arn: (AWS only) IAM role ARN to assume.  If omitted, uses
            ``get_session_token`` instead of ``assume_role``.
        aws_session_name: (AWS only) Role session name for audit trails.
        vault_role: (Vault only) Dynamic secrets role to request credentials for.
    """

    provider: InjectionProviderType
    path: str
    env_map: dict[str, str] = field(default_factory=dict)
    ttl: int = 900
    aws_role_arn: str = ""
    aws_session_name: str = "bernstein-agent"
    vault_role: str = ""


# ---------------------------------------------------------------------------
# Lease (tracks credentials for revocation)
# ---------------------------------------------------------------------------


@dataclass
class CredentialLease:
    """Tracks an issued credential set so it can be revoked on agent exit.

    Attributes:
        provider: Which backend issued this lease.
        lease_id: Provider-specific revocation handle (Vault lease ID, AWS
            access key ID, or empty string for 1Password).
        expires_at: When the credential expires (UTC).  The injector does not
            enforce this — it is informational for callers.
        revocable: Whether active revocation is supported.  AWS STS credentials
            expire automatically; 1Password has no revocation API.
    """

    provider: InjectionProviderType
    lease_id: str
    expires_at: datetime
    revocable: bool
    _metadata: dict[str, Any] = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------


class _VaultInjector:
    """Vault dynamic secrets: fetch a new lease, inject env vars, revoke on exit."""

    def __init__(self) -> None:
        self._addr = os.environ.get("VAULT_ADDR", "http://127.0.0.1:8200")
        self._token = os.environ.get("VAULT_TOKEN", "")

    def inject(self, config: InjectionConfig) -> tuple[dict[str, str], CredentialLease]:
        """Fetch Vault dynamic credentials and return (env_vars, lease)."""
        import urllib.error
        import urllib.request

        if not self._token:
            raise VaultInjectionError("VAULT_TOKEN is not set")

        # Dynamic secrets: POST /v1/{path}/creds/{role}
        # or KV read: GET /v1/{mount}/data/{path}
        if config.vault_role:
            url = f"{self._addr}/v1/{config.path}/creds/{config.vault_role}"
            data_bytes = json.dumps({"ttl": str(config.ttl)}).encode()
            req = urllib.request.Request(url, data=data_bytes, method="POST")
        else:
            url = f"{self._addr}/v1/{config.path}"
            req = urllib.request.Request(url, method="GET")

        req.add_header("X-Vault-Token", self._token)
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body: dict[str, Any] = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise VaultInjectionError(f"Vault HTTP {exc.code}: {exc.reason}") from exc
        except Exception as exc:
            raise VaultInjectionError(f"Vault request failed: {exc}") from exc

        # Dynamic secrets response: {lease_id, data: {...}}
        # KV v2 response: {data: {data: {...}}}
        raw_data: dict[str, str] = {}
        lease_id = str(body.get("lease_id", ""))
        lease_duration = int(body.get("lease_duration", config.ttl))

        if "data" in body:
            inner = body["data"]
            if isinstance(inner, dict) and "data" in inner:
                # KV v2
                raw_data = {str(k): str(v) for k, v in inner["data"].items()}
            elif isinstance(inner, dict):
                # Dynamic secrets
                raw_data = {str(k): str(v) for k, v in inner.items()}

        env_vars = _apply_env_map(raw_data, config.env_map)
        expires_at = datetime.now(tz=UTC) + timedelta(seconds=lease_duration)
        lease = CredentialLease(
            provider="vault",
            lease_id=lease_id,
            expires_at=expires_at,
            revocable=bool(lease_id),
        )
        logger.info(
            "Vault credential issued: path=%s lease=%s ttl=%ds",
            config.path,
            lease_id or "(static)",
            lease_duration,
        )
        return env_vars, lease

    def revoke(self, lease: CredentialLease) -> None:
        """Revoke a Vault lease."""
        import urllib.error
        import urllib.request

        if not lease.lease_id:
            logger.debug("Vault lease has no lease_id; nothing to revoke")
            return

        url = f"{self._addr}/v1/sys/leases/revoke"
        payload = json.dumps({"lease_id": lease.lease_id}).encode()
        req = urllib.request.Request(url, data=payload, method="PUT")
        req.add_header("X-Vault-Token", self._token)
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=10):
                pass  # Response body not needed for revocation
            logger.info("Vault lease revoked: %s", lease.lease_id)
        except urllib.error.HTTPError as exc:
            # 404 means already expired — not an error
            if exc.code == 404:
                logger.debug("Vault lease already expired: %s", lease.lease_id)
            else:
                logger.warning("Failed to revoke Vault lease %s: HTTP %d", lease.lease_id, exc.code)
        except Exception as exc:
            logger.warning("Failed to revoke Vault lease %s: %s", lease.lease_id, exc)


class _AwsInjector:
    """AWS STS temporary credentials: short-lived, expire automatically."""

    def inject(self, config: InjectionConfig) -> tuple[dict[str, str], CredentialLease]:
        """Fetch AWS STS credentials and return (env_vars, lease)."""
        try:
            import boto3  # type: ignore[import-untyped]
        except ImportError as exc:
            raise VaultInjectionError("boto3 required for AWS injection: pip install boto3") from exc

        ttl = max(900, min(config.ttl, 43200))  # STS min=900s, max=43200s

        try:
            sts = boto3.client("sts")  # type: ignore[reportUnknownMemberType]
            if config.aws_role_arn:
                response = sts.assume_role(  # type: ignore[reportUnknownMemberType]
                    RoleArn=config.aws_role_arn,
                    RoleSessionName=config.aws_session_name,
                    DurationSeconds=ttl,
                )
                creds = response["Credentials"]
            else:
                response = sts.get_session_token(DurationSeconds=ttl)  # type: ignore[reportUnknownMemberType]
                creds = response["Credentials"]
        except Exception as exc:
            raise VaultInjectionError(f"AWS STS failed: {exc}") from exc

        access_key = str(creds["AccessKeyId"])
        raw_data = {
            "aws_access_key_id": access_key,
            "aws_secret_access_key": str(creds["SecretAccessKey"]),
            "aws_session_token": str(creds["SessionToken"]),
        }
        # Default env map for AWS credentials (standard env var names)
        effective_env_map = config.env_map or {
            "aws_access_key_id": "AWS_ACCESS_KEY_ID",
            "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
            "aws_session_token": "AWS_SESSION_TOKEN",
        }
        env_vars = _apply_env_map(raw_data, effective_env_map)
        expiration = creds.get("Expiration")
        if expiration is not None and hasattr(expiration, "replace"):
            expires_at = expiration.replace(tzinfo=UTC) if expiration.tzinfo is None else expiration
        else:
            expires_at = datetime.now(tz=UTC) + timedelta(seconds=ttl)

        lease = CredentialLease(
            provider="aws",
            lease_id=access_key,
            expires_at=expires_at,
            revocable=False,  # STS credentials expire automatically
        )
        logger.info("AWS STS credential issued: key=%s ttl=%ds", access_key[:8] + "****", ttl)
        return env_vars, lease

    def revoke(self, lease: CredentialLease) -> None:
        """AWS STS credentials expire automatically; active revocation is not supported."""
        logger.debug(
            "AWS STS credentials expire automatically at %s; no active revocation needed",
            lease.expires_at.isoformat(),
        )


class _OnePasswordInjector:
    """1Password CLI injection: reads a static item; no lease/revocation API."""

    def inject(self, config: InjectionConfig) -> tuple[dict[str, str], CredentialLease]:
        """Fetch fields from a 1Password item and return (env_vars, lease)."""
        try:
            result = subprocess.run(
                ["op", "item", "get", config.path, "--format=json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
        except FileNotFoundError as exc:
            raise VaultInjectionError(
                "1Password CLI (op) not found — install from https://1password.com/downloads/command-line/"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise VaultInjectionError("1Password CLI timed out") from exc

        if result.returncode != 0:
            raise VaultInjectionError(f"1Password CLI failed: {result.stderr.strip()}")

        try:
            item: dict[str, Any] = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise VaultInjectionError(f"Failed to parse 1Password output: {exc}") from exc

        raw_data: dict[str, str] = {}
        for f in item.get("fields", []):
            label: str = str(f.get("label", ""))
            value: str = str(f.get("value", ""))
            if label and value:
                raw_data[label] = value

        env_vars = _apply_env_map(raw_data, config.env_map)
        expires_at = datetime.now(tz=UTC) + timedelta(seconds=config.ttl)
        lease = CredentialLease(
            provider="1password",
            lease_id="",  # no revocation handle
            expires_at=expires_at,
            revocable=False,
        )
        logger.info("1Password credential fetched: item=%s fields=%d", config.path, len(env_vars))
        return env_vars, lease

    def revoke(self, lease: CredentialLease) -> None:
        """1Password has no credential revocation API."""
        logger.debug("1Password credentials are static; no revocation available")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class VaultInjectionError(Exception):
    """Raised when JIT credential injection fails."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _apply_env_map(raw: dict[str, str], env_map: dict[str, str]) -> dict[str, str]:
    """Map raw secret field names to environment variable names.

    Args:
        raw: Secret key-value pairs from the provider.
        env_map: Mapping from field name to env var name.  If empty, raw fields
            are returned unchanged (field name becomes env var name).

    Returns:
        Dict mapping env var names to values.
    """
    if not env_map:
        return dict(raw)
    result: dict[str, str] = {}
    for field_name, env_var in env_map.items():
        value = raw.get(field_name)
        if value is not None:
            result[env_var] = value
        else:
            logger.warning("Secret field %r not found in provider response", field_name)
    return result


_PROVIDER_CLASSES: dict[InjectionProviderType, type[_VaultInjector | _AwsInjector | _OnePasswordInjector]] = {
    "vault": _VaultInjector,
    "aws": _AwsInjector,
    "1password": _OnePasswordInjector,
}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


class VaultInjector:
    """Just-in-time credential injector for agent spawn.

    Fetches short-lived credentials from a secrets provider at spawn time,
    exposes them as environment variable mappings, and revokes/expires them
    when the agent exits.

    Supports synchronous use (``inject`` / ``revoke``) and context manager
    use (credentials are automatically revoked on ``__exit__``).

    Args:
        config: Injection configuration specifying provider, path, env map, etc.
    """

    def __init__(self, config: InjectionConfig) -> None:
        if config.provider not in _VALID_PROVIDERS:
            raise VaultInjectionError(f"Unknown provider {config.provider!r}. Valid: {sorted(_VALID_PROVIDERS)}")
        self._config = config
        cls = _PROVIDER_CLASSES[config.provider]
        self._provider: _VaultInjector | _AwsInjector | _OnePasswordInjector = cls()
        self._active_lease: CredentialLease | None = None

    def inject(self) -> tuple[dict[str, str], CredentialLease]:
        """Fetch credentials from the configured provider.

        Returns:
            Tuple of (env_vars, lease).  ``env_vars`` is a dict mapping
            environment variable names to credential values — merge this into
            the agent's process environment.  ``lease`` tracks the issued
            credential for later revocation.

        Raises:
            VaultInjectionError: If the provider is unreachable or returns
                an error.
        """
        env_vars, lease = self._provider.inject(self._config)
        self._active_lease = lease
        return env_vars, lease

    def revoke(self, lease: CredentialLease | None = None) -> None:
        """Revoke / expire the issued credential.

        Args:
            lease: Lease to revoke.  If omitted, revokes the most recently
                issued lease (``inject()`` must have been called first).
        """
        target = lease or self._active_lease
        if target is None:
            logger.debug("VaultInjector.revoke called but no active lease")
            return
        self._provider.revoke(target)
        if target is self._active_lease:
            self._active_lease = None

    def __enter__(self) -> dict[str, str]:
        """Inject credentials; return env_vars dict for use in ``with`` block."""
        env_vars, _ = self.inject()
        return env_vars

    def __exit__(self, *_: object) -> None:
        """Revoke credentials on context manager exit (always, even on exception)."""
        self.revoke()


def inject_agent_credentials(config: InjectionConfig) -> tuple[dict[str, str], CredentialLease]:
    """Convenience function: fetch JIT credentials for a single agent spawn.

    Equivalent to ``VaultInjector(config).inject()`` but exposes a simple
    function interface for callers that manage the lease lifecycle themselves.

    Args:
        config: Injection configuration.

    Returns:
        Tuple of (env_vars, lease).
    """
    return VaultInjector(config).inject()


def revoke_agent_credentials(config: InjectionConfig, lease: CredentialLease) -> None:
    """Convenience function: revoke credentials after agent exit.

    Args:
        config: Must use the same provider as the lease that was issued.
        lease: Lease returned by ``inject_agent_credentials``.
    """
    VaultInjector(config).revoke(lease)
