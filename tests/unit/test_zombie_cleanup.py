"""Tests for AGENT-006 — zombie process cleanup on startup."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.zombie_cleanup import (
    scan_and_cleanup_zombies,
)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def pid_dir(workdir: Path) -> Path:
    d = workdir / ".sdd" / "runtime" / "pids"
    d.mkdir(parents=True)
    return d


def _write_pid_file(
    pid_dir: Path,
    session_id: str,
    *,
    worker_pid: int = 0,
    pid: int = 0,
    role: str = "backend",
) -> Path:
    data = {"worker_pid": worker_pid, "pid": pid, "role": role}
    f = pid_dir / f"{session_id}.json"
    f.write_text(json.dumps(data))
    return f


# ---------------------------------------------------------------------------
# No PID directory
# ---------------------------------------------------------------------------


class TestNoPidDir:
    def test_empty_result(self, workdir: Path) -> None:
        result = scan_and_cleanup_zombies(workdir)
        assert result.scanned == 0
        assert result.orphans_found == 0


# ---------------------------------------------------------------------------
# Stale PID files (process already dead)
# ---------------------------------------------------------------------------


class TestStalePidFiles:
    def test_dead_process_cleaned(self, workdir: Path, pid_dir: Path) -> None:
        _write_pid_file(pid_dir, "backend-abc", worker_pid=99999)

        with patch("bernstein.core.agents.zombie_cleanup.process_alive", return_value=False):
            result = scan_and_cleanup_zombies(workdir)

        assert result.scanned == 1
        assert result.stale_removed == 1
        assert result.orphans_found == 0
        # PID file should be removed
        assert not (pid_dir / "backend-abc.json").exists()

    def test_corrupt_pid_file_removed(self, workdir: Path, pid_dir: Path) -> None:
        f = pid_dir / "corrupt.json"
        f.write_text("not json at all")

        result = scan_and_cleanup_zombies(workdir)
        assert result.stale_removed == 1
        assert not f.exists()

    def test_empty_pids_removed(self, workdir: Path, pid_dir: Path) -> None:
        _write_pid_file(pid_dir, "no-pid", worker_pid=0, pid=0)

        result = scan_and_cleanup_zombies(workdir)
        assert result.stale_removed == 1


# ---------------------------------------------------------------------------
# Orphaned processes
# ---------------------------------------------------------------------------


class TestOrphanedProcesses:
    def test_alive_process_killed(self, workdir: Path, pid_dir: Path) -> None:
        _write_pid_file(pid_dir, "backend-alive", worker_pid=12345)

        killed_pids: list[int] = []

        def fake_process_alive(pid: int) -> bool:
            return pid == 12345 and pid not in killed_pids

        def fake_kill(pid: int, sig: int) -> bool:
            killed_pids.append(pid)
            return True

        with (
            patch("bernstein.core.agents.zombie_cleanup.process_alive", side_effect=fake_process_alive),
            patch("bernstein.core.agents.zombie_cleanup.kill_process", side_effect=fake_kill),
        ):
            result = scan_and_cleanup_zombies(workdir, grace_seconds=0)

        assert result.orphans_found == 1
        assert result.orphans_killed == 1
        assert 12345 in killed_pids

    def test_dry_run_no_kill(self, workdir: Path, pid_dir: Path) -> None:
        _write_pid_file(pid_dir, "backend-alive", worker_pid=12345)

        with patch("bernstein.core.agents.zombie_cleanup.process_alive", return_value=True):
            result = scan_and_cleanup_zombies(workdir, dry_run=True)

        assert result.orphans_found == 1
        assert result.orphans_killed == 0
        # PID file should still exist
        assert (pid_dir / "backend-alive.json").exists()

    def test_both_pids_checked(self, workdir: Path, pid_dir: Path) -> None:
        _write_pid_file(pid_dir, "dual-pid", worker_pid=100, pid=200)

        alive_set = {100, 200}
        killed: list[int] = []

        def fake_alive(pid: int) -> bool:
            return pid in alive_set and pid not in killed

        def fake_kill(pid: int, sig: int) -> bool:
            killed.append(pid)
            alive_set.discard(pid)
            return True

        with (
            patch("bernstein.core.agents.zombie_cleanup.process_alive", side_effect=fake_alive),
            patch("bernstein.core.agents.zombie_cleanup.kill_process", side_effect=fake_kill),
        ):
            result = scan_and_cleanup_zombies(workdir, grace_seconds=0)

        assert result.orphans_found == 1
        # Both PIDs should be killed
        assert 100 in killed
        assert 200 in killed


# ---------------------------------------------------------------------------
# Non-JSON files ignored
# ---------------------------------------------------------------------------


class TestFileFiltering:
    def test_non_json_ignored(self, workdir: Path, pid_dir: Path) -> None:
        (pid_dir / "README.txt").write_text("not a PID file")
        result = scan_and_cleanup_zombies(workdir)
        assert result.scanned == 0
