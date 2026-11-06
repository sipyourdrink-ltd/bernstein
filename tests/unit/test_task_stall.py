"""Tests for task-level stall detection (issue #5439)."""

from __future__ import annotations

import time
from pathlib import Path

from bernstein.core.orchestration.task_stall import (
    ArtifactProgressClock,
    ArtifactProgressState,
    FanoutCeiling,
    RepeatedCommandDetector,
    TaskStallSignal,
    _hash_worktree,
)

# ---------------------------------------------------------------------------
# TaskStallSignal
# ---------------------------------------------------------------------------


class TestTaskStallSignal:
    def test_fields(self) -> None:
        sig = TaskStallSignal(task_id="t1", reason="stalled", detail={"k": "v"})
        assert sig.task_id == "t1"
        assert sig.reason == "stalled"
        assert sig.detail == {"k": "v"}


# ---------------------------------------------------------------------------
# _hash_worktree
# ---------------------------------------------------------------------------


class TestHashWorktree:
    def test_empty_dir_gives_stable_hash(self, tmp_path: Path) -> None:
        h1 = _hash_worktree(tmp_path)
        h2 = _hash_worktree(tmp_path)
        assert h1 == h2

    def test_file_change_moves_hash(self, tmp_path: Path) -> None:
        f = tmp_path / "main.py"
        f.write_text("a", encoding="utf-8")
        h1 = _hash_worktree(tmp_path)
        f.write_text("b", encoding="utf-8")
        h2 = _hash_worktree(tmp_path)
        assert h1 != h2

    def test_nonexistent_path_returns_empty(self, tmp_path: Path) -> None:
        assert _hash_worktree(tmp_path / "missing") == ""

    def test_git_dir_excluded(self, tmp_path: Path) -> None:
        git = tmp_path / ".git"
        git.mkdir()
        (git / "config").write_text("repo", encoding="utf-8")
        (tmp_path / "main.py").write_text("x", encoding="utf-8")
        h1 = _hash_worktree(tmp_path)
        (git / "config").write_text("changed", encoding="utf-8")
        h2 = _hash_worktree(tmp_path)
        assert h1 == h2  # git change is invisible


# ---------------------------------------------------------------------------
# ArtifactProgressClock
# ---------------------------------------------------------------------------


class TestArtifactProgressClock:
    def test_new_file_resets_stall_clock(self, tmp_path: Path) -> None:
        clock = ArtifactProgressClock(stall_seconds=60)
        t0 = time.time() - 120  # 2 minutes ago

        # Seed with an old progress timestamp so the task looks stale.
        clock._states["t1"] = ArtifactProgressState(last_hash="old-hash", last_progress_ts=t0)
        (tmp_path / "new.py").write_text("content", encoding="utf-8")

        signal = clock.observe("t1", tmp_path)
        assert signal is None  # progress was made; no stall

    def test_no_change_beyond_threshold_signals_stall(self, tmp_path: Path) -> None:
        clock = ArtifactProgressClock(stall_seconds=60)
        (tmp_path / "main.py").write_text("x", encoding="utf-8")
        first_hash = _hash_worktree(tmp_path)
        t0 = time.time() - 120  # 2 minutes ago (beyond 60s threshold)

        clock._states["t1"] = ArtifactProgressState(last_hash=first_hash, last_progress_ts=t0)

        signal = clock.observe("t1", tmp_path)
        assert signal is not None
        assert signal.task_id == "t1"
        assert "no artefact progress" in signal.reason
        assert signal.detail["quiet_s"] >= 60

    def test_new_task_not_immediately_stalled(self, tmp_path: Path) -> None:
        clock = ArtifactProgressClock(stall_seconds=60)
        signal = clock.observe("t-new", tmp_path)
        assert signal is None

    def test_reset_removes_state(self, tmp_path: Path) -> None:
        clock = ArtifactProgressClock(stall_seconds=1)
        t0 = time.time() - 10
        clock._states["t1"] = ArtifactProgressState(last_hash="h", last_progress_ts=t0)
        clock.reset("t1")
        signal = clock.observe("t1", tmp_path)
        assert signal is None  # fresh start


# ---------------------------------------------------------------------------
# RepeatedCommandDetector
# ---------------------------------------------------------------------------


