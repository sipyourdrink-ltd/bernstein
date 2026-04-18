"""Tests for ``persistence.disk_retention`` (audit-081).

Long-running orchestrator sessions accumulated per-run state in
``.sdd/runs/`` and ``.sdd/runtime/wal/`` with no GC. The retention janitor
keeps the newest N entries by mtime and deletes the rest.
"""

from __future__ import annotations

import os
from pathlib import Path

from bernstein.core.persistence.disk_retention import (
    prune_old_runs,
    prune_old_wal_files,
    run_retention,
)


def _touch(path: Path, mtime: float) -> None:
    """Create an empty file/dir and stamp its mtime."""
    if not path.exists():
        if path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        else:
            path.mkdir(parents=True, exist_ok=True)
    os.utime(path, (mtime, mtime))


def test_prune_old_runs_keeps_newest_n(tmp_path: Path) -> None:
    """With 25 run dirs and retention 20, the 5 oldest must be deleted."""
    sdd = tmp_path / ".sdd"
    base_ts = 1_700_000_000.0
    for i in range(25):
        _touch(sdd / "runs" / f"run-{i:02d}", base_ts + i)

    scanned, deleted, errors = prune_old_runs(sdd, retention_count=20)

    assert scanned == 25
    assert errors == []
    assert len(deleted) == 5
    remaining = sorted((sdd / "runs").iterdir())
    assert len(remaining) == 20
    remaining_names = {p.name for p in remaining}
    assert "run-00" not in remaining_names
    assert "run-04" not in remaining_names
    assert "run-05" in remaining_names
    assert "run-24" in remaining_names


def test_prune_old_runs_preserves_active(tmp_path: Path) -> None:
    """The active run must never be deleted even if it is the oldest."""
    sdd = tmp_path / ".sdd"
    base_ts = 1_700_000_000.0
    for i in range(25):
        _touch(sdd / "runs" / f"run-{i:02d}", base_ts + i)

    # run-00 is the OLDEST — without protection it would be deleted.
    scanned, deleted, errors = prune_old_runs(
        sdd,
        retention_count=20,
        active_run_id="run-00",
    )

    assert scanned == 25
    assert errors == []
    assert "run-00" not in deleted
    assert (sdd / "runs" / "run-00").is_dir()


def test_prune_old_runs_empty_dir_is_noop(tmp_path: Path) -> None:
    """Missing .sdd/runs must not error."""
    scanned, deleted, errors = prune_old_runs(tmp_path / ".sdd", retention_count=20)
    assert scanned == 0
    assert deleted == []
    assert errors == []


def test_prune_old_wal_files_keeps_newest_n(tmp_path: Path) -> None:
    """With 60 WAL run groups and retention 50, the 10 oldest must be deleted."""
    sdd = tmp_path / ".sdd"
    wal_dir = sdd / "runtime" / "wal"
    base_ts = 1_700_000_000.0
    for i in range(60):
        _touch(wal_dir / f"run-{i:02d}.wal.jsonl", base_ts + i)

    scanned, deleted, errors = prune_old_wal_files(sdd, retention_count=50)

    assert scanned == 60
    assert errors == []
    assert len(deleted) == 10
    remaining = [p.name for p in wal_dir.iterdir() if p.is_file()]
    assert len(remaining) == 50


def test_prune_old_wal_files_groups_rotated_backups(tmp_path: Path) -> None:
    """Rotated ``.N`` backups must be grouped with their live file."""
    sdd = tmp_path / ".sdd"
    wal_dir = sdd / "runtime" / "wal"
    base_ts = 1_700_000_000.0
    for i in range(5):
        _touch(wal_dir / f"run-{i:02d}.wal.jsonl", base_ts + i)
        _touch(wal_dir / f"run-{i:02d}.wal.jsonl.1", base_ts + i)
        _touch(wal_dir / f"run-{i:02d}.wal.jsonl.2", base_ts + i)

    # Keep newest 2 runs — 3 runs (each with 3 files) should be deleted.
    scanned, deleted, errors = prune_old_wal_files(sdd, retention_count=2)

    assert scanned == 5  # 5 run groups, not 15 individual files
    assert errors == []
    assert len(deleted) == 9  # 3 runs * 3 files per run
    # run-03 and run-04 survive with all their backups.
    assert (wal_dir / "run-04.wal.jsonl").exists()
    assert (wal_dir / "run-04.wal.jsonl.1").exists()
    assert (wal_dir / "run-04.wal.jsonl.2").exists()
    assert (wal_dir / "run-03.wal.jsonl").exists()
    # run-00 is deleted along with all its rollovers.
    assert not (wal_dir / "run-00.wal.jsonl").exists()
    assert not (wal_dir / "run-00.wal.jsonl.1").exists()


def test_prune_old_wal_files_skips_idempotency(tmp_path: Path) -> None:
    """``idempotency.jsonl`` + rollovers must never be deleted by WAL retention."""
    sdd = tmp_path / ".sdd"
    wal_dir = sdd / "runtime" / "wal"
    base_ts = 1_700_000_000.0
    _touch(wal_dir / "idempotency.jsonl", base_ts - 10_000)
    _touch(wal_dir / "idempotency.jsonl.1", base_ts - 10_000)
    for i in range(5):
        _touch(wal_dir / f"run-{i:02d}.wal.jsonl", base_ts + i)

    _scanned, deleted, errors = prune_old_wal_files(sdd, retention_count=1)
    assert errors == []
    assert "idempotency.jsonl" not in deleted
    assert "idempotency.jsonl.1" not in deleted
    assert (wal_dir / "idempotency.jsonl").exists()
    assert (wal_dir / "idempotency.jsonl.1").exists()


def test_prune_old_wal_files_preserves_active_run(tmp_path: Path) -> None:
    """Active run's WAL must survive even when it is the oldest."""
    sdd = tmp_path / ".sdd"
    wal_dir = sdd / "runtime" / "wal"
    base_ts = 1_700_000_000.0
    for i in range(5):
        _touch(wal_dir / f"run-{i:02d}.wal.jsonl", base_ts + i)

    _scanned, deleted, _errors = prune_old_wal_files(
        sdd,
        retention_count=2,
        active_run_id="run-00",
    )

    assert "run-00.wal.jsonl" not in deleted
    assert (wal_dir / "run-00.wal.jsonl").exists()


def test_run_retention_sweeps_both_trees(tmp_path: Path) -> None:
    """Single-call sweep handles both runs/ and runtime/wal/."""
    sdd = tmp_path / ".sdd"
    base_ts = 1_700_000_000.0
    for i in range(25):
        _touch(sdd / "runs" / f"run-{i:02d}", base_ts + i)
        _touch(sdd / "runtime" / "wal" / f"run-{i:02d}.wal.jsonl", base_ts + i)

    result = run_retention(
        sdd,
        active_run_id="run-24",
        run_retention_count=20,
        wal_retention_count=50,
    )

    assert result.runs_scanned == 25
    assert result.wal_scanned == 25
    assert len(result.runs_deleted) == 5
    # 25 WAL files with retention 50 — nothing should be deleted.
    assert result.wal_deleted == []
    assert result.errors == []
    assert (sdd / "runs" / "run-24").is_dir()


def test_run_retention_missing_sdd_is_noop(tmp_path: Path) -> None:
    """Non-existent .sdd tree must be a silent no-op."""
    result = run_retention(tmp_path / "nope" / ".sdd")
    assert result.runs_scanned == 0
    assert result.wal_scanned == 0
    assert result.runs_deleted == []
    assert result.wal_deleted == []
    assert result.errors == []
