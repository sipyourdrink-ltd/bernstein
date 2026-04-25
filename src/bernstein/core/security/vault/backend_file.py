"""AES-GCM file-backed :class:`CredentialVault` implementation.

This backend is the explicit opt-in fallback for environments without an
OS keychain (containers, headless CI). It encrypts a single JSON blob per
provider and persists everything to ``~/.config/bernstein/vault.enc``.

Encryption details:

* AES-256-GCM via :mod:`cryptography.hazmat.primitives.ciphers.aead`.
* The 32-byte key is derived from a passphrase env-var via PBKDF2-HMAC-SHA256
  with 200,000 iterations and a 16-byte random salt stored in the file
  header (the salt is not secret; it just prevents pre-computation).
* A fresh 12-byte nonce is generated on every write; nonce + ciphertext +
  GCM tag are concatenated and base64-encoded.

File layout (JSON, mode 0600)::

    {
      "version": 1,
      "salt": "<base64>",
      "kdf": "pbkdf2-sha256",
      "kdf_iterations": 200000,
      "ciphertext": "<base64-nonce|cipher|tag>"
    }

The plaintext payload (a dict ``{provider_id: envelope}``) is what gets
encrypted. Loading a record decrypts the entire blob; writes re-encrypt
with a fresh nonce.

The backend refuses to start if the passphrase env-var is unset or empty —
booting with no protection would silently downgrade security versus the
keyring backend.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.security.vault.protocol import (
    CredentialRecord,
    CredentialVault,
    StoredSecret,
    VaultError,
    VaultNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Default vault path. Per the ticket, Linux fallback under
#: ``~/.config/bernstein/vault.enc``; this backend uses the same location on
#: every platform so docs and tests stay simple.
DEFAULT_VAULT_PATH = Path.home() / ".config" / "bernstein" / "vault.enc"

_KDF_ITERATIONS = 200_000
_KDF_NAME = "pbkdf2-sha256"
_KEY_LEN = 32
_SALT_LEN = 16
_NONCE_LEN = 12
_FILE_VERSION = 1


class FileBackendUnavailable(VaultError):
    """Raised when the file backend cannot start (missing passphrase, bad file)."""


@dataclass(frozen=True)
class _FileEnvelope:
    """Cleartext representation of the on-disk file (sans ciphertext)."""

    salt: bytes
    kdf: str
    kdf_iterations: int


class FileBackend(CredentialVault):
    """AES-GCM-encrypted credential vault.

    Args:
        passphrase_env: Name of the environment variable that holds the
            passphrase. The backend refuses to start if this env-var is
            unset or empty — there is no implicit zero-passphrase mode.
        path: Override the on-disk vault location. Defaults to
            :data:`DEFAULT_VAULT_PATH`.
        environ: Optional mapping for tests; defaults to :data:`os.environ`.
    """

    backend_id = "file"

    def __init__(
        self,
        *,
        passphrase_env: str,
        path: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        env = environ if environ is not None else os.environ
        passphrase = env.get(passphrase_env, "")
        if not passphrase:
            raise FileBackendUnavailable(
                f"Vault file backend requires a passphrase in {passphrase_env}; the variable is unset or empty."
            )
        self._passphrase = passphrase.encode("utf-8")
        self._path = path or DEFAULT_VAULT_PATH
        self._passphrase_env = passphrase_env

    # ------------------------------------------------------------------
    # Encryption helpers
    # ------------------------------------------------------------------

    def _derive_key(self, salt: bytes) -> bytes:
        # Imported lazily so tests can monkeypatch / skip when cryptography
        # is not yet installed in CI bootstraps.
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=_KEY_LEN,
            salt=salt,
            iterations=_KDF_ITERATIONS,
        )
        return kdf.derive(self._passphrase)

    def _encrypt(self, plaintext: bytes, salt: bytes) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = self._derive_key(salt)
        aesgcm = AESGCM(key)
        nonce = os.urandom(_NONCE_LEN)
        ct = aesgcm.encrypt(nonce, plaintext, None)
        return base64.b64encode(nonce + ct).decode("ascii")

    def _decrypt(self, blob_b64: str, salt: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            blob = base64.b64decode(blob_b64.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise FileBackendUnavailable(f"Vault ciphertext is not valid base64: {exc}") from exc
        if len(blob) < _NONCE_LEN + 16:
            raise FileBackendUnavailable("Vault ciphertext is too short to decrypt")
        nonce = blob[:_NONCE_LEN]
        ct = blob[_NONCE_LEN:]
        key = self._derive_key(salt)
        aesgcm = AESGCM(key)
        try:
            return aesgcm.decrypt(nonce, ct, None)
        except Exception as exc:
            raise FileBackendUnavailable("Vault decryption failed — passphrase mismatch or file corrupted.") from exc

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _load(self) -> tuple[dict[str, Any], _FileEnvelope]:
        if not self._path.exists():
            return {}, _FileEnvelope(salt=os.urandom(_SALT_LEN), kdf=_KDF_NAME, kdf_iterations=_KDF_ITERATIONS)
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise FileBackendUnavailable(f"Cannot read vault file {self._path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FileBackendUnavailable(f"Vault file is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise FileBackendUnavailable("Vault file root must be a JSON object.")
        try:
            salt_b64 = str(data["salt"])
            ciphertext = str(data.get("ciphertext", ""))
            iterations = int(data.get("kdf_iterations", _KDF_ITERATIONS))
        except (KeyError, TypeError, ValueError) as exc:
            raise FileBackendUnavailable(f"Vault file is missing required fields: {exc}") from exc
        try:
            salt = base64.b64decode(salt_b64.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise FileBackendUnavailable(f"Vault salt is not valid base64: {exc}") from exc
        envelope = _FileEnvelope(salt=salt, kdf=_KDF_NAME, kdf_iterations=iterations)
        if not ciphertext:
            return {}, envelope
        plaintext = self._decrypt(ciphertext, salt)
        try:
            payload = json.loads(plaintext.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FileBackendUnavailable(f"Vault plaintext is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise FileBackendUnavailable("Vault plaintext must encode a JSON object.")
        return cast(dict[str, Any], payload), envelope

    def _save(self, store: dict[str, Any], envelope: _FileEnvelope) -> None:
        plaintext = json.dumps(store, sort_keys=True).encode("utf-8")
        ciphertext = self._encrypt(plaintext, envelope.salt)
        body = {
            "version": _FILE_VERSION,
            "salt": base64.b64encode(envelope.salt).decode("ascii"),
            "kdf": envelope.kdf,
            "kdf_iterations": envelope.kdf_iterations,
            "ciphertext": ciphertext,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Best-effort harden the directory so peer users can't browse it.
        with contextlib.suppress(OSError):
            self._path.parent.chmod(0o700)
        # Write to a temp file and atomic-rename so a crash leaves the old
        # vault intact rather than producing a half-written file.
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(body, sort_keys=True).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp_path, self._path)
        with contextlib.suppress(OSError):
            self._path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600

    # ------------------------------------------------------------------
    # CredentialVault protocol
    # ------------------------------------------------------------------

    def put(self, provider_id: str, secret: StoredSecret) -> None:
        store, envelope = self._load()
        store[provider_id] = {
            "secret": secret.secret,
            "account": secret.account,
            "fingerprint": secret.fingerprint,
            "created_at": secret.created_at,
            "last_used_at": secret.last_used_at,
            "metadata": secret.metadata or {},
        }
        self._save(store, envelope)

    def get(self, provider_id: str) -> StoredSecret:
        store, _envelope = self._load()
        record = store.get(provider_id)
        if not isinstance(record, dict):
            raise VaultNotFoundError(f"No vault entry for provider {provider_id!r}")
        return _record_to_stored(record)

    def delete(self, provider_id: str) -> bool:
        store, envelope = self._load()
        if provider_id not in store:
            return False
        del store[provider_id]
        self._save(store, envelope)
        return True

    def list(self) -> list[CredentialRecord]:
        store, _envelope = self._load()
        out: list[CredentialRecord] = []
        for provider_id, record in store.items():
            if not isinstance(record, dict):
                continue
            stored = _record_to_stored(record)
            out.append(
                CredentialRecord(
                    provider_id=str(provider_id),
                    account=stored.account,
                    fingerprint=stored.fingerprint,
                    created_at=stored.created_at,
                    last_used_at=stored.last_used_at,
                    metadata=stored.metadata,
                )
            )
        return out

    def touch(self, provider_id: str, last_used_at: str) -> None:
        store, envelope = self._load()
        record = store.get(provider_id)
        if not isinstance(record, dict):
            return
        record["last_used_at"] = last_used_at
        self._save(store, envelope)


def _record_to_stored(record: dict[str, Any]) -> StoredSecret:
    metadata = record.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        metadata = None
    return StoredSecret(
        secret=str(record.get("secret", "")),
        account=str(record.get("account", "")),
        fingerprint=str(record.get("fingerprint", "")),
        created_at=str(record.get("created_at", "")),
        last_used_at=(str(record["last_used_at"]) if record.get("last_used_at") else None),
        metadata=cast(dict[str, str], metadata) if metadata else None,
    )
