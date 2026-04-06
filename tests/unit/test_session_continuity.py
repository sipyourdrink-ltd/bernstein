"""Tests for agent session continuity (AGENT-016)."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.session_continuity import (
    SessionContinuityStore,
    SessionSnapshot,
)


class TestSessionSnapshot:
    def test_to_dict_roundtrip(self) -> None:
        snap = SessionSnapshot(
            session_id="sess-1",
            task_ids=["t-1"],
            role="backend",
            files_modified=["src/main.py"],
            partial_work_summary="Added new endpoint",
            context_hints=["Check db migration"],
            retry_count=1,
            terminal_reason="timeout",
            worktree_branch="agent/sess-1",
            last_commit_sha="abc123",
        )
        data = snap.to_dict()
        restored = SessionSnapshot.from_dict(data)
        assert restored.session_id == "sess-1"
        assert restored.task_ids == ["t-1"]
        assert restored.role == "backend"
        assert restored.files_modified == ["src/main.py"]
        assert restored.partial_work_summary == "Added new endpoint"
        assert restored.retry_count == 1
        assert restored.terminal_reason == "timeout"
        assert restored.worktree_branch == "agent/sess-1"
        assert restored.last_commit_sha == "abc123"

    def test_from_dict_with_defaults(self) -> None:
        snap = SessionSnapshot.from_dict({})
        assert snap.session_id == ""
        assert snap.retry_count == 0
        assert snap.task_ids == []

    def test_build_retry_context(self) -> None:
        snap = SessionSnapshot(
            session_id="sess-1",
            task_ids=["t-1"],
            role="backend",
            files_modified=["src/main.py"],
            partial_work_summary="Implemented GET /users",
            context_hints=["Need to also add POST /users"],
            retry_count=1,
            terminal_reason="timeout",
            last_commit_sha="abc123",
        )
        context = snap.build_retry_context()
        assert "retry #2" in context
        assert "timeout" in context
        assert "Implemented GET /users" in context
        assert "src/main.py" in context
        assert "abc123" in context
        assert "Need to also add POST /users" in context
        assert "Do NOT repeat" in context

    def test_build_retry_context_minimal(self) -> None:
        snap = SessionSnapshot(session_id="sess-1")
        context = snap.build_retry_context()
        assert "retry #1" in context


class TestSessionContinuityStore:
    def test_save_and_load(self, tmp_path: Path) -> None:
        store = SessionContinuityStore(state_dir=tmp_path / "continuity")
        snap = SessionSnapshot(
            session_id="sess-1",
            task_ids=["t-1"],
            role="backend",
        )
        store.save_snapshot(snap)

        loaded = store.load_snapshot("sess-1")
        assert loaded is not None
        assert loaded.session_id == "sess-1"
        assert loaded.task_ids == ["t-1"]

    def test_load_nonexistent(self, tmp_path: Path) -> None:
        store = SessionContinuityStore(state_dir=tmp_path / "continuity")
        assert store.load_snapshot("nonexistent") is None

    def test_delete_snapshot(self, tmp_path: Path) -> None:
        store = SessionContinuityStore(state_dir=tmp_path / "continuity")
        snap = SessionSnapshot(session_id="sess-1", role="qa")
        store.save_snapshot(snap)
        assert store.delete_snapshot("sess-1")
        assert store.load_snapshot("sess-1") is None

    def test_delete_nonexistent(self, tmp_path: Path) -> None:
        store = SessionContinuityStore(state_dir=tmp_path / "continuity")
        assert not store.delete_snapshot("nonexistent")

    def test_list_snapshots(self, tmp_path: Path) -> None:
        store = SessionContinuityStore(state_dir=tmp_path / "continuity")
        store.save_snapshot(SessionSnapshot(session_id="a", role="qa"))
        store.save_snapshot(SessionSnapshot(session_id="b", role="be"))
        ids = store.list_snapshots()
        assert set(ids) == {"a", "b"}

    def test_list_snapshots_empty_dir(self, tmp_path: Path) -> None:
        store = SessionContinuityStore(state_dir=tmp_path / "nonexistent")
        assert store.list_snapshots() == []

    def test_corrupted_snapshot_returns_none(self, tmp_path: Path) -> None:
        store = SessionContinuityStore(state_dir=tmp_path / "continuity")
        (tmp_path / "continuity").mkdir(parents=True)
        (tmp_path / "continuity" / "bad.json").write_text("not json", encoding="utf-8")
        assert store.load_snapshot("bad") is None