class TestRepeatedCommandDetector:
    def test_below_threshold_no_signal(self) -> None:
        det = RepeatedCommandDetector(threshold=5)
        for _ in range(4):
            sig = det.observe("t1", "rg --foo", exit_code=1)
        assert sig is None

    def test_at_threshold_signals_stall(self) -> None:
        det = RepeatedCommandDetector(threshold=5)
        for _ in range(4):
            det.observe("t1", "rg --foo", exit_code=1)
        sig = det.observe("t1", "rg --foo", exit_code=1)
        assert sig is not None
        assert sig.task_id == "t1"
        assert "repeated command" in sig.reason
        assert sig.detail["count"] == 5
        assert sig.detail["command"] == "rg --foo"

    def test_different_exit_codes_are_distinct_keys(self) -> None:
        det = RepeatedCommandDetector(threshold=3)
        det.observe("t1", "pytest", exit_code=1)
        det.observe("t1", "pytest", exit_code=2)
        det.observe("t1", "pytest", exit_code=1)
        det.observe("t1", "pytest", exit_code=2)
        det.observe("t1", "pytest", exit_code=1)
        sig = det.observe("t1", "pytest", exit_code=1)
        assert sig is not None  # 3x exit_code=1

    def test_different_commands_are_distinct_keys(self) -> None:
        det = RepeatedCommandDetector(threshold=3)
        for cmd in ["ls", "pwd", "ls", "pwd", "ls"]:
            det.observe("t1", cmd, exit_code=0)
        sig = det.observe("t1", "ls", exit_code=0)
        assert sig is not None  # 4th ls

    def test_different_tasks_tracked_separately(self) -> None:
        det = RepeatedCommandDetector(threshold=3)
        for _ in range(5):
            det.observe("t1", "make build", exit_code=1)
        sig2 = det.observe("t2", "make build", exit_code=1)
        assert sig2 is None  # t2 has only seen it once

    def test_reset_clears_history(self) -> None:
        det = RepeatedCommandDetector(threshold=3)
        for _ in range(5):
            det.observe("t1", "fail", exit_code=1)
        det.reset("t1")
        sig = det.observe("t1", "fail", exit_code=1)
        assert sig is None  # count is back to 1


# ---------------------------------------------------------------------------
# FanoutCeiling
# ---------------------------------------------------------------------------


class TestFanoutCeiling:
    def test_admit_below_ceiling(self) -> None:
        c = FanoutCeiling(base_ceiling=10)
        assert c.admit(9) is True
        assert c.admit(10) is False

    def test_degrade_halves_ceiling(self) -> None:
        c = FanoutCeiling(base_ceiling=10, degrade_floor=4, no_progress_for_degrade=2)
        state = c.update(no_progress_count=2)
        assert state is not None
        assert state.degraded is True
        assert c.current_ceiling == 5  # 10 // 2
        assert c.admit(4) is True
        assert c.admit(5) is False

    def test_degrade_floor_enforced(self) -> None:
        c = FanoutCeiling(base_ceiling=6, degrade_floor=4, no_progress_for_degrade=1)
        c.update(no_progress_count=1)
        assert c.current_ceiling == 4  # floor prevents going below 4

    def test_recovery_restores_ceiling(self) -> None:
        c = FanoutCeiling(base_ceiling=10, degrade_floor=4, no_progress_for_degrade=2)
        c.update(no_progress_count=2)  # degrade
        state = c.update(no_progress_count=1)  # recover
        assert state is not None
        assert state.degraded is False
        assert c.current_ceiling == 10

    def test_no_change_returns_none(self) -> None:
        c = FanoutCeiling(base_ceiling=10, no_progress_for_degrade=3)
        assert c.update(no_progress_count=1) is None  # not enough to degrade
        assert c.update(no_progress_count=2) is None

    def test_state_to_dict(self) -> None:
        c = FanoutCeiling(base_ceiling=8, degrade_floor=4, no_progress_for_degrade=2)
        state = c.update(no_progress_count=2)
        assert state is not None
        d = state.to_dict()
        assert d["ceiling"] == 4
        assert d["degraded"] is True
        assert "degraded" in d["reason"]

    def test_degrade_emitted_once_until_recovery(self) -> None:
        c = FanoutCeiling(base_ceiling=10, no_progress_for_degrade=1)
        s1 = c.update(no_progress_count=1)
        s2 = c.update(no_progress_count=2)  # still degraded, no change event
        assert s1 is not None
        assert s2 is None  # no change since we're still degraded
