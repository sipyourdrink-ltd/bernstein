"""Tests for disaster_recovery — backup/restore .sdd/ state."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from bernstein.core.disaster_recovery import (
    _BACKUP_DIRS,
    _EXCLUDE_DIRS,
    _EXCLUDE_PATTERNS,
    _MANIFEST_FILE,
    backup_sdd,
    restore_sdd,
)


def _create_test_sdd(tmp_path: Path) -> Path:
    """Create a minimal .sdd/ directory with test data.

    Populates both durable runtime state (WAL, file locks, team roster,
    session, task graph) and transient runtime state (pids, signals,
    logs) so backup inclusion/exclusion can be asserted.
    """
    sdd = tmp_path / ".sdd"
    for d in _BACKUP_DIRS:
        (sdd / d).mkdir(parents=True, exist_ok=True)
    for d in _EXCLUDE_DIRS:
        (sdd / d).mkdir(parents=True, exist_ok=True)

    # Persistent backlog / metrics / traces.
    (sdd / "backlog/open/task1.yaml").write_text("id: T1\n", encoding="utf-8")
    (sdd / "backlog/open/task2.yaml").write_text("id: T2\n", encoding="utf-8")
    (sdd / "backlog/done/task3.yaml").write_text("id: T3\n", encoding="utf-8")
    (sdd / "metrics/test.jsonl").write_text("{}", encoding="utf-8")
    (sdd / "traces/trace.jsonl").write_text("{}", encoding="utf-8")
    (sdd / "config.yaml").write_text("workspace: test\n", encoding="utf-8")

    # Durable runtime state — MUST be included in backups.
    (sdd / "runtime/wal").mkdir(parents=True, exist_ok=True)
    (sdd / "runtime/wal/20260101-000000.wal.jsonl").write_text('{"event": "task_started"}\n', encoding="utf-8")
    (sdd / "runtime/file_locks.json").write_text('{"locks": []}', encoding="utf-8")
    (sdd / "runtime/team.json").write_text('{"agents": []}', encoding="utf-8")
    (sdd / "runtime/session.json").write_text('{"started_at": 0}', encoding="utf-8")
    (sdd / "runtime/session_state.json").write_text('{"phase": "idle"}', encoding="utf-8")
    (sdd / "runtime/task_graph.json").write_text('{"nodes": []}', encoding="utf-8")
    (sdd / "runtime/completion_budgets.json").write_text("{}", encoding="utf-8")
    (sdd / "runtime/watchdog_incidents.jsonl").write_text('{"incident": "stall"}\n', encoding="utf-8")

    # Transient runtime state — MUST be excluded from backups.
    (sdd / "runtime/pids").mkdir(parents=True, exist_ok=True)
    (sdd / "runtime/pids/worker-99479").write_text("99479\n", encoding="utf-8")
    (sdd / "runtime/signals").mkdir(parents=True, exist_ok=True)
    (sdd / "runtime/signals/drain").write_text("now", encoding="utf-8")
    (sdd / "runtime/heartbeats").mkdir(parents=True, exist_ok=True)
    (sdd / "runtime/heartbeats/arch-1.json").write_text("{}", encoding="utf-8")
    (sdd / "runtime/hooks").mkdir(parents=True, exist_ok=True)
    (sdd / "runtime/hooks/arch-1.jsonl").write_text("{}\n", encoding="utf-8")
    (sdd / "runtime/draining").mkdir(parents=True, exist_ok=True)
    (sdd / "runtime/draining/agent-1").write_text("", encoding="utf-8")
    (sdd / "runtime/gates").mkdir(parents=True, exist_ok=True)
    (sdd / "runtime/gates/qa-gate").write_text("", encoding="utf-8")
    (sdd / "runtime/completed").mkdir(parents=True, exist_ok=True)
    (sdd / "runtime/completed/arch-1").write_text("", encoding="utf-8")
    (sdd / "runtime/orchestrator.log").write_text("ts=1\n", encoding="utf-8")
    (sdd / "runtime/server.log.1").write_text("ts=2\n", encoding="utf-8")
    (sdd / "runtime/access.jsonl").write_text("{}\n", encoding="utf-8")
    (sdd / "runtime/access.jsonl.1").write_text("{}\n", encoding="utf-8")
    (sdd / "runtime/agent-1.kill").write_text("", encoding="utf-8")
    (sdd / "runtime/agent-1.pid").write_text("123\n", encoding="utf-8")
    (sdd / "runtime/retrospective.md").write_text("# notes\n", encoding="utf-8")
    (sdd / "runtime/summary.md").write_text("# summary\n", encoding="utf-8")

    # Top-level excluded directories.
    (sdd / "logs/app.log").write_text("log entry\n", encoding="utf-8")
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
            # Backlog and top-level excluded dirs behave as expected.
            assert any("backlog/open/task1.yaml" in n for n in names)
            assert not any(n.startswith("logs/") or "/logs/" in n for n in names)

    def test_excludes_ephemeral_dirs(self, tmp_path: Path) -> None:
        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        backup_sdd(sdd, dest)

        with tarfile.open(dest, "r:gz") as tar:
            names = tar.getnames()
            for excluded in _EXCLUDE_DIRS:
                assert not any(excluded + "/" in n for n in names)

    def test_includes_durable_runtime_state(self, tmp_path: Path) -> None:
        """Regression for audit-074: backup MUST capture ``runtime/`` state.

        WAL, file locks, session, team roster, task graph, budgets, and
        incident history drive warm-restart behaviour — excluding them
        turned restores into cold starts.
        """
        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        backup_sdd(sdd, dest)

        with tarfile.open(dest, "r:gz") as tar:
            names = tar.getnames()

        expected = (
            "runtime/wal/20260101-000000.wal.jsonl",
            "runtime/file_locks.json",
            "runtime/team.json",
            "runtime/session.json",
            "runtime/session_state.json",
            "runtime/task_graph.json",
            "runtime/completion_budgets.json",
            "runtime/watchdog_incidents.jsonl",
        )
        for path in expected:
            assert any(path in n for n in names), f"missing from backup: {path}"

    def test_excludes_transient_runtime_artifacts(self, tmp_path: Path) -> None:
        """Logs, pids, signals, hooks, heartbeats — stay out of backups."""
        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        backup_sdd(sdd, dest)

        with tarfile.open(dest, "r:gz") as tar:
            names = tar.getnames()

        forbidden = (
            "runtime/pids/worker-99479",
            "runtime/signals/drain",
            "runtime/heartbeats/arch-1.json",
            "runtime/hooks/arch-1.jsonl",
            "runtime/draining/agent-1",
            "runtime/gates/qa-gate",
            "runtime/completed/arch-1",
            "runtime/orchestrator.log",
            "runtime/server.log.1",
            "runtime/access.jsonl",
            "runtime/access.jsonl.1",
            "runtime/agent-1.kill",
            "runtime/agent-1.pid",
            "runtime/retrospective.md",
            "runtime/summary.md",
        )
        for path in forbidden:
            assert not any(n.endswith(path) for n in names), f"transient artifact leaked into backup: {path}"

    def test_manifest_records_exclude_patterns(self, tmp_path: Path) -> None:
        """The manifest should document which glob patterns were filtered."""
        import json

        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        backup_sdd(sdd, dest)

        with tarfile.open(dest, "r:gz") as tar:
            member = tar.getmember(_MANIFEST_FILE)
            extracted = tar.extractfile(member)
            assert extracted is not None
            manifest = json.loads(extracted.read())

        assert "runtime" in manifest["included_dirs"]
        assert manifest["excluded_patterns"] == list(_EXCLUDE_PATTERNS)

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

    def test_encrypt_without_password_raises(self, tmp_path: Path) -> None:
        """Regression for audit-075: encrypt=True without a password must fail.

        Previously, ``_get_crypto`` silently fell back to an ephemeral
        ``Fernet.generate_key()`` — the random key was never persisted, so
        restore could never succeed (silent data-loss bug).
        """
        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        with pytest.raises(ValueError, match="encryption requires a password"):
            backup_sdd(sdd, dest, encrypt=True, password=None)

    def test_encrypt_with_empty_password_raises(self, tmp_path: Path) -> None:
        """Empty string password is also rejected (falsy)."""
        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        with pytest.raises(ValueError, match="encryption requires a password"):
            backup_sdd(sdd, dest, encrypt=True, password="")


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
        # Durable runtime state (audit-074) must round-trip.
        assert (restore_dir / "runtime/wal/20260101-000000.wal.jsonl").exists()
        assert (restore_dir / "runtime/file_locks.json").exists()
        assert (restore_dir / "runtime/team.json").exists()
        # Transient artifacts stay filtered.
        assert not (restore_dir / "runtime/pids/worker-99479").exists()
        assert not (restore_dir / "runtime/orchestrator.log").exists()
        assert not (restore_dir / "runtime/access.jsonl").exists()

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
        with pytest.raises((ValueError, tarfile.OutsideDestinationError)):
            restore_sdd(malicious_tar, restore_dir)

    def test_restore_sdd_no_fd_leak_non_decrypt(self, tmp_path: Path) -> None:
        """Regression for audit-079: non-decrypt restore must close source fd.

        Previously, ``tarfile.open(fileobj=source.open('rb'), mode='r:*')``
        left the underlying file descriptor dangling — tarfile does not own
        fds passed via ``fileobj``.  Repeated restores accumulated leaked
        fds until the process hit ``EMFILE``.
        """
        import gc
        import os

        sdd = _create_test_sdd(tmp_path)
        dest = tmp_path / "backup.tar.gz"
        backup_sdd(sdd, dest)

        fd_dir = Path(f"/proc/{os.getpid()}/fd")

        def _open_fd_count() -> int:
            if fd_dir.is_dir():
                return sum(1 for _ in fd_dir.iterdir())
            # Fallback for platforms without /proc (e.g. macOS): count via
            # sentinel file descriptor allocation.  If fds leaked, the next
            # freshly opened fd number grows unbounded across iterations.
            import tempfile as _tempfile

            with _tempfile.TemporaryFile() as tf:
                return tf.fileno()

        # Warm-up: pay one-time allocations before sampling the baseline.
        for _ in range(3):
            restore_sdd(dest, tmp_path / "restore_warmup" / ".sdd")
        gc.collect()
        baseline = _open_fd_count()

        for i in range(100):
            restore_sdd(dest, tmp_path / f"restore_{i}" / ".sdd")

        gc.collect()
        after = _open_fd_count()

        # Tight bound: the restore itself must close its own fd.  Allow a
        # small slack for unrelated transient fds opened by pytest/logging.
        assert after - baseline < 10, (
            f"FD leak: baseline={baseline} after 100 restores={after} (delta={after - baseline})"
        )
