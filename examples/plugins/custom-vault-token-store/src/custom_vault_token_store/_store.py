"""HashiCorp Vault token-role secret store (worked example).

Worked example of the ``provide_secret_store`` hookspec and the
:class:`ExternalSecretStore` contract. Wires the broker to Vault's own
token-role machinery so every ``mint_credential`` call mints a genuinely
new, Vault-issued, short-lived token rather than handing back a static
KV value with no real expiry.

Naming: ``path`` is a Vault token role name, referenced as ``vault:<path>``
(for example ``vault:bernstein-agent``) via ``SecretRef``. The store
prefixes the Vault API path itself, so the reference is just the role
name, never ``vault:auth/token/roles/<path>``. The expansion differs per
call: ``resolve``/``report_revocation``'s role branch read
``auth/token/roles/<path>``, ``mint_credential`` posts to
``auth/token/create/<path>``. The role must already exist in Vault
(``vault write auth/token/roles/<path> ...``); this plugin only mints
against it, it does not manage role configuration.

The HTTP transport is a small injectable seam (:class:`VaultTransport`)
so unit tests can run without a live Vault. The default transport uses
only :mod:`urllib.request`, no vendor SDK, per the contract's own
"no vendor SDKs in core" rule, extended here to keep this example
dependency-free too.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from bernstein.core.security.external_secret_store import (
    ExternalCredential,
    ExternalSecretStore,
    ExternalStoreError,
    SecretDescriptor,
)

logger = logging.getLogger(__name__)


class VaultTransport(Protocol):
    """One HTTP call against a Vault API path.

    Split out so tests substitute a fake transport instead of needing a
    live Vault server. ``method`` is ``"GET"`` or ``"POST"``; ``body``
    (when given) is serialised as JSON. Returns the parsed JSON response
    body, or ``None`` on a 204. Raises :class:`ExternalStoreError` on any
    non-2xx response or transport failure.
    """

    def __call__(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any] | None: ...


class VaultHttpError(ExternalStoreError):
    """``ExternalStoreError`` that keeps the HTTP status code when Vault gave one.

    The store needs to tell "the accessor or role is gone" (a 400/404 from
    Vault) apart from "this caller's token cannot see it" (403) or "Vault is
    down" (no status). The status code is the only reliable discriminator.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class VaultHttpTransport:
    """Default transport: plain ``urllib`` calls against a Vault server.

    Attributes:
        addr: Vault base URL, e.g. ``"http://127.0.0.1:8200"``.
        token: Vault token used to authenticate every call
            (``X-Vault-Token``). This plugin never logs it.
        timeout_seconds: Per-request socket timeout.
    """

    addr: str
    token: str = field(repr=False)
    timeout_seconds: float = 5.0

    def __call__(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
        url = f"{self.addr.rstrip('/')}/v1/{path.lstrip('/')}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"X-Vault-Token": self.token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise VaultHttpError(f"vault {method} {path} -> HTTP {exc.code}: {detail}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise VaultHttpError(f"vault {method} {path} unreachable: {exc.reason}") from exc
        except TimeoutError as exc:
            raise VaultHttpError(f"vault {method} {path} timed out after {self.timeout_seconds}s") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VaultHttpError(f"vault {method} {path} returned a non-JSON body") from exc


class VaultTokenRoleStore(ExternalSecretStore):
    """:class:`ExternalSecretStore` backed by a Vault token role.

    ``path`` names a Vault token role (``auth/token/roles/<path>``).
    ``resolve`` checks the role exists. ``mint_credential`` calls
    ``auth/token/create/<path>`` so every mint is a fresh Vault-issued
    token with its own accessor and lease. ``report_revocation`` looks
    the accessor up; a missing accessor means the token is gone, which
    this store reports as revoked so the broker stops treating it as
    live.
    """

    store_id = "vault"

    def __init__(self, *, transport: VaultTransport) -> None:
        self._transport = transport

    def resolve(self, path: str) -> SecretDescriptor:
        try:
            role = self._transport("GET", f"auth/token/roles/{path}")
        except VaultHttpError as exc:
            if exc.status == 404:
                raise ExternalStoreError(f"vault token role {path!r} not found") from exc
            raise
        if role is None:
            raise ExternalStoreError(f"vault token role {path!r} not found")
        data = role.get("data", {})
        max_ttl = int(data.get("token_explicit_max_ttl") or data.get("explicit_max_ttl") or 0)
        return SecretDescriptor(
            store_id=self.store_id,
            upstream_id=str(data.get("name", path)),
            revoked=False,
            expires_at=0.0 if max_ttl == 0 else time.time() + max_ttl,
        )

    def mint_credential(self, path: str, *, audience: str, ttl_seconds: int) -> ExternalCredential:
        body = {"ttl": f"{ttl_seconds}s"}
        if audience:
            body["display_name"] = _sanitize_display_name(audience)
        response = self._transport("POST", f"auth/token/create/{path}", body)
        if response is None:
            raise ExternalStoreError(f"vault token create/{path} returned no body")
        auth = response.get("auth")
        if not auth or not auth.get("client_token"):
            raise ExternalStoreError(f"vault token create/{path} returned no auth block")
        lease_duration = int(auth.get("lease_duration") or 0)
        capped_ttl = min(ttl_seconds, lease_duration) if lease_duration else ttl_seconds
        return ExternalCredential(
            value=auth["client_token"],
            expires_at=time.time() + capped_ttl,
            upstream_id=str(auth.get("accessor", "")),
            audience=audience,
        )

    def report_revocation(self, path: str, *, upstream_id: str) -> bool:
        if not upstream_id:
            return True
        # The broker passes what resolve() returned. For this store that is
        # the role name, not an accessor, so "is the role still usable" is
        # the correct question. An accessor (from a minted credential) gets
        # the lookup-accessor path instead.
        if upstream_id == path:
            return self._role_revoked(path)
        return self._accessor_revoked(upstream_id)

    def _role_revoked(self, path: str) -> bool:
        try:
            role = self._transport("GET", f"auth/token/roles/{path}")
        except VaultHttpError as exc:
            if exc.status == 404:
                return True
            raise
        return role is None

    def _accessor_revoked(self, accessor: str) -> bool:
        try:
            lookup = self._transport(
                "POST",
                "auth/token/lookup-accessor",
                {"accessor": accessor},
            )
        except VaultHttpError as exc:
            # Vault answers "invalid accessor" for an accessor it no longer
            # knows; that is the revoked case this method reports. Everything
            # else (403, 5xx, unreachable) is a caller or transport failure
            # and must not be flattened into "revoked".
            if exc.status == 400 and "invalid accessor" in str(exc):
                logger.debug("vault lookup-accessor %s is gone: %s", accessor, exc)
                return True
            raise
        if lookup is None:
            return True
        ttl_remaining = lookup.get("data", {}).get("ttl", 0)
        return int(ttl_remaining or 0) <= 0


def _sanitize_display_name(audience: str) -> str:
    """Vault display names allow only ``[a-zA-Z0-9-_.]``; map anything else."""
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in audience)[:64]
