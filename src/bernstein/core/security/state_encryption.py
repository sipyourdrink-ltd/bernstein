"""AES-256-GCM encryption for .sdd/ state files at rest.

Provides transparent file-level encryption for sensitive Bernstein state
(tasks.jsonl, traces, audit logs) using AES-256-GCM via the ``cryptography``
library.  Encrypted files use a ``.enc`` suffix and can be read/written
transparently through the ``EncryptedFile`` context manager.

File format (binary)::

    +--------+----------+-----------+------------+----------+
    | HEADER | IV (12B) | AAD (var) | CIPHERTEXT | TAG(16B) |
    +--------+----------+-----------+------------+----------+

HEADER = ``b"BSD1"`` (4 bytes — Bernstein State Data, version 1)

Usage::

    # Write encrypted
    with EncryptedFile(path, key, mode="wb") as ef:
        ef.write(b"plaintext data")

    # Read decrypted
    with EncryptedFile(path, key, mode="rb") as ef:
        data = ef.read()
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)

HEADER_MAGIC = b"BSD1"  # Bernstein State Data, version 1
HEADER_LEN = len(HEADER_MAGIC)
IV_LEN = 12  # 96-bit IV recommended for GCM
TAG_LEN = 16  # 128-bit authentication tag


def generate_key() -> bytes:
    """Generate a random 256-bit AES key.

    Returns:
        32 random bytes suitable for AES-256-GCM.
    """
    import secrets

    # Generate 32 random bytes directly
    return secrets.token_bytes(32)


def derive_key(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    """Derive an AES-256 key from a password using PBKDF2.

    Args:
        password: Human-readable password.
        salt: Random salt (generated if None).

    Returns:
        Tuple of (key, salt).
    """

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    if salt is None:
        import secrets

        salt = secrets.token_bytes(16)

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,  # OWASP 2023 recommendation
    )
    key = kdf.derive(password.encode("utf-8"))
    return key, salt


class EncryptedFile:
    """File-like wrapper that encrypts/decrypts data transparently.

    Supports binary read/write modes only (``"rb"`` and ``"wb"``).

    Args:
        path: Path to the encrypted file (will have ``.enc`` suffix appended).
        key: 32-byte AES-256 key.
        mode: File mode (``"rb"`` or ``"wb"``).

    Raises:
        ValueError: If the key is not 32 bytes or mode is unsupported.
    """

    def __init__(self, path: Path | str, key: bytes, mode: str = "rb") -> None:
        self._path = Path(path)
        if not str(self._path).endswith(".enc"):
            self._path = self._path.with_suffix(self._path.suffix + ".enc")
        if len(key) != 32:
            raise ValueError(f"Key must be 32 bytes, got {len(key)}")
        if mode not in ("rb", "wb", "ab"):
            raise ValueError(f"Mode must be 'rb', 'wb', or 'ab', got '{mode}'")

        self._key = key
        self._mode = mode
        self._fh: BinaryIO | None = None
        self._write_buf = bytearray()

    # -- Context manager ----------------------------------------------------

    def __enter__(self) -> EncryptedFile:
        if self._mode == "rb":
            self._fh = open(self._path, "rb")
        elif self._mode in ("wb", "ab"):
            # Read existing file content if in append mode
            if self._mode == "ab" and self._path.exists():
                existing = self._path.read_bytes()
                if self._starts_with_header(existing):
                    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

                    iv = existing[HEADER_LEN : HEADER_LEN + IV_LEN]
                    aad = self._path.name.encode("utf-8")
                    auth_tag = existing[-TAG_LEN:]
                    ciphertext = existing[HEADER_LEN + IV_LEN + len(aad) : -TAG_LEN]
                    aesgcm = AESGCM(self._key)
                    with contextlib.suppress(Exception):
                        self._write_buf = bytearray(aesgcm.decrypt(iv, ciphertext + auth_tag, aad))
            self._fh = open(self._path, "wb")
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # -- Write operations ---------------------------------------------------

    def write(self, data: bytes) -> int:
        """Encrypt and write *data* to file.

        Args:
            data: Plaintext bytes to encrypt and write.

        Returns:
            Number of plaintext bytes written.
        """
        if self._mode not in ("wb", "ab"):
            raise OSError("write() called on non-writable file")
        self._write_buf.extend(data)
        return len(data)

    def writelines(self, lines: list[bytes]) -> None:
        """Encrypt and write multiple lines.

        Args:
            lines: List of plaintext byte strings.
        """
        for line in lines:
            self.write(line)

    # -- Read operations ----------------------------------------------------

    def read(self, _size: int = -1) -> bytes:
        """Read and decrypt file contents.

        Args:
            size: Max bytes to read (-1 for all).

        Returns:
            Decrypted plaintext bytes.
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if self._mode != "rb":
            raise OSError("read() called on non-readable file")
        if self._fh is None:
            raise OSError("file not opened")

        file_data = self._fh.read()
        if not file_data:
            return b""

        if not self._starts_with_header(file_data):
            raise ValueError("Invalid file header — not an encrypted file")

        iv = file_data[HEADER_LEN : HEADER_LEN + IV_LEN]
        aad = self._path.name.encode("utf-8")
        auth_tag = file_data[-TAG_LEN:]
        ciphertext = file_data[HEADER_LEN + IV_LEN + len(aad) : -TAG_LEN]

        aesgcm = AESGCM(self._key)
        try:
            plaintext = aesgcm.decrypt(iv, ciphertext + auth_tag, aad)
        except Exception as exc:
            raise ValueError("Decryption failed — file may be corrupted") from exc
        return plaintext

    def readlines(self) -> list[bytes]:
        """Read and decrypt all lines.

        Returns:
            List of decrypted byte lines.
        """
        data = self.read()
        return data.split(b"\n") if data else []

    # -- File operations ----------------------------------------------------

    def close(self) -> None:
        """Flush any buffered writes and close the file."""
        if self._fh is not None:
            # Encrypt and write buffered data
            if self._write_buf:
                import os

                from cryptography.hazmat.primitives.ciphers.aead import AESGCM

                iv = os.urandom(IV_LEN)
                aad = self._path.name.encode("utf-8")
                aesgcm = AESGCM(self._key)
                ciphertext = aesgcm.encrypt(iv, bytes(self._write_buf), aad)
                self._fh.write(HEADER_MAGIC)
                self._fh.write(iv)
                self._fh.write(aad)
                self._fh.write(ciphertext)
                self._write_buf.clear()
            self._fh.close()
            self._fh = None

    def flush(self) -> None:
        """Flush the underlying file handle."""
        if self._fh is not None:
            self._fh.flush()

    # -- Internal helpers ---------------------------------------------------

    @staticmethod
    def _starts_with_header(data: bytes) -> bool:
        return data[:HEADER_LEN] == HEADER_MAGIC

    @property
    def encrypted_path(self) -> Path:
        """Return the path to the encrypted file."""
        return self._path

    @property
    def original_path(self) -> Path:
        """Return the original (unencrypted) file path."""
        p = str(self._path)
        if p.endswith(".enc"):
            return Path(p[:-4])
        return self._path


