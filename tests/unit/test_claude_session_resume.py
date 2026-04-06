"""Tests for bernstein.core.claude_session_resume (CLAUDE-012)."""

from __future__ import annotations

import time
from pathlib import Path

from bernstein.core.claude_session_resume import (
    SessionResumeManager,
    SessionState,
)


class TestSessionState:
    def test_is_resumable_active(self) -> None:
        s = SessionState(
            session_id="s1",
            agent_id="a1",
            task_id="t1",
            status="active",
            last_activity_at=time.time(),
        )
        assert s.is_resumable()

    def test_is_resumable_interrupted(self) -> None:
        s = SessionState(
            session_id="s1",
            agent_id="a1",
            task_id="t1",
            status="interrupted",
            last_activity_at=time.time(),
        )
        assert s.is_resumable()

    def test_not_resumable_completed(self) -> None:
        s = SessionState(
            session_id="s1",
            agent_id="a1",
            task_id="t1",
            status="completed",
        )
        assert not s.is_resumable()

    def test_not_resumable_stale(self) -> None:
        s = SessionState(
            session_id="s1",
            agent_id="a1",
            task_id="t1",
            status="interrupted",
            last_activity_at=time.time() - 7200,  # 2 hours ago.
        )
        assert not s.is_resumable()

    def test_mark_interrupted(self) -> None:
        s = SessionState(session_id="s1", agent_id="a1", task_id="t1")
        s.mark_interrupted()
        assert s.status == "interrupted"

    def test_round_trip_serialization(self) -> None:
        s = SessionState(
            session_id="s1",
            agent_id="a1",
            task_id="t1",
            role="backend",
            model="sonnet",
        )
        d = s.to_dict()
        restored = SessionState.from_dict(d)
        assert restored.session_id == "s1"
        assert restored.role == "backend"


class TestSessionResumeManager:
    def test_register_and_find(self, tmp_path: Path) -> None:
        mgr = SessionResumeManager(state_dir=tmp_path)
        state = SessionState(
            session_id="s1",
            agent_id="a1",
            task_id="t1",
            status="interrupted",
            last_activity_at=time.time(),
        )
        mgr.register_session(state)
        found = mgr.find_resumable("t1")
        assert found is not None
        assert found.session_id == "s1"

    def test_find_resumable_no_match(self, tmp_path: Path) -> None:
        mgr = SessionResumeManager(state_dir=tmp_path)
        assert mgr.find_resumable("nonexistent") is None

    def test_update_activity(self, tmp_path: Path) -> None:
        mgr = SessionResumeManager(state_dir=tmp_path)
        state = SessionState(
            session_id="s1",
            agent_id="a1",
            task_id="t1",
            last_activity_at=1000.0,
        )
        mgr.register_session(state)
        mgr.update_activity("s1", context_tokens=50_000, turns=5)
        assert mgr.sessions["s1"].context_tokens == 50_000
        assert mgr.sessions["s1"].turns_completed == 5

    def test_mark_interrupted(self, tmp_path: Path) -> None:
        mgr = SessionResumeManager(state_dir=tmp_path)
        state = SessionState(session_id="s1", agent_id="a1", task_id="t1")
        mgr.register_session(state)
        mgr.mark_interrupted("s1")
        assert mgr.sessions["s1"].status == "interrupted"

    def test_mark_completed(self, tmp_path: Path) -> None:
        mgr = SessionResumeManager(state_dir=tmp_path)
        state = SessionState(session_id="s1", agent_id="a1", task_id="t1")
        mgr.register_session(state)
        mgr.mark_completed("s1")
        assert mgr.sessions["s1"].status == "completed"

    def test_build_resume_command(self, tmp_path: Path) -> None:
        mgr = SessionResumeManager(state_dir=tmp_path)
        state = SessionState(
            session_id="s1",
            agent_id="a1",
            task_id="t1",
            model="sonnet",
            status="interrupted",
            last_activity_at=time.time(),
        )
        mgr.register_session(state)
        cmd = mgr.build_resume_command("s1")
        assert "claude" in cmd
        assert "s1" in cmd
        assert "sonnet" in cmd

    def test_resumable_sessions(self, tmp_path: Path) -> None:
        mgr = SessionResumeManager(state_dir=tmp_path)
        for i in range(3):
            state = SessionState(
                session_id=f"s{i}",
                agent_id=f"a{i}",
                task_id=f"t{i}",
                status="interrupted",
                last_activity_at=time.time(),
            )
            mgr.register_session(state)
        # Mark one as completed.
        mgr.mark_completed("s1")
        resumable = mgr.resumable_sessions()
        assert len(resumable) == 2

    def test_cleanup_stale(self, tmp_path: Path) -> None:
        mgr = SessionResumeManager(state_dir=tmp_path, stale_threshold_s=60.0)
        state = SessionState(
            session_id="s1",
            agent_id="a1",
            task_id="t1",
            status="interrupted",
            last_activity_at=time.time() - 120.0,  # 2 min ago (> 60s threshold).
        )
        mgr.register_session(state)
        count = mgr.cleanup_stale()
        assert count == 1
        assert mgr.sessions["s1"].status == "stale"

    def test_persist_and_load(self, tmp_path: Path) -> None:
        mgr = SessionResumeManager(state_dir=tmp_path)
        state = SessionState(
            session_id="s1",
            agent_id="a1",
            task_id="t1",
            role="backend",
        )
        mgr.register_session(state)

        # Verify file exists.
        assert (tmp_path / "session_s1.json").exists()

        # Load into fresh manager.
        mgr2 = SessionResumeManager(state_dir=tmp_path)
        loaded = mgr2.load_from_disk()
        assert loaded == 1
        assert mgr2.sessions["s1"].role == "backend"
