"""Tests for in-process agent backend — mocks adapter and subprocess."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bernstein.adapters.base import SpawnResult
from bernstein.core.in_process_agent import (
    InProcessAgent,
    _next_pid,  # type: ignore[reportPrivateUsage]
    _wait_on_pid,  # type: ignore[reportPrivateUsage]
)
from bernstein.core.models import ModelConfig


def _make_model_config() -> ModelConfig:
    return ModelConfig(model="sonnet", effort="high")


def _make_spawn_result(pid: int, log_path: Path) -> SpawnResult:
    return SpawnResult(pid=pid, log_path=log_path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def mock_adapter(workdir: Path) -> MagicMock:
    adapter = MagicMock()
    adapter.name.return_value = "test-adapter"
    adapter.spawn.return_value = _make_spawn_result(
        pid=42,
        log_path=workdir / ".sdd" / "logs" / "test.log",
    )
    return adapter


# ---------------------------------------------------------------------------
# TestInProcessAgent
# ---------------------------------------------------------------------------


class TestInProcessAgent:
    """Tests for the InProcessAgent class."""

    def test_name_returns_wrapped_adapter_name(self, mock_adapter: MagicMock, workdir: Path) -> None:
        backend = InProcessAgent(mock_adapter, workdir)
        assert backend.name() == "in-process:test-adapter"

    def test_run_starts_thread(self, mock_adapter: MagicMock, workdir: Path) -> None:
        backend = InProcessAgent(mock_adapter, workdir)
        pid, log_path = backend.run(
            prompt="hello",
            workdir=workdir,
            model_config=_make_model_config(),
            session_id="test-abc",
        )
        assert pid > 0
        assert log_path.name == "test-abc.log"
        backend.cleanup("test-abc")

    def test_is_alive_true_while_running(self, mock_adapter: MagicMock, workdir: Path) -> None:
        # Make the adapter spawn block so we can check is_alive
        spawn_started = threading.Event()
        spawn_done = threading.Event()

        def _blocking_spawn(**kwargs: object) -> SpawnResult:
            spawn_started.set()
            spawn_done.wait(timeout=5)
            return _make_spawn_result(pid=42, log_path=workdir / ".sdd" / "logs" / "test.log")

        mock_adapter.spawn.side_effect = _blocking_spawn

        backend = InProcessAgent(mock_adapter, workdir)
        backend.run(
            prompt="hello",
            workdir=workdir,
            model_config=_make_model_config(),
            session_id="test-abc",
        )

        spawn_started.wait(timeout=2)
        assert backend.is_alive("test-abc") is True

        spawn_done.set()  # unblock the thread
        backend.cleanup("test-abc")

    def test_is_alive_false_after_completion(self, mock_adapter: MagicMock, workdir: Path) -> None:
        backend = InProcessAgent(mock_adapter, workdir)
        backend.run(
            prompt="hello",
            workdir=workdir,
            model_config=_make_model_config(),
            session_id="test-done",
        )
        # Wait for the thread to finish
        time.sleep(0.2)
        assert backend.is_alive("test-done") is False
        backend.cleanup("test-done")

    def test_is_alive_false_for_unknown_session(self, mock_adapter: MagicMock, workdir: Path) -> None:
        backend = InProcessAgent(mock_adapter, workdir)
        assert backend.is_alive("nonexistent") is False

    def test_wait_returns_exit_code_after_completion(self, mock_adapter: MagicMock, workdir: Path) -> None:
        backend = InProcessAgent(mock_adapter, workdir)
        backend.run(
            prompt="hello",
            workdir=workdir,
            model_config=_make_model_config(),
            session_id="test-wait",
        )
        exit_code = backend.wait("test-wait", timeout=5.0)
        assert exit_code == 0
        backend.cleanup("test-wait")

    def test_wait_returns_none_for_unknown_session(self, mock_adapter: MagicMock, workdir: Path) -> None:
        backend = InProcessAgent(mock_adapter, workdir)
        assert backend.wait("nonexistent") is None

    def test_cleanup_removes_session(self, mock_adapter: MagicMock, workdir: Path) -> None:
        backend = InProcessAgent(mock_adapter, workdir)
        backend.run(
            prompt="hello",
            workdir=workdir,
            model_config=_make_model_config(),
            session_id="test-cleanup",
        )
        backend.cleanup("test-cleanup")
        assert backend.is_alive("test-cleanup") is False

    def test_stop_writes_signal_file(self, mock_adapter: MagicMock, workdir: Path) -> None:
        # Ensure the signal path base dir exists
        (workdir / ".sdd" / "runtime").mkdir(parents=True, exist_ok=True)

        backend = InProcessAgent(mock_adapter, workdir)
        backend.run(
            prompt="hello",
            workdir=workdir,
            model_config=_make_model_config(),
            session_id="test-sig",
        )
        backend.stop("test-sig")

        signal_file = workdir / ".sdd" / "runtime" / "signals" / "test-sig" / "SHUTDOWN"
        assert signal_file.exists()
        backend.cleanup("test-sig")

    def test_stop_sets_flag_on_session(self, mock_adapter: MagicMock, workdir: Path) -> None:
        backend = InProcessAgent(mock_adapter, workdir)
        backend.run(
            prompt="hello",
            workdir=workdir,
            model_config=_make_model_config(),
            session_id="test-stop-flag",
        )
        backend.stop("test-stop-flag")
        with backend._lock:  # type: ignore[reportPrivateUsage]
            session = backend._sessions.get("test-stop-flag")  # type: ignore[reportPrivateUsage]
            assert session is not None
            assert session.stop_requested is True
        backend.cleanup("test-stop-flag")

    def test_pid_file_written(self, mock_adapter: MagicMock, workdir: Path) -> None:
        backend = InProcessAgent(mock_adapter, workdir, pid_dir=workdir / "pids")
        backend.run(
            prompt="hello",
            workdir=workdir,
            model_config=_make_model_config(),
            session_id="test-pidfile",
        )
        pid_file = workdir / "pids" / "test-pidfile.json"
        assert pid_file.exists()

        data = json.loads(pid_file.read_text(encoding="utf-8"))
        assert data["backend"] == "in_process"
        assert data["session"] == "test-pidfile"
        backend.cleanup("test-pidfile")

    def test_active_sessions_returns_copy(self, mock_adapter: MagicMock, workdir: Path) -> None:
        backend = InProcessAgent(mock_adapter, workdir)
        sessions_before = backend.active_sessions
        # Mutating the returned dict should not affect the internal state
        sessions_before["fake"] = MagicMock()
        assert "fake" not in backend.active_sessions

    def test_active_sessions_empty_initially(self, mock_adapter: MagicMock, workdir: Path) -> None:
        backend = InProcessAgent(mock_adapter, workdir)
        assert backend.active_sessions == {}

    def test_stop_unknown_session_noop(self, mock_adapter: MagicMock, workdir: Path) -> None:
        backend = InProcessAgent(mock_adapter, workdir)
        backend.stop("nonexistent")  # should not raise

    def test_cleanup_unknown_session_noop(self, mock_adapter: MagicMock, workdir: Path) -> None:
        backend = InProcessAgent(mock_adapter, workdir)
        backend.cleanup("nonexistent")  # should not raise


# ---------------------------------------------------------------------------
# Test_next_pid
# ---------------------------------------------------------------------------


class TestNextPid:
    """Tests for the synthetic PID generator."""

    def test_returns_monotonic_increasing_values(self) -> None:
        pids = [_next_pid() for _ in range(10)]
        for i in range(1, len(pids)):
            assert pids[i] > pids[i - 1]

    def test_starts_above_10000(self) -> None:
        # Reset to a fresh state and verify first value
        import bernstein.core.in_process_agent as mod

        with mod._next_pid_lock:  # type: ignore[reportPrivateUsage]
            old_counter: int = mod._next_pid_counter  # type: ignore[reportPrivateUsage]
            mod._next_pid_counter = 10000  # type: ignore[reportPrivateUsage]
        try:
            # _next_pid returns 10000 then increments to 10001
            assert _next_pid() == 10000
            assert _next_pid() > 10000
        finally:
            with mod._next_pid_lock:  # type: ignore[reportPrivateUsage]
                mod._next_pid_counter = old_counter  # type: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Test_wait_on_pid
# ---------------------------------------------------------------------------


class TestWaitOnPid:
    """Tests for the PID wait helper."""

    def test_returns_zero_for_nonexistent_pid(self) -> None:
        # PID 1 on macOS: we can't wait on it (not our child), returns 0
        result = _wait_on_pid(999999, timeout=0.1)
        assert result == 0


# ---------------------------------------------------------------------------
# TestInProcessAgentWithMockedProc
# ---------------------------------------------------------------------------


class TestInProcessAgentWithMockedProc:
    """Tests where the adapter returns a SpawnResult with a real proc mock."""

    def test_wait_returns_proc_exit_code(self, mock_adapter: MagicMock, workdir: Path) -> None:
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0

        mock_adapter.spawn.return_value = SpawnResult(
            pid=42,
            log_path=workdir / ".sdd" / "logs" / "test.log",
            proc=mock_proc,
        )

        backend = InProcessAgent(mock_adapter, workdir)
        backend.run(
            prompt="hello",
            workdir=workdir,
            model_config=_make_model_config(),
            session_id="test-proc",
        )

        exit_code = backend.wait("test-proc", timeout=5.0)
        assert exit_code == 0
        mock_proc.wait.assert_called_once()
        backend.cleanup("test-proc")

    def test_error_records_failure(self, mock_adapter: MagicMock, workdir: Path) -> None:
        mock_adapter.spawn.side_effect = RuntimeError("adapter crashed")

        backend = InProcessAgent(mock_adapter, workdir)
        backend.run(
            prompt="hello",
            workdir=workdir,
            model_config=_make_model_config(),
            session_id="test-error",
        )

        exit_code = backend.wait("test-error", timeout=5.0)
        assert exit_code == 1

        with backend._lock:  # type: ignore[reportPrivateUsage]
            session = backend._sessions.get("test-error")  # type: ignore[reportPrivateUsage]
            assert session is not None
            assert "adapter crashed" in session.error_detail

        backend.cleanup("test-error")

    def test_systemexit_is_trapped(self, mock_adapter: MagicMock, workdir: Path) -> None:
        mock_adapter.spawn.side_effect = SystemExit(42)

        backend = InProcessAgent(mock_adapter, workdir)
        backend.run(
            prompt="hello",
            workdir=workdir,
            model_config=_make_model_config(),
            session_id="test-systemexit",
        )

        exit_code = backend.wait("test-systemexit", timeout=5.0)
        assert exit_code == 42
        backend.cleanup("test-systemexit")
