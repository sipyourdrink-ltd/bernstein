"""Tests for Bernstein Playground sandbox (road-031)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.cli.playground import (
    PlaygroundConfig,
    PlaygroundSession,
    discard_sandbox,
    list_active_sessions,
    load_sessions,
    save_session,
)

# ---------------------------------------------------------------------------
# PlaygroundSession / PlaygroundConfig construction
# ---------------------------------------------------------------------------


class TestPlaygroundSession:
    """Basic construction and field checks for PlaygroundSession."""

    def test_defaults(self) -> None:
        session = PlaygroundSession(
            session_id="abc123",
            original_dir="/tmp/orig",
            sandbox_dir="/tmp/sandbox",
            created_at="2026-04-10T00:00:00Z",
        )
        assert session.status == "active"
        assert session.session_id == "abc123"
        assert session.original_dir == "/tmp/orig"
        assert session.sandbox_dir == "/tmp/sandbox"
        assert session.created_at == "2026-04-10T00:00:00Z"

    def test_frozen(self) -> None:
        session = PlaygroundSession(
            session_id="abc123",
            original_dir="/tmp/orig",
            sandbox_dir="/tmp/sandbox",
            created_at="2026-04-10T00:00:00Z",
        )
        with pytest.raises(AttributeError):
            session.status = "discarded"  # type: ignore[misc]

    def test_explicit_status(self) -> None:
        session = PlaygroundSession(
            session_id="x",
            original_dir="/a",
            sandbox_dir="/b",
            created_at="2026-01-01T00:00:00Z",
            status="applied",
        )
        assert session.status == "applied"


class TestPlaygroundConfig:
    """Basic construction and field checks for PlaygroundConfig."""

    def test_defaults(self) -> None:
        cfg = PlaygroundConfig()
        assert cfg.auto_cleanup is True
        assert cfg.sandbox_prefix == ".bernstein-playground-"

    def test_custom(self) -> None:
        cfg = PlaygroundConfig(auto_cleanup=False, sandbox_prefix="sandbox-")
        assert cfg.auto_cleanup is False
        assert cfg.sandbox_prefix == "sandbox-"

    def test_frozen(self) -> None:
        cfg = PlaygroundConfig()
        with pytest.raises(AttributeError):
            cfg.auto_cleanup = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Persistence: save / load roundtrip
# ---------------------------------------------------------------------------


class TestSessionPersistence:
    """save_session / load_sessions roundtrip tests."""

    @staticmethod
    def _make_session(
        session_id: str = "sess-001",
        status: str = "active",
    ) -> PlaygroundSession:
        return PlaygroundSession(
            session_id=session_id,
            original_dir="/tmp/orig",
            sandbox_dir="/tmp/sandbox",
            created_at="2026-04-10T12:00:00Z",
            status=status,  # type: ignore[arg-type]
        )

    def test_roundtrip(self, tmp_path: Path) -> None:
        session = self._make_session()
        save_session(session, tmp_path)

        loaded = load_sessions(tmp_path)
        assert len(loaded) == 1
        assert loaded[0] == session

    def test_roundtrip_multiple(self, tmp_path: Path) -> None:
        s1 = self._make_session(session_id="aaa")
        s2 = self._make_session(session_id="bbb", status="applied")
        save_session(s1, tmp_path)
        save_session(s2, tmp_path)

        loaded = load_sessions(tmp_path)
        assert len(loaded) == 2
        ids = {s.session_id for s in loaded}
        assert ids == {"aaa", "bbb"}

    def test_load_empty_dir(self, tmp_path: Path) -> None:
        assert load_sessions(tmp_path) == []

    def test_load_nonexistent_dir(self, tmp_path: Path) -> None:
        assert load_sessions(tmp_path / "does-not-exist") == []

    def test_corrupt_file_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "bad.json").write_text("NOT JSON", encoding="utf-8")
        s1 = self._make_session(session_id="good")
        save_session(s1, tmp_path)

        loaded = load_sessions(tmp_path)
        assert len(loaded) == 1
        assert loaded[0].session_id == "good"

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c"
        session = self._make_session()
        save_session(session, nested)
        assert (nested / f"{session.session_id}.json").is_file()

    def test_json_format(self, tmp_path: Path) -> None:
        session = self._make_session()
        save_session(session, tmp_path)
        path = tmp_path / f"{session.session_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["session_id"] == session.session_id
        assert data["status"] == "active"


# ---------------------------------------------------------------------------
# list_active_sessions
# ---------------------------------------------------------------------------


class TestListActiveSessions:
    """list_active_sessions filters correctly."""

    def test_empty(self, tmp_path: Path) -> None:
        assert list_active_sessions(tmp_path) == []

    def test_filters_non_active(self, tmp_path: Path) -> None:
        sdir = tmp_path / ".sdd" / "playground"
        sdir.mkdir(parents=True)

        active = PlaygroundSession(
            session_id="a1",
            original_dir=str(tmp_path),
            sandbox_dir="/tmp/sb1",
            created_at="2026-04-10T00:00:00Z",
            status="active",
        )
        applied = PlaygroundSession(
            session_id="a2",
            original_dir=str(tmp_path),
            sandbox_dir="/tmp/sb2",
            created_at="2026-04-10T00:01:00Z",
            status="applied",
        )
        discarded = PlaygroundSession(
            session_id="a3",
            original_dir=str(tmp_path),
            sandbox_dir="/tmp/sb3",
            created_at="2026-04-10T00:02:00Z",
            status="discarded",
        )

        for s in (active, applied, discarded):
            save_session(s, sdir)

        result = list_active_sessions(tmp_path)
        assert len(result) == 1
        assert result[0].session_id == "a1"


# ---------------------------------------------------------------------------
# discard_sandbox cleanup
# ---------------------------------------------------------------------------


class TestDiscardSandbox:
    """discard_sandbox removes sandbox directory when present."""

    def test_discard_removes_directory(self, tmp_path: Path) -> None:
        original = tmp_path / "orig"
        original.mkdir()
        sdd = original / ".sdd" / "playground"
        sdd.mkdir(parents=True)

        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "file.txt").write_text("hello", encoding="utf-8")

        session = PlaygroundSession(
            session_id="d1",
            original_dir=str(original),
            sandbox_dir=str(sandbox),
            created_at="2026-04-10T00:00:00Z",
            status="active",
        )

        result = discard_sandbox(session)
        assert result is True
        assert not sandbox.exists()

    def test_discard_updates_status(self, tmp_path: Path) -> None:
        original = tmp_path / "orig"
        original.mkdir()
        sdd = original / ".sdd" / "playground"
        sdd.mkdir(parents=True)

        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()

        session = PlaygroundSession(
            session_id="d2",
            original_dir=str(original),
            sandbox_dir=str(sandbox),
            created_at="2026-04-10T00:00:00Z",
            status="active",
        )
        save_session(session, sdd)

        discard_sandbox(session)

        loaded = load_sessions(sdd)
        assert len(loaded) == 1
        assert loaded[0].status == "discarded"

    def test_discard_nonexistent_sandbox(self, tmp_path: Path) -> None:
        original = tmp_path / "orig"
        original.mkdir()
        sdd = original / ".sdd" / "playground"
        sdd.mkdir(parents=True)

        session = PlaygroundSession(
            session_id="d3",
            original_dir=str(original),
            sandbox_dir=str(tmp_path / "gone"),
            created_at="2026-04-10T00:00:00Z",
            status="active",
        )

        # Should succeed even if sandbox dir doesn't exist.
        result = discard_sandbox(session)
        assert result is True
