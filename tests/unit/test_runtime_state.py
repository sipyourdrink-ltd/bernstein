"""Tests for runtime state helpers used by Track B features."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.runtime_state import (
    SessionReplayMetadata,
    read_session_replay_metadata,
    rotate_log_file,
    write_session_replay_metadata,
)


def test_rotate_log_file_moves_large_log(tmp_path: Path) -> None:
    log_path = tmp_path / "server.log"
    log_path.write_text("x" * 32, encoding="utf-8")

    rotated = rotate_log_file(log_path, max_bytes=16)

    assert rotated is True
    assert not log_path.exists()
    assert (tmp_path / "server.log.1").exists()


def test_session_replay_metadata_round_trip(tmp_path: Path) -> None:
    metadata = SessionReplayMetadata(
        run_id="run-123",
        started_at=123.0,
        git_sha="abcdef123456",
        git_branch="main",
        config_hash="deadbeef",
        seed_path="bernstein.yaml",
    )

    write_session_replay_metadata(tmp_path / ".sdd", metadata)
    loaded = read_session_replay_metadata(tmp_path / ".sdd" / "runs" / "run-123")

    assert loaded == metadata
