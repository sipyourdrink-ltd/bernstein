"""Validate a candidate secret against a provider's whoami endpoint.

This is a deliberately small wrapper around :mod:`httpx` so the connect /
test CLI commands and the vault-first resolver share one validation
implementation. Returns the account label on success or raises a typed
error so the CLI can format a useful message.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from bernstein.core.security.vault.providers import ProviderConfig, WhoamiSpec

_TIMEOUT_S = 10.0


class WhoamiError(RuntimeError):
    """Raised when the whoami round-trip fails or rejects the secret."""


def _format_template(template: str, fields: dict[str, str]) -> str:
    """Apply ``str.format(**fields)`` but tolerate unknown placeholders.

    A missing key just leaves the placeholder intact rather than raising —
    handy for the Telegram URL where some fields don't apply.
    """
    try:
        return template.format(**fields)
    except KeyError:
        # Best-effort partial substitution; real fields almost always exist.
        formatted = template
        for key, value in fields.items():
            formatted = formatted.replace("{" + key + "}", value)
        return formatted


def _extract_path(payload: Any, path: tuple[str, ...]) -> str | None:
    """Walk a JSON ``path`` into ``payload``.

    Returns ``None`` when the path doesn't exist or terminates in something
    that isn't a string. Used to pull the account label out of whoami
    responses (``("login",)``, ``("user",)``, etc.).
    """
    cursor: Any = payload
    for key in path:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(key)
        if cursor is None:
            return None
    if isinstance(cursor, str) and cursor.strip():
        return cursor.strip()
    return None


def call_whoami(
    provider: ProviderConfig,
    fields: dict[str, str],
    *,
    client: httpx.Client | None = None,
) -> str:
    """Validate ``fields`` against ``provider.whoami`` and return the account.

    Args:
        provider: Provider whose whoami spec to use.
        fields: Form values supplied during ``connect``. Must include the
            secret token under ``"token"`` plus any helper fields like
            ``"email"``/``"base_url"`` for Jira.
        client: Optional injected :class:`httpx.Client` for tests.

    Returns:
        The account label (login / email / username) on success.

    Raises:
        WhoamiError: On network failure, non-2xx response, or missing
            account field.
    """
    if provider.whoami is None:
        raise WhoamiError(f"Provider {provider.id} has no whoami endpoint configured.")

    spec: WhoamiSpec = provider.whoami
    url = _format_template(spec.url_template, fields)
    headers: dict[str, str] = {"Accept": "application/json"}

    # Jira uses HTTP Basic auth (email + token). Materialise it once so the
    # provider table can stay declarative.
    if "{basic_b64}" in spec.auth_header_template:
        email = fields.get("email", "")
        token = fields.get("token", "")
        creds = f"{email}:{token}".encode()
        fields = {**fields, "basic_b64": base64.b64encode(creds).decode("ascii")}

    if spec.auth_header_template:
        headers["Authorization"] = _format_template(spec.auth_header_template, fields)

    method = "POST" if provider.id == "linear" else "GET"
    payload: dict[str, Any] | None = (
        {"query": "query { viewer { id email name } }"}
        if provider.id == "linear"
        else None
    )

    owns_client = client is None
    cli = client if client is not None else httpx.Client(timeout=_TIMEOUT_S)
    try:
        try:
            if method == "POST":
                resp = cli.post(url, headers=headers, json=payload, timeout=_TIMEOUT_S)
            else:
                resp = cli.get(url, headers=headers, timeout=_TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise WhoamiError(f"Network error calling {provider.display_name}: {exc}") from exc
    finally:
        if owns_client:
            cli.close()

    if resp.status_code != spec.success_status:
        raise WhoamiError(
            f"{provider.display_name} rejected the credential "
            f"(HTTP {resp.status_code}); response: {resp.text[:200]}"
        )

    try:
        body: Any = resp.json()
    except ValueError as exc:
        raise WhoamiError(f"{provider.display_name} returned non-JSON whoami response.") from exc

    # Slack returns 200 even on auth failure; check the ok flag explicitly.
    if provider.id == "slack" and not body.get("ok", False):
        raise WhoamiError(f"Slack rejected the token: {body.get('error', 'unknown error')}")

    account = _extract_path(body, spec.account_field)
    if not account:
        raise WhoamiError(
            f"{provider.display_name} whoami succeeded but did not return an account label "
            f"(looked at {'.'.join(spec.account_field)})."
        )
    return account
