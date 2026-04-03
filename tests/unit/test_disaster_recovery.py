"""Tests for disaster_recovery — backup/restore .sdd/ state."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from bernstein.core.disaster_recovery import (
    _BACKUP_DIRS,
    _EXCLUDE_DIRS,
    _MANIFEST_FILE,
    backup_sdd,
    restore_sdd,
)


def _create_test_sdd(tmp_path: Path) -> Path:
    """Create a minimal .sdd/ directory with test data."""
    sdd = tmp_path / ".sdd"
    for d in _BACKUP_DIRS:
        (sdd / d).mkdir(parents=True, exist_ok=True)
    for d in _EXCLUDE_DIRS:
        (sdd / d).mkdir(parents=True, exist_ok=True)

    # Create test files
    (sdd / "backlog/open/task1.yaml").write_text("id: T1\n", encoding="utf-8")
    (sdd / "backlog/open/task2.yaml").write_text("id: T2\n", encoding="utf-8")
    (sdd / "backlog/done/task3.yaml").write_text("id: T3\n", encoding="utf-8")
    (sdd / "metrics/test.jsonl").write_text("{}", encoding="utf-8")
    (sdd / "runtime/ephemeral.pid").write_text("12345\n", encoding="utf-8")
    (sdd / "logs/app.log").write_text("log entry\n", encoding="utf-8")
    (sdd / "traces/trace.jsonl").write_text("{}", encoding="utf-8")
    (sdd / "config.yaml").write_text("workspace: test\n", encoding="utf-8")
    return sdd


class TestBackupSdd:
    def test_creates_tarball(self, tmp_path: Path) -> None:
        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        result = backup_sdd(sdd, dest)

        assert dest.exists()
        assert "path" in result
        assert "size_bytes" in result
        assert "file_count" in result
        assert "sha256" in result
        assert int(result["file_count"]) > 0

    def test_includes_persistent_dirs(self, tmp_path: Path) -> None:
        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        backup_sdd(sdd, dest)

        with tarfile.open(dest, "r:gz") as tar:
            names = tar.getnames()
            # Should include persistent files but not runtime
            assert any("backlog/open/task1.yaml" in n for n in names)
            assert not any("runtime/" in n for n in names)
            assert not any("logs/" in n for n in names)

    def test_excludes_ephemeral_dirs(self, tmp_path: Path) -> None:
        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        backup_sdd(sdd, dest)

        with tarfile.open(dest, "r:gz") as tar:
            names = tar.getnames()
            for excluded in _EXCLUDE_DIRS:
                assert not any(excluded + "/" in n for n in names)

    def test_contains_manifest(self, tmp_path: Path) -> None:
        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        backup_sdd(sdd, dest)

        with tarfile.open(dest, "r:gz") as tar:
            manifest_found = any(_MANIFEST_FILE in n for n in tar.getnames())
        assert manifest_found

    def test_missing_sdd_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            backup_sdd(tmp_path / "nonexistent", tmp_path / "backup.tar.gz")

    def test_encrypts_when_requested(self, tmp_path: Path) -> None:
        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        result = backup_sdd(sdd, dest, encrypt=True, password="testpass")

        assert result["path"].endswith(".enc")
        assert Path(result["path"]).exists()
        # Original should be deleted
        assert not dest.exists()


class TestRestoreSdd:
    def test_restores_backup(self, tmp_path: Path) -> None:
        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        backup_sdd(sdd, dest)

        restore_dir = tmp_path / "restore" / ".sdd"
        result = restore_sdd(dest, restore_dir)

        assert "files_restored" in result
        assert int(result["files_restored"]) > 0
        assert (restore_dir / "backlog/open/task1.yaml").exists()
        assert (restore_dir / "backlog/done/task3.yaml").exists()
        # Ephemeral should NOT be restored
        assert not (restore_dir / "runtime/ephemeral.pid").exists()

    def test_dry_run_lists_contents(self, tmp_path: Path) -> None:
        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        backup_sdd(sdd, dest)

        result = restore_sdd(dest, tmp_path / "nonexistent/.sdd", dry_run=True)
        assert "files" in result
        assert "files_restored" in result

    def test_missing_source_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            restore_sdd(tmp_path / "missing.tar.gz", tmp_path / ".sdd")

    def test_restores_encrypted_backup(self, tmp_path: Path) -> None:
        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        result = backup_sdd(sdd, dest, encrypt=True, password="testpass")

        restore_dir = tmp_path / "restore" / ".sdd"
        restored = restore_sdd(Path(result["path"]), restore_dir, decrypt=True, password="testpass")
        assert "files_restored" in restored

    def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        """Malicious tar with path traversal should be blocked."""
        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        backup_sdd(sdd, dest)

        # Create a malicious tar with path traversal
        import io

        malicious_tar = tmp_path / "malicious.tar.gz"
        with tarfile.open(malicious_tar, "w:gz") as tar:
            data = b"malicious content"
            info = tarfile.TarInfo(name="../../../etc/malicious")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        restore_dir = tmp_path / "restore" / ".sdd"
        with pytest.raises(ValueError, match="Path traversal"):
            restore_sdd(malicious_tar, restore_dir)
