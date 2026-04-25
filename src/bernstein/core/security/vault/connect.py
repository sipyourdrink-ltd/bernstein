"""High-level ``bernstein connect`` orchestration.

This module is the seam between the click command surface and the vault /
provider machinery. The CLI delegates here so all the "ask for the secret,
validate it, store it, audit it" logic stays in one testable place.

Design notes:

* The helpers take an explicit :class:`CredentialVault` so the CLI can
  inject the keyring backend and tests can inject a fake.
* All UI lives in the CLI; this module returns plain strings / dataclasses.
* ``perform_connect`` returns a dataclass rather than raising on whoami
  failure so the CLI can echo the masked token plus the validation error
  in one place.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bernstein.core.security.vault.audit import audit_event
from bernstein.core.security.vault.protocol import (
    CredentialVault,
    StoredSecret,
    VaultNotFoundError,
)
from bernstein.core.security.vault.providers import (
    AuthMode,
    ProviderConfig,
)
from bernstein.core.security.vault.resolver import fingerprint, mask_secret
from bernstein.core.security.vault.whoami import WhoamiError, call_whoami


@dataclass(frozen=True)
class ConnectResult:
    """Outcome of a single ``bernstein connect`` invocation."""

    success: bool
    provider_id: str
    account: str
    fingerprint: str
    masked_secret: str
    error: str = ""


@dataclass(frozen=True)
class TestResult:
    """Outcome of a ``bernstein creds test`` invocation."""

    success: bool
    provider_id: str
    account: str
    error: str = ""


@dataclass(frozen=True)
class RevokeResult:
    """Outcome of a ``bernstein creds revoke`` invocation."""

    removed_local: bool
    revoked_remote: bool
    provider_id: str
    error: str = ""


def perform_connect(
    provider: ProviderConfig,
    fields: dict[str, str],
    *,
    vault: CredentialVault,
    http_client: Any | None = None,
) -> ConnectResult:
    """Validate ``fields`` against the provider, store on success, audit.

    Args:
        provider: Provider being connected.
        fields: Form values (must include ``"token"``; Jira also needs
            ``"email"`` and ``"base_url"``).
        vault: Vault to write into.
        http_client: Optional injected :class:`httpx.Client` for tests.

    Returns:
        A :class:`ConnectResult`. The CLI is responsible for printing.
    """
    secret = fields.get("token", "")
    if not secret:
        return ConnectResult(
            success=False,
            provider_id=provider.id,
            account="",
            fingerprint="",
            masked_secret="",
            error="No token supplied.",
        )

    try:
        account = call_whoami(provider, fields, client=http_client)
    except WhoamiError as exc:
        return ConnectResult(
            success=False,
            provider_id=provider.id,
            account="",
            fingerprint=fingerprint(secret),
            masked_secret=mask_secret(secret),
            error=str(exc),
        )

    fp = fingerprint(secret)
    metadata: dict[str, str] = {
        "auth_mode": provider.auth_mode.value,
    }
    # Jira needs the base URL & email at read time; carry them as metadata
    # so the resolver doesn't need to ask the user again.
    for helper in ("base_url", "email"):
        value = fields.get(helper)
        if value:
            metadata[helper] = value

    stored = StoredSecret(
        secret=secret,
        account=account,
        fingerprint=fp,
        created_at=_utc_now(),
        last_used_at=None,
        metadata=metadata,
    )
    vault.put(provider.id, stored)
    audit_event(
        action="connect",
        provider_id=provider.id,
        account=account,
        fingerprint=fp,
        backend=getattr(vault, "backend_id", "unknown"),
        extra={"auth_mode": provider.auth_mode.value},
    )
    return ConnectResult(
        success=True,
        provider_id=provider.id,
        account=account,
        fingerprint=fp,
        masked_secret=mask_secret(secret),
    )


def perform_test(
    provider: ProviderConfig,
    *,
    vault: CredentialVault,
    http_client: Any | None = None,
) -> TestResult:
    """Re-validate the stored credential against the provider's whoami.

    Reads the secret from the vault, posts it to the whoami endpoint, and
    audits the test. Useful when a user suspects the token was rotated or
    revoked elsewhere.
    """
    try:
        stored = vault.get(provider.id)
    except VaultNotFoundError:
        return TestResult(
            success=False,
            provider_id=provider.id,
            account="",
            error=f"No vault entry for {provider.id}; run `bernstein connect {provider.id}` first.",
        )

    fields = _stored_to_fields(stored)
    try:
        account = call_whoami(provider, fields, client=http_client)
    except WhoamiError as exc:
        audit_event(
            action="test",
            provider_id=provider.id,
            account=stored.account,
            fingerprint=stored.fingerprint,
            backend=getattr(vault, "backend_id", "unknown"),
            extra={"result": "failure"},
        )
        return TestResult(
            success=False,
            provider_id=provider.id,
            account=stored.account,
            error=str(exc),
        )

    audit_event(
        action="test",
        provider_id=provider.id,
        account=account,
        fingerprint=stored.fingerprint,
        backend=getattr(vault, "backend_id", "unknown"),
        extra={"result": "success"},
    )
    return TestResult(success=True, provider_id=provider.id, account=account)


def perform_revoke(
    provider: ProviderConfig,
    *,
    vault: CredentialVault,
    http_client: Any | None = None,
) -> RevokeResult:
    """Remove the local entry and call the remote revoke endpoint when present.

    Idempotent: removing a credential that does not exist locally returns
    ``removed_local=False`` without error and still emits an audit entry so
    operators can correlate revoke attempts.
    """
    try:
        stored = vault.get(provider.id)
    except VaultNotFoundError:
        stored = None

    revoked_remote = False
    error_message = ""

    if stored is not None and provider.revoke is not None:
        revoked_remote, error_message = _call_revoke_endpoint(
            provider,
            stored_token=stored.secret,
            http_client=http_client,
        )

    removed_local = False
    if stored is not None:
        removed_local = vault.delete(provider.id)

    audit_event(
        action="revoke",
        provider_id=provider.id,
        account=stored.account if stored else "",
        fingerprint=stored.fingerprint if stored else "",
        backend=getattr(vault, "backend_id", "unknown"),
        extra={
            "remote_revoked": revoked_remote,
            "local_removed": removed_local,
        },
    )
    return RevokeResult(
        removed_local=removed_local,
        revoked_remote=revoked_remote,
        provider_id=provider.id,
        error=error_message,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stored_to_fields(stored: StoredSecret) -> dict[str, str]:
    """Reconstruct the ``fields`` dict the whoami helper expects."""
    fields = {"token": stored.secret}
    if stored.metadata:
        for key in ("base_url", "email"):
            value = stored.metadata.get(key)
            if value:
                fields[key] = value
    if "email" in fields and "token" in fields:
        creds = f"{fields['email']}:{fields['token']}".encode("utf-8")
        fields["basic_b64"] = base64.b64encode(creds).decode("ascii")
    return fields


def _call_revoke_endpoint(
    provider: ProviderConfig,
    *,
    stored_token: str,
    http_client: Any | None,
) -> tuple[bool, str]:
    """POST/DELETE to the provider's revoke URL when one is configured."""
    spec = provider.revoke
    if spec is None:
        return False, ""
    import httpx

    fields = {"token": stored_token}
    url = spec.url_template.format(**fields)
    headers: dict[str, str] = {"Accept": "application/json"}
    if spec.auth_header_template:
        headers["Authorization"] = spec.auth_header_template.format(**fields)

    owns = http_client is None
    cli = http_client if http_client is not None else httpx.Client(timeout=10.0)
    try:
        try:
            resp = cli.request(spec.method, url, headers=headers)
        except httpx.HTTPError as exc:
            return False, f"Network error revoking {provider.display_name}: {exc}"
    finally:
        if owns:
            cli.close()
    if resp.status_code in spec.success_statuses:
        return True, ""
    return False, f"{provider.display_name} revoke failed (HTTP {resp.status_code}): {resp.text[:200]}"


def _utc_now() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _check_auth_mode(provider: ProviderConfig) -> None:  # pragma: no cover - sanity guard
    """Ensure unsupported auth modes are not silently treated as token-paste."""
    if provider.auth_mode not in (
        AuthMode.TOKEN_PASTE,
        AuthMode.OAUTH_DEVICE_CODE,
        AuthMode.OAUTH_PKCE,
    ):
        raise ValueError(f"Unsupported auth mode: {provider.auth_mode}")
