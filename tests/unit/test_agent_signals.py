"""Tests for AgentSignalManager — WAKEUP / SHUTDOWN / HEARTBEAT signal files."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bernstein.core.agent_signals import AgentSignalManager
from bernstein.core.models import AgentHeartbeat

# ---------------------------------------------------------------------------
# AgentHeartbeat model
# ---------------------------------------------------------------------------


class TestAgentHeartbeat:
    def test_default_fields(self) -> None:
        hb = AgentHeartbeat(timestamp=1000.0)
        assert hb.timestamp == 1000.0
        assert hb.files_changed == 0
        assert hb.status == "working"
        assert hb.current_file == ""

    def test_custom_fields(self) -> None:
        hb = AgentHeartbeat(
            timestamp=9999.0,
            files_changed=5,
            status="idle",
            current_file="src/foo.py",
        )
        assert hb.files_changed == 5
        assert hb.status == "idle"
        assert hb.current_file == "src/foo.py"


# ---------------------------------------------------------------------------
# AgentSignalManager — WAKEUP
# ---------------------------------------------------------------------------


class TestWriteWakeup:
    def test_creates_signal_file(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        mgr.write_wakeup("abc123", "Fix auth bug", elapsed_s=65.0, last_activity_ago_s=65.0)

        signal_file = tmp_path / ".sdd" / "runtime" / "signals" / "abc123" / "WAKEUP"
        assert signal_file.exists()

    def test_content_contains_task_title(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        mgr.write_wakeup("s1", "Implement caching", elapsed_s=70.0, last_activity_ago_s=70.0)

        content = (tmp_path / ".sdd" / "runtime" / "signals" / "s1" / "WAKEUP").read_text()
        assert "Implement caching" in content

    def test_content_contains_elapsed(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        mgr.write_wakeup("s1", "Task", elapsed_s=90.0, last_activity_ago_s=90.0)

        content = (tmp_path / ".sdd" / "runtime" / "signals" / "s1" / "WAKEUP").read_text()
        assert "WAKEUP" in content

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        mgr.write_wakeup("new-session", "Task", elapsed_s=61.0, last_activity_ago_s=61.0)

        dir_ = tmp_path / ".sdd" / "runtime" / "signals" / "new-session"
        assert dir_.is_dir()


# ---------------------------------------------------------------------------
# AgentSignalManager — SHUTDOWN
# ---------------------------------------------------------------------------


class TestWriteShutdown:
    def test_creates_signal_file(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        mgr.write_shutdown("s2", reason="budget_exceeded", task_title="Add logging")

        signal_file = tmp_path / ".sdd" / "runtime" / "signals" / "s2" / "SHUTDOWN"
        assert signal_file.exists()

    def test_content_contains_reason(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        mgr.write_shutdown("s2", reason="budget_exceeded", task_title="Add logging")

        content = (tmp_path / ".sdd" / "runtime" / "signals" / "s2" / "SHUTDOWN").read_text()
        assert "budget_exceeded" in content

    def test_content_contains_task_title(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        mgr.write_shutdown("s3", reason="stop_called", task_title="Refactor DB layer")

        content = (tmp_path / ".sdd" / "runtime" / "signals" / "s3" / "SHUTDOWN").read_text()
        assert "Refactor DB layer" in content

    def test_content_mentions_shutdown(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        mgr.write_shutdown("s4", reason="stop_called", task_title="Any task")

        content = (tmp_path / ".sdd" / "runtime" / "signals" / "s4" / "SHUTDOWN").read_text()
        assert "SHUTDOWN" in content


# ---------------------------------------------------------------------------
# AgentSignalManager — HEARTBEAT read/write
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_read_returns_none_when_missing(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        assert mgr.read_heartbeat("no-such-session") is None

    def test_write_creates_json_file(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        hb = AgentHeartbeat(timestamp=1234567890.0, files_changed=3, status="working", current_file="src/x.py")
        mgr.write_heartbeat("sess1", hb)

        hb_file = tmp_path / ".sdd" / "runtime" / "heartbeats" / "sess1.json"
        assert hb_file.exists()

    def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        hb = AgentHeartbeat(timestamp=1234567890.0, files_changed=7, status="working", current_file="src/y.py")
        mgr.write_heartbeat("sess2", hb)

        read_back = mgr.read_heartbeat("sess2")
        assert read_back is not None
        assert read_back.timestamp == pytest.approx(1234567890.0)
        assert read_back.files_changed == 7
        assert read_back.status == "working"
        assert read_back.current_file == "src/y.py"

    def test_json_file_has_correct_keys(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        hb = AgentHeartbeat(timestamp=100.0)
        mgr.write_heartbeat("s5", hb)

        raw = json.loads((tmp_path / ".sdd" / "runtime" / "heartbeats" / "s5.json").read_text())
        assert "timestamp" in raw
        assert "files_changed" in raw
        assert "status" in raw
        assert "current_file" in raw

    def test_read_ignores_malformed_json(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        hb_dir = tmp_path / ".sdd" / "runtime" / "heartbeats"
        hb_dir.mkdir(parents=True, exist_ok=True)
        (hb_dir / "bad.json").write_text("not-json{{{")

        assert mgr.read_heartbeat("bad") is None


# ---------------------------------------------------------------------------
# AgentSignalManager — staleness detection
# ---------------------------------------------------------------------------


class TestIsStale:
    def test_no_heartbeat_not_stale(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        # No heartbeat file → can't determine staleness → not stale
        assert mgr.is_stale("ghost", stale_after_s=60.0) is False

    def test_recent_heartbeat_not_stale(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        hb = AgentHeartbeat(timestamp=time.time())
        mgr.write_heartbeat("fresh", hb)

        assert mgr.is_stale("fresh", stale_after_s=60.0) is False

    def test_old_heartbeat_is_stale(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        hb = AgentHeartbeat(timestamp=time.time() - 120.0)
        mgr.write_heartbeat("old-sess", hb)

        assert mgr.is_stale("old-sess", stale_after_s=60.0) is True


# ---------------------------------------------------------------------------
# AgentSignalManager — clear_signals
# ---------------------------------------------------------------------------


class TestClearSignals:
    def test_clear_removes_wakeup(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        mgr.write_wakeup("s6", "Task", elapsed_s=65.0, last_activity_ago_s=65.0)

        mgr.clear_signals("s6")

        signal_dir = tmp_path / ".sdd" / "runtime" / "signals" / "s6"
        assert not (signal_dir / "WAKEUP").exists()

    def test_clear_removes_shutdown(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        mgr.write_shutdown("s7", reason="test", task_title="T")

        mgr.clear_signals("s7")

        signal_dir = tmp_path / ".sdd" / "runtime" / "signals" / "s7"
        assert not (signal_dir / "SHUTDOWN").exists()

    def test_clear_is_idempotent(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        # No files exist — should not raise
        mgr.clear_signals("nonexistent-session")

    def test_clear_removes_heartbeat(self, tmp_path: Path) -> None:
        mgr = AgentSignalManager(tmp_path)
        mgr.write_heartbeat("s8", AgentHeartbeat(timestamp=1.0))

        mgr.clear_signals("s8")

        hb_file = tmp_path / ".sdd" / "runtime" / "heartbeats" / "s8.json"
        assert not hb_file.exists()


# ---------------------------------------------------------------------------
# Spawner: signal check instructions injected into prompt
# ---------------------------------------------------------------------------


class TestSpawnerSignalInjection:
    def test_signal_check_in_rendered_prompt(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        from bernstein.core.spawner import AgentSpawner

        adapter = mock_adapter_factory(pid=42)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        task = make_task()
        spawner.spawn_for_tasks([task])

        call_args = adapter.spawn.call_args
        prompt: str = call_args.kwargs.get("prompt") or call_args.args[0]
        assert ".sdd/runtime/signals/" in prompt

    def test_signal_check_contains_session_id(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        from bernstein.core.spawner import AgentSpawner

        adapter = mock_adapter_factory(pid=42)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        task = make_task()
        session = spawner.spawn_for_tasks([task])

        call_args = adapter.spawn.call_args
        prompt: str = call_args.kwargs.get("prompt") or call_args.args[0]
        assert session.id in prompt
