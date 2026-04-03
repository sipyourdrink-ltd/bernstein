"""Tests for state_encryption — AES-256-GCM file encryption at rest."""

from __future__ import annotations

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
        km = KeyManager(sdd)
        key = km.ensure_key()
        assert len(key) == 32
        assert (sdd / "config" / "state-key").exists()

    def test_load_key_returns_existing(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        km = KeyManager(sdd)
        k1 = km.ensure_key()
        k2 = km.load_key()
        assert k1 == k2

    def test_load_key_returns_none_if_missing(self, tmp_path: Path) -> None:
        km = KeyManager(tmp_path / ".sdd")
        assert km.load_key() is None

    def test_delete_key_removes_file(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        km = KeyManager(sdd)
        km.ensure_key()
        km.delete_key()
        assert not (sdd / "config" / "state-key").exists()

    def test_key_file_has_restricted_permissions(self, tmp_path: Path) -> None:
        import os

        sdd = tmp_path / ".sdd"
        km = KeyManager(sdd)
        km.ensure_key()
        mode = os.stat(sdd / "config" / "state-key").st_mode & 0o777
        assert mode == 0o600
