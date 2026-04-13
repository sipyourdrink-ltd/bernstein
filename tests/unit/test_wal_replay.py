"""Tests for ORCH-007: WAL replay on crash recovery."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from bernstein.core.wal import WALWriter
from bernstein.core.wal_replay import (
    IdempotencyStore,
    ReplaySummary,
    WALReplayEngine,
)


@pytest.fixture
def sdd_dir(tmp_path: Path) -> Path:
    """Return a temporary .sdd directory."""
    d = tmp_path / ".sdd"
    d.mkdir(parents=True)
    return d


def _write_uncommitted_entries(sdd_dir: Path, run_id: str, count: int = 3) -> None:
    """Helper to write uncommitted WAL entries for a given run."""
    writer = WALWriter(run_id, sdd_dir)
    for i in range(count):
        writer.append(
            decision_type="task_created",
            inputs={"task_id": f"T-{i:03d}", "title": f"Task {i}"},
            output={"status": "created"},
            actor="test",
            committed=False,
        )


def _write_committed_entries(sdd_dir: Path, run_id: str, count: int = 2) -> None:
    """Helper to write committed WAL entries."""
    writer = WALWriter(run_id, sdd_dir)
    for i in range(count):
        writer.append(
            decision_type="tick_start",
            inputs={"tick": i + 1},
            output={},
            actor="test",
            committed=True,
        )


# ---------------------------------------------------------------------------
# IdempotencyStore
# ---------------------------------------------------------------------------


class TestIdempotencyStore:
    """Tests for the idempotency store."""

    def test_initially_empty(self, sdd_dir: Path) -> None:
        store = IdempotencyStore(sdd_dir)
        from bernstein.core.wal import WALEntry

        entry = WALEntry(
            seq=0,
            prev_hash="",
            entry_hash="abc123",
            timestamp=0.0,
            decision_type="test",
            inputs={},
            output={},
            actor="test",
        )
        assert store.is_executed(entry) is False

    def test_mark_and_check(self, sdd_dir: Path) -> None:
        store = IdempotencyStore(sdd_dir)
        from bernstein.core.wal import WALEntry

        entry = WALEntry(
            seq=0,
            prev_hash="",
            entry_hash="abc123",
            timestamp=0.0,
            decision_type="test",
            inputs={},
            output={},
            actor="test",
        )
        store.mark_executed(entry)
        assert store.is_executed(entry) is True

    def test_persists_across_instances(self, sdd_dir: Path) -> None:
        from bernstein.core.wal import WALEntry

        entry = WALEntry(
            seq=0,
            prev_hash="",
            entry_hash="abc123",
            timestamp=0.0,
            decision_type="test",
            inputs={},
            output={},
            actor="test",
        )
        store1 = IdempotencyStore(sdd_dir)
        store1.mark_executed(entry)
        store2 = IdempotencyStore(sdd_dir)
        assert store2.is_executed(entry) is True

    def test_clear_removes_all(self, sdd_dir: Path) -> None:
        from bernstein.core.wal import WALEntry

        entry = WALEntry(
            seq=0,
            prev_hash="",
            entry_hash="abc123",
            timestamp=0.0,
            decision_type="test",
            inputs={},
            output={},
            actor="test",
        )
        store = IdempotencyStore(sdd_dir)
        store.mark_executed(entry)
        store.clear()
        assert store.is_executed(entry) is False


# ---------------------------------------------------------------------------
# WALReplayEngine — scanning
# ---------------------------------------------------------------------------


class TestWALReplayScanning:
    """Tests for scanning uncommitted entries."""

    def test_no_entries_returns_empty_summary(self, sdd_dir: Path) -> None:
        engine = WALReplayEngine(sdd_dir, current_run_id="current")
        summary = engine.scan_and_replay()
        assert summary.total_uncommitted == 0
        assert summary.replayed == 0

    def test_finds_uncommitted_entries(self, sdd_dir: Path) -> None:
        _write_uncommitted_entries(sdd_dir, "old-run", count=3)
        engine = WALReplayEngine(sdd_dir, current_run_id="current")
        summary = engine.scan_and_replay()
        assert summary.total_uncommitted == 3
        assert summary.replayed == 3

    def test_skips_current_run(self, sdd_dir: Path) -> None:
        _write_uncommitted_entries(sdd_dir, "current", count=5)
        engine = WALReplayEngine(sdd_dir, current_run_id="current")
        summary = engine.scan_and_replay()
        assert summary.total_uncommitted == 0

    def test_skips_committed_entries(self, sdd_dir: Path) -> None:
        _write_committed_entries(sdd_dir, "old-run", count=3)
        engine = WALReplayEngine(sdd_dir, current_run_id="current")
        summary = engine.scan_and_replay()
        assert summary.total_uncommitted == 0


# ---------------------------------------------------------------------------
# WALReplayEngine — idempotency
# ---------------------------------------------------------------------------


class TestWALReplayIdempotency:
    """Tests for idempotency during replay."""

    def test_skips_already_executed(self, sdd_dir: Path) -> None:
        _write_uncommitted_entries(sdd_dir, "old-run", count=2)
        engine = WALReplayEngine(sdd_dir, current_run_id="current")
        # First replay
        summary1 = engine.scan_and_replay()
        assert summary1.replayed == 2
        # Second replay — same entries should be skipped
        engine2 = WALReplayEngine(sdd_dir, current_run_id="current")
        summary2 = engine2.scan_and_replay()
        assert summary2.skipped_idempotent == 2
        assert summary2.replayed == 0


# ---------------------------------------------------------------------------
# WALReplayEngine — staleness
# ---------------------------------------------------------------------------


class TestWALReplayStaleness:
    """Tests for stale entry filtering."""

    def test_skips_stale_entries(self, sdd_dir: Path) -> None:
        # Write entries with old timestamps
        writer = WALWriter("old-run", sdd_dir)
        writer.append(
            decision_type="task_created",
            inputs={"task_id": "T-old"},
            output={},
            actor="test",
            committed=False,
        )
        # Hack: overwrite the WAL to have an old timestamp
        wal_path = sdd_dir / "runtime" / "wal" / "old-run.wal.jsonl"
        lines = wal_path.read_text().splitlines()
        old_entries = []
        for line in lines:
            if line.strip():
                data = json.loads(line)
                data["timestamp"] = time.time() - 7200  # 2 hours old
                old_entries.append(json.dumps(data))
        wal_path.write_text("\n".join(old_entries) + "\n")

        engine = WALReplayEngine(sdd_dir, current_run_id="current", max_replay_age_s=3600)
        summary = engine.scan_and_replay()
        assert summary.skipped_stale == 1


# ---------------------------------------------------------------------------
# WALReplayEngine — replay handler
# ---------------------------------------------------------------------------


class TestReplayHandler:
    """Tests for custom replay handlers."""

    def test_handler_called_for_each_entry(self, sdd_dir: Path) -> None:
        _write_uncommitted_entries(sdd_dir, "old-run", count=2)
        replayed_entries: list[str] = []

        def handler(entry: object) -> bool:
            replayed_entries.append(getattr(entry, "decision_type", ""))
            return True

        engine = WALReplayEngine(sdd_dir, current_run_id="current")
        summary = engine.scan_and_replay(replay_handler=handler)
        assert summary.replayed == 2
        assert len(replayed_entries) == 2

    def test_handler_failure_recorded(self, sdd_dir: Path) -> None:
        _write_uncommitted_entries(sdd_dir, "old-run", count=1)

        def failing_handler(entry: object) -> bool:
            raise RuntimeError("replay failed")

        engine = WALReplayEngine(sdd_dir, current_run_id="current")
        summary = engine.scan_and_replay(replay_handler=failing_handler)
        assert summary.failed == 1
        assert summary.replayed == 0

    def test_handler_returning_false(self, sdd_dir: Path) -> None:
        _write_uncommitted_entries(sdd_dir, "old-run", count=1)

        def handler(entry: object) -> bool:
            return False

        engine = WALReplayEngine(sdd_dir, current_run_id="current")
        summary = engine.scan_and_replay(replay_handler=handler)
        assert summary.failed == 1


# ---------------------------------------------------------------------------
# WALReplayEngine — informational entries
# ---------------------------------------------------------------------------


class TestInformationalEntries:
    """Tests for skipping informational entries."""

    def test_tick_start_entries_skipped(self, sdd_dir: Path) -> None:
        writer = WALWriter("old-run", sdd_dir)
        writer.append(
            decision_type="tick_start",
            inputs={"tick": 1},
            output={},
            actor="orchestrator",
            committed=False,
        )
        engine = WALReplayEngine(sdd_dir, current_run_id="current")
        summary = engine.scan_and_replay()
        # tick_start is informational, not counted as replayed
        assert summary.replayed == 0


# ---------------------------------------------------------------------------
# ReplaySummary
# ---------------------------------------------------------------------------


class TestReplaySummary:
    """Tests for the summary dataclass."""

    def test_defaults(self) -> None:
        summary = ReplaySummary()
        assert summary.total_uncommitted == 0
        assert summary.replayed == 0
        assert summary.skipped_idempotent == 0
        assert summary.skipped_stale == 0
        assert summary.failed == 0
        assert summary.results == []
        assert summary.duration_s == pytest.approx(0.0)
