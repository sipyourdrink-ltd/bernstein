"""Install-identity Ed25519 signing key loader.

Extracted from ``bernstein.cli.commands.credential_cmd`` (issue #2526) so
core code -- the orchestrator's live OTel export finalizer -- can sign a
span projection with the same install identity the CLI uses, without a
core -> CLI import. The CLI keeps its ``_load_or_create_install_key`` /
``_signing_key_path`` names as thin wrappers over this module, so the key
location, on-disk format, and permissions are unchanged.
"""

from __future__ import annotations

import os
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

__all__ = [
    "DEFAULT_INSTALL_SIGNING_KEY",
    "INSTALL_SIGNING_KEY_ENV",
    "InstallKeyError",
    "load_or_create_install_key",
    "signing_key_path",
]

#: Env var overriding where the install signing key seed lives.
INSTALL_SIGNING_KEY_ENV = "BERNSTEIN_CREDENTIAL_SIGNING_KEY"

#: Default install signing key location, relative to the project root.
DEFAULT_INSTALL_SIGNING_KEY = ".sdd/runtime/credential/install.key"


class InstallKeyError(RuntimeError):
    """Raised when the install signing key cannot be read or is malformed."""


def signing_key_path(root: Path) -> Path:
    """Return the install signing key path for a project ``root``.

    Honours the ``BERNSTEIN_CREDENTIAL_SIGNING_KEY`` override; defaults to
    ``.sdd/runtime/credential/install.key`` under ``root``.
    """
    from pathlib import Path as _Path

    override = os.environ.get(INSTALL_SIGNING_KEY_ENV)
    if override:
        return _Path(override).expanduser()
    return root / DEFAULT_INSTALL_SIGNING_KEY


def load_or_create_install_key(path: Path) -> Ed25519PrivateKey:
    """Load or generate the install Ed25519 signing key at ``path``.

    Reuses an existing 32-byte seed when present; generates a fresh
    keypair otherwise and persists the raw seed with mode 0600. The same
    key anchors the install identity so every artifact signed with it
    (credential manifests, OTel span projections) shares one attestation
    root.

    Raises:
        InstallKeyError: The key file exists but cannot be read or is not
            exactly 32 raw bytes.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if path.exists():
        try:
            # Read the seed exactly as written: the create path below persists
            # the raw 32-byte private seed with no trailing delimiter, so the
            # bytes on disk ARE the key. Do NOT strip(): a random Ed25519 seed
            # ends (or starts) with an ASCII-whitespace byte (0x09, 0x0a, 0x0b,
            # 0x0c, 0x0d, 0x20) ~4.7% of the time, and strip() would silently
            # drop it, corrupting a valid key into a "not 32 raw bytes" error.
            raw = path.read_bytes()
        except OSError as exc:
            raise InstallKeyError(f"cannot read signing key {path}: {exc}") from exc
        if len(raw) != 32:
            raise InstallKeyError(
                f"install signing key {path} is not 32 raw bytes; refusing to use it",
            )
        return Ed25519PrivateKey.from_private_bytes(raw)

    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.parent.chmod(0o700)
    priv = Ed25519PrivateKey.generate()
    raw_bytes = priv.private_bytes_raw()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, raw_bytes)
    finally:
        os.close(fd)
    path.chmod(0o600)
    return priv
