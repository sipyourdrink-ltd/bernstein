"""Vault-first credential resolution for ticket / chat / PR commands.

Public entry point: :func:`resolve_secret`. Strategy:

1. Look the provider up in the vault. On hit, audit the read and return.
2. Fall back to the legacy environment variables. Emit a one-time
   deprecation warning so users know to migrate.
3. Return :class:`VaultResolution` with ``found=False`` so the caller can
   raise the existing ``TicketAuthError`` / ``UsageError`` at the right
   level instead of inheriting a vault-specific error type.

The resolver also exposes :func:`fingerprint` (SHA-256 → 12 hex chars) and
:func:`mask_secret` so the CLI can format secrets the same way everywhere.
"""

from __future__ import annotations

import hashlib
import logging
import os
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bernstein.core.security.vault.audit import audit_event
from bernstein.core.security.vault.protocol import (
    CredentialVault,
    VaultError,
    VaultNotFoundError,
)
from bernstein.core.security.vault.providers import (
    ProviderConfig,
    require_provider,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VaultResolution:
    """Outcome of a single :func:`resolve_secret` call.

    Attributes:
        found: Whether a usable secret was located.
        source: ``"vault"``, ``"env"``, or ``"missing"``.
        secret: The resolved secret value, or empty string when missing.
        account: Account label associated with the secret. Empty when the
            secret came from an env var (we never had a whoami round-trip).
        provider_id: The provider id the caller asked for.
        env_var_used: When ``source == "env"``, the env-var name we read.
            ``None`` for vault hits.
    """

    found: bool
    source: str
    secret: str
    account: str
    provider_id: str
    env_var_used: str | None = None


def fingerprint(secret: str) -> str:
    """Return a 12-character SHA-256 hex prefix used for safe identification.

    The fingerprint lets ``creds list`` show ``ab12cd34ef56`` instead of the
    secret. SHA-256 is used (rather than e.g. SHA-1) so the fingerprint
    doubles as a duplicate-detection primitive.
    """
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return digest[:12]


def mask_secret(secret: str) -> str:
    """Return a masked rendering of ``secret`` for log lines.

    Always preserves at most the first 4 characters so users can spot a
    typo without exposing the whole secret. Strings shorter than 8 chars
    are fully masked because keeping a 4-char prefix would leak too much.
    """
    if len(secret) < 8:
        return "*" * len(secret)
    return secret[:4] + "*" * (len(secret) - 4)


def resolve_secret(
    provider_id: str,
    *,
    vault: CredentialVault | None,
    environ: Mapping[str, str] | None = None,
    audit: bool = True,
) -> VaultResolution:
    """Locate a secret for ``provider_id`` via the vault then the environment.

    Args:
        provider_id: The provider whose credential to resolve.
        vault: A configured vault, or ``None`` to skip the vault lookup
            (useful for tests and for the deprecation-warning path).
        environ: Optional environment mapping; defaults to :data:`os.environ`.
        audit: When ``True`` (default), every successful resolution writes
            a ``vault.read`` audit event. Set to ``False`` for hot paths
            that resolve the same credential repeatedly.

    Returns:
        A :class:`VaultResolution`. The caller decides how to react when
        ``found`` is ``False`` — the existing CLI commands raise their own
        auth-error types so error messages stay consistent.
    """
    provider = require_provider(provider_id)
    env = environ if environ is not None else os.environ

    if vault is not None:
        try:
            stored = vault.get(provider_id)
        except VaultNotFoundError:
            stored = None
        except VaultError as exc:
            logger.debug(
                "vault: read failed for %s (%s); falling back to env-vars",
                provider_id,
                exc,
            )
            stored = None
        if stored is not None and stored.secret:
            now = _utc_now()
            try:
                vault.touch(provider_id, now)
            except Exception as exc:  # pragma: no cover - depends on backend
                logger.debug("vault: touch failed for %s: %s", provider_id, exc)
            if audit:
                audit_event(
                    action="read",
                    provider_id=provider_id,
                    account=stored.account,
                    fingerprint=stored.fingerprint,
                    backend=getattr(vault, "backend_id", "unknown"),
                )
            return VaultResolution(
                found=True,
                source="vault",
                secret=stored.secret,
                account=stored.account,
                provider_id=provider_id,
            )

    env_value, env_name = _first_env(provider, env)
    if env_value:
        _emit_deprecation_warning(provider, env_name)
        if audit:
            audit_event(
                action="read",
                provider_id=provider_id,
                account="",
                fingerprint=fingerprint(env_value),
                backend="env",
                extra={"env_var": env_name},
            )
        return VaultResolution(
            found=True,
            source="env",
            secret=env_value,
            account="",
            provider_id=provider_id,
            env_var_used=env_name,
        )

    return VaultResolution(
        found=False,
        source="missing",
        secret="",
        account="",
        provider_id=provider_id,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _first_env(provider: ProviderConfig, env: Mapping[str, str]) -> tuple[str, str]:
    """Return ``(value, name)`` for the first non-empty legacy env-var, or ``("", "")``."""
    for name in provider.legacy_env_vars:
        raw = env.get(name)
        if raw:
            return raw, name
    return "", ""


def _utc_now() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# Track which (provider, env_var) pairs we've already warned about within
# the process so chatty paths (a polling chat bridge, a long ticket import)
# don't fill the terminal with duplicate warnings.
_WARNED: set[tuple[str, str]] = set()


def _emit_deprecation_warning(provider: ProviderConfig, env_name: str) -> None:
    key = (provider.id, env_name)
    if key in _WARNED:
        return
    _WARNED.add(key)
    msg = (
        f"Resolving {provider.display_name} credentials via {env_name} is deprecated. "
        f"Run `bernstein connect {provider.id}` to migrate the secret into the OS keychain; "
        f"the env-var fallback will be removed in a future release."
    )
    warnings.warn(msg, DeprecationWarning, stacklevel=2)
    logger.warning("%s", msg)
