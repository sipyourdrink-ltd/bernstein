"""Tests for state_encryption — AES-256-GCM file encryption at rest."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from bernstein.core.state_encryption import (
    EncryptedFile,
    KeyManager,
    decrypt_file,
    encrypt_file,
    generate_key,
    is_encrypted,
)


@pytest.fixture(autouse=True)
def _pin_key_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep KeyManager off the real ~/.config during tests.

    Each test gets its own override path; any test that wants to
    exercise the default (``~/.config/bernstein/keys/<hash>``) path can
    delete the env var explicitly.
    """
    monkeypatch.delenv("BERNSTEIN_STATE_KEY_PASSPHRASE", raising=False)
    monkeypatch.setenv("BERNSTEIN_STATE_KEY_PATH", str(tmp_path / "state-key-override"))


class TestGenerateKey:
    def test_returns_32_bytes(self) -> None:
        key = generate_key()
        assert len(key) == 32

    def test_keys_are_unique(self) -> None:
        k1 = generate_key()
        k2 = generate_key()
        assert k1 != k2


class TestEncryptedFileWriteThenRead:
    def test_roundtrip_small_data(self, tmp_path: Path) -> None:
        key = generate_key()
        path = tmp_path / "data.bin"
        plaintext = b"hello world"

        ef = EncryptedFile(path, key, mode="wb")
        with ef:
            ef.write(plaintext)

        assert ef.encrypted_path.exists()
        assert is_encrypted(ef.encrypted_path)

        ef2 = EncryptedFile(path, key, mode="rb")
        with ef2:
            result = ef2.read()
        assert result == plaintext

    def test_roundtrip_empty_data(self, tmp_path: Path) -> None:
        key = generate_key()
        ef = EncryptedFile(tmp_path / "empty.bin", key, mode="wb")
        with ef:
            ef.write(b"")
        ef2 = EncryptedFile(tmp_path / "empty.bin", key, mode="rb")
        with ef2:
            assert ef2.read() == b""

    def test_roundtrip_large_data(self, tmp_path: Path) -> None:
        key = generate_key()
        path = tmp_path / "large.bin"
        plaintext = b"x" * (1024 * 1024)  # 1 MB

        ef = EncryptedFile(path, key, mode="wb")
        with ef:
            ef.write(plaintext)

        ef2 = EncryptedFile(path, key, mode="rb")
        with ef2:
            result = ef2.read()
        assert result == plaintext

    def test_wrong_key_fails(self, tmp_path: Path) -> None:
        key1 = generate_key()
        key2 = generate_key()
        path = tmp_path / "secret.bin"

        ef = EncryptedFile(path, key1, mode="wb")
        with ef:
            ef.write(b"secret")

        ef2 = EncryptedFile(path, key2, mode="rb")
        with ef2:
            with pytest.raises(ValueError, match="Decryption failed"):
                ef2.read()

    def test_invalid_key_length(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Key must be 32 bytes"):
            EncryptedFile(tmp_path / "x.bin", b"short", mode="rb")

    def test_invalid_mode(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Mode must be"):
            EncryptedFile(tmp_path / "x.bin", generate_key(), mode="r")

    def test_writelines(self, tmp_path: Path) -> None:
        key = generate_key()
        lines = [b"line one\n", b"line two\n", b"line three\n"]
        ef = EncryptedFile(tmp_path / "lines.bin", key, mode="wb")
        with ef:
            ef.writelines(lines)

        ef2 = EncryptedFile(tmp_path / "lines.bin", key, mode="rb")
        with ef2:
            data = ef2.read()
        assert data == b"".join(lines)

    def test_readlines(self, tmp_path: Path) -> None:
        key = generate_key()
        ef = EncryptedFile(tmp_path / "lines.bin", key, mode="wb")
        with ef:
            ef.write(b"alpha\nbeta\ngamma\n")

        ef2 = EncryptedFile(tmp_path / "lines.bin", key, mode="rb")
        with ef2:
            lines = ef2.readlines()
        assert lines == [b"alpha", b"beta", b"gamma", b""]


class TestEncryptDecryptFile:
    def test_encrypt_then_decrypt(self, tmp_path: Path) -> None:
        key = generate_key()
        path = tmp_path / "task.jsonl"
        path.write_text('{"id":"T1"}\n', encoding="utf-8")

        enc_path = encrypt_file(path, key, remove_original=True)
        assert enc_path.exists()
        assert is_encrypted(enc_path)
        assert not path.exists()

        dec_path = decrypt_file(enc_path, key, remove_encrypted=True)
        assert dec_path.exists()
        assert dec_path.read_text(encoding="utf-8") == '{"id":"T1"}\n'

    def test_encrypt_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            encrypt_file(tmp_path / "nope.jsonl", generate_key())


class TestKeyManager:
    def test_ensure_key_creates_and_returns_32_bytes(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        key_path = tmp_path / "state-key-override"
        km = KeyManager(sdd)
        key = km.ensure_key()
        assert len(key) == 32
        assert key_path.exists()
        # Key must NOT live inside the .sdd/ tarball.
        assert not (sdd / "config" / "state-key").exists()

    def test_load_key_returns_existing(self, tmp_path: Path) -> None:
        km = KeyManager(tmp_path / ".sdd")
        k1 = km.ensure_key()
        k2 = km.load_key()
        assert k1 == k2

    def test_load_key_returns_none_if_missing(self, tmp_path: Path) -> None:
        km = KeyManager(tmp_path / ".sdd")
        assert km.load_key() is None

    def test_delete_key_removes_file(self, tmp_path: Path) -> None:
        key_path = tmp_path / "state-key-override"
        km = KeyManager(tmp_path / ".sdd")
        km.ensure_key()
        km.delete_key()
        assert not key_path.exists()

    def test_key_file_has_restricted_permissions(self, tmp_path: Path) -> None:
        key_path = tmp_path / "state-key-override"
        km = KeyManager(tmp_path / ".sdd")
        km.ensure_key()
        mode = os.stat(key_path).st_mode & 0o777
        assert mode == 0o600


class TestKeyManagerDefaultPath:
    def test_default_path_is_outside_sdd_and_uses_workspace_hash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Point $HOME at tmp_path so the default ~/.config resolves here,
        # and clear the override so the real default kicks in.
        monkeypatch.delenv("BERNSTEIN_STATE_KEY_PATH", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        sdd = tmp_path / "workspace" / ".sdd"
        sdd.mkdir(parents=True)

        km = KeyManager(sdd)
        key = km.ensure_key()
        assert len(key) == 32

        keys_dir = tmp_path / ".config" / "bernstein" / "keys"
        assert keys_dir.exists()
        key_files = list(keys_dir.iterdir())
        assert len(key_files) == 1
        # 16-hex-char workspace hash, not the literal "state-key" name.
        assert len(key_files[0].name) == 16
        # Must not be inside .sdd/.
        assert ".sdd" not in str(key_files[0])


class TestKeyManagerMigration:
    def test_migrates_legacy_key_and_deletes_old(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        legacy_key = b"x" * 32
        sdd = tmp_path / ".sdd"
        legacy_path = sdd / "config" / "state-key"
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_bytes(legacy_key)

        new_path = tmp_path / "state-key-override"
        assert not new_path.exists()

        km = KeyManager(sdd)
        loaded = km.load_key()

        assert loaded == legacy_key
        assert new_path.exists()
        assert new_path.read_bytes() == legacy_key
        # Old in-tree copy is gone so it can't leak via tarballs.
        assert not legacy_path.exists()

    def test_migration_preserves_existing_new_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # New key already exists — legacy key should be discarded, not
        # clobber the new one.
        new_path = tmp_path / "state-key-override"
        new_path.write_bytes(b"n" * 32)

        sdd = tmp_path / ".sdd"
        legacy_path = sdd / "config" / "state-key"
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_bytes(b"l" * 32)

        km = KeyManager(sdd)
        loaded = km.load_key()

        assert loaded == b"n" * 32
        assert not legacy_path.exists()


class TestKeyManagerPassphraseWrapping:
    def test_wraps_key_when_passphrase_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_STATE_KEY_PASSPHRASE", "hunter2")
        key_path = tmp_path / "state-key-override"
        km = KeyManager(tmp_path / ".sdd")
        key = km.ensure_key()

        raw = key_path.read_bytes()
        # On-disk blob must not equal the raw key (it's wrapped).
        assert raw != key
        assert raw[:4] == b"BSK1"
        assert len(raw) > 32

        # Same passphrase round-trips.
        km2 = KeyManager(tmp_path / ".sdd")
        assert km2.load_key() == key

    def test_wrong_passphrase_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_STATE_KEY_PASSPHRASE", "hunter2")
        km = KeyManager(tmp_path / ".sdd")
        km.ensure_key()

        monkeypatch.setenv("BERNSTEIN_STATE_KEY_PASSPHRASE", "wrong")
        km2 = KeyManager(tmp_path / ".sdd")
        with pytest.raises(ValueError, match="unwrap"):
            km2.load_key()

    def test_wrapped_key_without_passphrase_errors(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_STATE_KEY_PASSPHRASE", "hunter2")
        km = KeyManager(tmp_path / ".sdd")
        km.ensure_key()

        monkeypatch.delenv("BERNSTEIN_STATE_KEY_PASSPHRASE", raising=False)
        km2 = KeyManager(tmp_path / ".sdd")
        with pytest.raises(ValueError, match="PASSPHRASE"):
            km2.load_key()

    def test_plain_key_ignored_by_passphrase_env_on_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A pre-existing unwrapped key (e.g. just migrated) must still
        # load cleanly even if the passphrase env var is set — we only
        # unwrap blobs that carry the BSK1 magic.
        key_path = tmp_path / "state-key-override"
        key_path.write_bytes(b"p" * 32)

        monkeypatch.setenv("BERNSTEIN_STATE_KEY_PASSPHRASE", "hunter2")
        km = KeyManager(tmp_path / ".sdd")
        assert km.load_key() == b"p" * 32
