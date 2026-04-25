"""Credential vault for Bernstein provider integrations.

The vault is a single-user, single-machine secrets store used by
``bernstein from-ticket``, ``bernstein chat serve``, ``bernstein pr`` and
``bernstein ticket import`` to resolve provider credentials (GitHub PAT,
Linear API key / OAuth token, Jira API token, Slack bot token, Telegram bot
token) without ever round-tripping the secret through the shell or a
plaintext ``.env`` file.

Two backends are provided:

* :class:`KeyringBackend` (default) — delegates to the OS-native keychain
  via the portable ``keyring`` Python package: macOS Keychain on Darwin,
  Secret Service / libsecret on Linux, Credential Manager (DPAPI) on
  Windows.
* :class:`FileBackend` — explicit AES-GCM-encrypted blob fallback for
  containers and headless CI where no Secret Service is available. The
  encryption key is derived from a passphrase stored in an environment
  variable; the backend refuses to start if the env-var is empty.

Both backends speak the :class:`CredentialVault` protocol so callers (the
provider registry, the CLI, the ticket fetchers) never branch on backend
type. Every ``connect`` / ``read`` / ``revoke`` call is HMAC-audited via
:mod:`bernstein.core.security.audit` so ``bernstein audit verify`` keeps a
clean chain across vault operations.
"""

from __future__ import annotations

from bernstein.core.security.vault.audit import audit_event, default_audit_log
from bernstein.core.security.vault.backend_file import FileBackend, FileBackendUnavailable
from bernstein.core.security.vault.backend_keyring import (
    KeyringBackend,
    KeyringUnavailable,
)
from bernstein.core.security.vault.protocol import (
    CredentialRecord,
    CredentialVault,
    StoredSecret,
    VaultError,
    VaultNotFoundError,
)
from bernstein.core.security.vault.providers import (
    Provider,
    ProviderConfig,
    ProviderId,
    list_providers,
    require_provider,
)
from bernstein.core.security.vault.resolver import (
    VaultResolution,
    fingerprint,
    mask_secret,
    resolve_secret,
)

__all__ = [
    "CredentialRecord",
    "CredentialVault",
    "FileBackend",
    "FileBackendUnavailable",
    "KeyringBackend",
    "KeyringUnavailable",
    "Provider",
    "ProviderConfig",
    "ProviderId",
    "StoredSecret",
    "VaultError",
    "VaultNotFoundError",
    "VaultResolution",
    "audit_event",
    "default_audit_log",
    "fingerprint",
    "list_providers",
    "mask_secret",
    "require_provider",
    "resolve_secret",
]