def encrypt_file(path: Path, key: bytes, *, remove_original: bool = True) -> Path:
    """Encrypt a single file in-place.

    Args:
        path: Path to the plaintext file.
        key: 32-byte AES-256 key.
        remove_original: Delete the original after encrypting.

    Returns:
        Path to the encrypted file (``.enc`` suffix).
    """
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    plaintext = path.read_bytes()
    enc = EncryptedFile(path, key, mode="wb")
    with enc:
        enc.write(plaintext)

    if remove_original:
        path.unlink(missing_ok=True)

    return enc.encrypted_path


def decrypt_file(path: Path, key: bytes, *, remove_encrypted: bool = True) -> Path:
    """Decrypt a single file in-place.

    Args:
        path: Path to the encrypted file (``.enc`` suffix).
        key: 32-byte AES-256 key.
        remove_encrypted: Delete the encrypted file after decrypting.

    Returns:
        Path to the decrypted file.
    """
    enc_path = Path(str(path) + ".enc") if not str(path).endswith(".enc") else path
    if not enc_path.exists():
        raise FileNotFoundError(f"Encrypted file not found: {enc_path}")

    enc = EncryptedFile(enc_path, key, mode="rb")
    with enc:
        plaintext = enc.read()

    original_path = enc.original_path
    original_path.write_bytes(plaintext)

    if remove_encrypted:
        enc_path.unlink(missing_ok=True)

    return original_path


