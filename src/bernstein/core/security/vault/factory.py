"""Factory for picking the right :class:`CredentialVault` backend at runtime.

The CLI commands accept ``--backend keyring`` (default) or ``--backend file
--passphrase-env VAR`` and call :func:`open_vault`. The same factory is
used by the resolver path inside ``from-ticket`` / ``chat`` / ``pr`` so a
user who opted into the file backend keeps that backend across read calls
in the same process.

The default backend can also be selected via the
``BERNSTEIN_VAULT_BACKEND`` environment variable (``keyring`` or ``file``)
plus ``BERNSTEIN_VAULT_PASSPHRASE_ENV`` for the file backend's passphrase
env-var name. This makes container deployments scriptable without
plumbing CLI flags through every entry point.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Literal

from bernstein.core.security.vault.backend_file import (
    DEFAULT_VAULT_PATH,
    FileBackend,
    FileBackendUnavailable,
)
from bernstein.core.security.vault.backend_keyring import (
    KeyringBackend,
    KeyringUnavailable,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.vault.protocol import CredentialVault

logger = logging.getLogger(__name__)

BackendChoice = Literal["keyring", "file"]

#: Env var that picks the default backend when the CLI flag is not given.
ENV_BACKEND = "BERNSTEIN_VAULT_BACKEND"
#: Env var that names the passphrase env-var for the file backend.
ENV_PASSPHRASE_ENV = "BERNSTEIN_VAULT_PASSPHRASE_ENV"


def open_vault(
    *,
    backend: BackendChoice | None = None,
    passphrase_env: str | None = None,
    file_path: Path | None = None,
) -> CredentialVault:
    """Return a configured :class:`CredentialVault`.

    Args:
        backend: Explicit backend choice. ``None`` consults ``$BERNSTEIN_VAULT_BACKEND``,
            falling back to ``"keyring"``.
        passphrase_env: Name of the env-var the file backend reads for its
            passphrase. Required when ``backend == "file"``.
        file_path: Override the file vault path; defaults to
            :data:`bernstein.core.security.vault.backend_file.DEFAULT_VAULT_PATH`.

    Raises:
        FileBackendUnavailable: When the file backend is requested without
            a usable passphrase env-var.
        KeyringUnavailable: When the keyring backend is requested but the
            ``keyring`` package or its OS backend cannot be reached.
    """
    chosen = backend or os.environ.get(ENV_BACKEND, "keyring")
    if chosen not in ("keyring", "file"):
        raise ValueError(f"Unknown vault backend {chosen!r}; expected 'keyring' or 'file'.")

    if chosen == "file":
        env_name = passphrase_env or os.environ.get(ENV_PASSPHRASE_ENV, "")
        if not env_name:
            raise FileBackendUnavailable(
                "File backend requires --passphrase-env or $BERNSTEIN_VAULT_PASSPHRASE_ENV.",
            )
        return FileBackend(passphrase_env=env_name, path=file_path or DEFAULT_VAULT_PATH)

    return KeyringBackend()


def open_vault_silent() -> CredentialVault | None:
    """Open the default vault, returning ``None`` if no backend is reachable.

    The resolver uses this so a missing keyring backend on a headless box
    falls through to the env-var path with a deprecation warning rather
    than crashing the read.
    """
    try:
        return open_vault()
    except (KeyringUnavailable, FileBackendUnavailable) as exc:
        logger.debug("vault: no backend available (%s); falling back to env-vars", exc)
        return None