def is_encrypted(path: Path) -> bool:
    """Check whether a file appears to be encrypted.

    Args:
        path: File path to check.

    Returns:
        True if the file has the ``.enc`` suffix and valid header.
    """
    if not str(path).endswith(".enc"):
        return False
    if not path.exists():
        return False
    header = path.read_bytes()[:HEADER_LEN]
    return header == HEADER_MAGIC


_KEY_FILE_MAGIC = b"BSK1"  # Bernstein State Key, version 1 (wrapped format)
_KEY_SALT_LEN = 16
_KEY_IV_LEN = 12
_KEY_TAG_LEN = 16


class KeyManager:
    """Manages encryption keys stored on disk, outside the state directory.

    By default the key lives at
    ``~/.config/bernstein/keys/<workspace-hash>`` — one key per workspace,
    keyed by the SHA-256 of the ``.sdd/`` absolute path. This prevents a
    stolen ``.sdd/`` tarball from containing both ciphertext and key.

    Environment variables
    ---------------------
    ``BERNSTEIN_STATE_KEY_PATH``
        Overrides the computed default key path entirely (useful for
        tests, shared keys, or mounted KMS volumes).

    ``BERNSTEIN_STATE_KEY_PASSPHRASE``
        When set, the on-disk key is wrapped (AES-256-GCM) by a KEK
        derived from this passphrase via PBKDF2-HMAC-SHA256 (600k
        iterations). A random salt is stored alongside the wrapped key,
        so rotating the passphrase requires rotating the salt/wrap.

    Migration
    ---------
    On first instantiation, any legacy key at
    ``<sdd>/config/state-key`` is copied to the new out-of-tree
    location (only if the new path does not already exist) and then
    deleted so future tarballs of ``.sdd/`` do not contain it.

    Args:
        sdd_dir: Path to the .sdd/ directory. Used to derive the
            workspace-hash and to locate any legacy in-tree key for
            migration.
    """

    def __init__(self, sdd_dir: Path) -> None:
        self._sdd_dir = sdd_dir
        self._legacy_key_dir = sdd_dir / "config"
        self._legacy_key_path = self._legacy_key_dir / "state-key"
        self._key_path_override = os.environ.get("BERNSTEIN_STATE_KEY_PATH")
        # Perform migration lazily on first key access — avoid touching
        # disk in __init__ so tests that construct the manager without
        # calling ensure_key()/load_key() stay hermetic.
        self._migrated = False

    # -- Path computation ---------------------------------------------------

    @staticmethod
    def _workspace_hash(sdd_dir: Path) -> str:
        """Return a short deterministic hash of the .sdd/ absolute path."""
        import hashlib

        abs_path = str(sdd_dir.resolve())
        digest = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()
        return digest[:16]

    @property
    def _key_path(self) -> Path:
        if self._key_path_override:
            return Path(self._key_path_override).expanduser()
        base = Path.home() / ".config" / "bernstein" / "keys"
        return base / self._workspace_hash(self._sdd_dir)

    @property
    def _key_dir(self) -> Path:
        return self._key_path.parent

    # -- Wrapping helpers ---------------------------------------------------

    @staticmethod
    def _passphrase() -> str | None:
        return os.environ.get("BERNSTEIN_STATE_KEY_PASSPHRASE")

    @classmethod
    def _wrap_key(cls, raw_key: bytes, passphrase: str) -> bytes:
        """Wrap *raw_key* with a KEK derived from *passphrase*.

        Returns bytes suitable for writing to disk: ``BSK1 || salt(16) ||
        iv(12) || ciphertext || tag(16)``.
        """
        import secrets

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        salt = secrets.token_bytes(_KEY_SALT_LEN)
        kek, _ = derive_key(passphrase, salt=salt)
        iv = secrets.token_bytes(_KEY_IV_LEN)
        aesgcm = AESGCM(kek)
        wrapped = aesgcm.encrypt(iv, raw_key, _KEY_FILE_MAGIC)
        return _KEY_FILE_MAGIC + salt + iv + wrapped

    @classmethod
    def _unwrap_key(cls, blob: bytes, passphrase: str) -> bytes:
        """Unwrap a passphrase-wrapped key blob. Raises ValueError on failure."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        magic_len = len(_KEY_FILE_MAGIC)
        header_len = magic_len + _KEY_SALT_LEN + _KEY_IV_LEN
        if len(blob) <= header_len + _KEY_TAG_LEN or blob[:magic_len] != _KEY_FILE_MAGIC:
            raise ValueError("Invalid wrapped-key blob")
        salt = blob[magic_len : magic_len + _KEY_SALT_LEN]
        iv = blob[magic_len + _KEY_SALT_LEN : header_len]
        ciphertext = blob[header_len:]
        kek, _ = derive_key(passphrase, salt=salt)
        aesgcm = AESGCM(kek)
        try:
            return aesgcm.decrypt(iv, ciphertext, _KEY_FILE_MAGIC)
        except Exception as exc:  # pragma: no cover - exercised via tests
            raise ValueError("Failed to unwrap state key — wrong passphrase?") from exc

    @staticmethod
    def _is_wrapped(blob: bytes) -> bool:
        return blob[: len(_KEY_FILE_MAGIC)] == _KEY_FILE_MAGIC

    # -- Migration ----------------------------------------------------------

    def _migrate_legacy_key_if_needed(self) -> None:
        """Move legacy ``<sdd>/config/state-key`` out of .sdd/ (once).

        Runs for both the default path and any ``BERNSTEIN_STATE_KEY_PATH``
        override — the override only changes the destination, not whether
        migration happens.
        """
        if self._migrated:
            return
        self._migrated = True
        if not self._legacy_key_path.exists():
            return
        if self._key_path.exists():
            # Never clobber an existing out-of-tree key; just drop the
            # legacy copy so .sdd/ stops carrying secrets.
            with contextlib.suppress(OSError):
                self._legacy_key_path.unlink()
            return
        legacy_bytes = self._legacy_key_path.read_bytes()
        self._key_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, legacy_bytes)
        finally:
            os.close(fd)
        with contextlib.suppress(OSError):
            self._legacy_key_path.unlink()
        logger.info(
            "Migrated state encryption key from %s to %s",
            self._legacy_key_path,
            self._key_path,
        )

    # -- Public API ---------------------------------------------------------

    def ensure_key(self) -> bytes:
        """Return the existing key, or generate and store a new one.

        Returns:
            32-byte AES-256 key.
        """
        self._migrate_legacy_key_if_needed()
        existing = self.load_key()
        if existing is not None:
            return existing

        self._key_dir.mkdir(parents=True, exist_ok=True)
        key = generate_key()
        passphrase = self._passphrase()
        blob = self._wrap_key(key, passphrase) if passphrase else key
        fd = os.open(str(self._key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)
        logger.info("Generated new state encryption key at %s", self._key_path)
        return key

    def load_key(self) -> bytes | None:
        """Load the key from disk, if it exists.

        Transparently unwraps passphrase-wrapped blobs when
        ``BERNSTEIN_STATE_KEY_PASSPHRASE`` is set.

        Returns:
            32-byte AES-256 key, or None if not found.
        """
        self._migrate_legacy_key_if_needed()
        if not self._key_path.exists():
            return None
        blob = self._key_path.read_bytes()
        if self._is_wrapped(blob):
            passphrase = self._passphrase()
            if not passphrase:
                raise ValueError("State key is wrapped but BERNSTEIN_STATE_KEY_PASSPHRASE is not set")
            return self._unwrap_key(blob, passphrase)
        return blob

    def delete_key(self) -> None:
        """Delete the key file. Data encrypted with this key will be unrecoverable."""
        self._migrate_legacy_key_if_needed()
        self._key_path.unlink(missing_ok=True)
