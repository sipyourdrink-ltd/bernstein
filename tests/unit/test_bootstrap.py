"""Tests for bernstein.core.bootstrap."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from bernstein.core.bootstrap import (
    SDD_DIRS,
    _clean_stale_runtime,
    _ensure_sdd,
    _is_alive,
    _read_pid,
    _start_server,
    _start_spawner,
    _wait_for_server,
)


# ---------------------------------------------------------------------------
# _read_pid
# ---------------------------------------------------------------------------

class TestReadPid:
    def test_returns_pid_from_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "server.pid"
        pid_file.write_text("12345")
        assert _read_pid(pid_file) == 12345

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert _read_pid(tmp_path / "nonexistent.pid") is None

    def test_returns_none_for_invalid_content(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "bad.pid"
        pid_file.write_text("not-a-number")
        assert _read_pid(pid_file) is None

    def test_returns_none_for_empty_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "empty.pid"
        pid_file.write_text("")
        assert _read_pid(pid_file) is None


# ---------------------------------------------------------------------------
# _is_alive
# ---------------------------------------------------------------------------

class TestIsAlive:
    def test_alive_process_returns_true(self) -> None:
        # os.getpid() is guaranteed to be alive
        assert _is_alive(os.getpid()) is True

    def test_dead_process_returns_false(self) -> None:
        # PID 0 is never a real user process; sending signal 0 raises OSError
        with patch("os.kill", side_effect=OSError):
            assert _is_alive(99999999) is False


# ---------------------------------------------------------------------------
# _clean_stale_runtime
# ---------------------------------------------------------------------------

class TestCleanStaleRuntime:
    def test_no_runtime_dir_is_noop(self, tmp_path: Path) -> None:
        # Should not raise even when .sdd/runtime does not exist
        _clean_stale_runtime(tmp_path)

    def test_removes_stale_pid_file(self, tmp_path: Path) -> None:
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        pid_file = runtime / "server.pid"
        pid_file.write_text("999999999")  # almost certainly dead

        with patch("bernstein.core.bootstrap._is_alive", return_value=False):
            _clean_stale_runtime(tmp_path)

        assert not pid_file.exists()

    def test_keeps_alive_pid_file(self, tmp_path: Path) -> None:
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        pid_file = runtime / "server.pid"
        pid_file.write_text(str(os.getpid()))

        with patch("bernstein.core.bootstrap._is_alive", return_value=True):
            _clean_stale_runtime(tmp_path)

        assert pid_file.exists()

    def test_removes_log_files(self, tmp_path: Path) -> None:
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        log = runtime / "server.log"
        log.write_text("old log")

        _clean_stale_runtime(tmp_path)

        assert not log.exists()

    def test_removes_tasks_jsonl(self, tmp_path: Path) -> None:
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        jsonl = runtime / "tasks.jsonl"
        jsonl.write_text('{"id":"t1"}\n')

        _clean_stale_runtime(tmp_path)

        assert not jsonl.exists()

    def test_pid_file_with_invalid_content_is_removed(self, tmp_path: Path) -> None:
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        pid_file = runtime / "spawner.pid"
        pid_file.write_text("garbage")

        _clean_stale_runtime(tmp_path)

        # _read_pid returns None for invalid content → treated as stale → removed
        assert not pid_file.exists()


# ---------------------------------------------------------------------------
# _ensure_sdd
# ---------------------------------------------------------------------------

class TestEnsureSdd:
    def test_creates_all_sdd_dirs(self, tmp_path: Path) -> None:
        _ensure_sdd(tmp_path)
        for d in SDD_DIRS:
            assert (tmp_path / d).is_dir(), f"Missing {d}"

    def test_writes_default_config(self, tmp_path: Path) -> None:
        _ensure_sdd(tmp_path)
        config = (tmp_path / ".sdd" / "config.yaml").read_text()
        assert "server_port: 8052" in config
        assert "max_workers: 4" in config

    def test_writes_gitignore(self, tmp_path: Path) -> None:
        _ensure_sdd(tmp_path)
        gi = (tmp_path / ".sdd" / "runtime" / ".gitignore").read_text()
        assert "*.pid" in gi
        assert "*.log" in gi

    def test_returns_true_when_newly_created(self, tmp_path: Path) -> None:
        assert _ensure_sdd(tmp_path) is True

    def test_returns_false_when_already_exists(self, tmp_path: Path) -> None:
        _ensure_sdd(tmp_path)
        assert _ensure_sdd(tmp_path) is False

    def test_does_not_overwrite_existing_config(self, tmp_path: Path) -> None:
        _ensure_sdd(tmp_path)
        config_path = tmp_path / ".sdd" / "config.yaml"
        config_path.write_text("custom: true\n")
        _ensure_sdd(tmp_path)
        assert config_path.read_text() == "custom: true\n"


# ---------------------------------------------------------------------------
# _start_server
# ---------------------------------------------------------------------------

class TestStartServer:
    def _setup_runtime(self, workdir: Path) -> None:
        (workdir / ".sdd" / "runtime").mkdir(parents=True)

    def test_spawns_uvicorn_process(self, tmp_path: Path) -> None:
        self._setup_runtime(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 42

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            pid = _start_server(tmp_path, port=8052)

        assert pid == 42
        args = mock_popen.call_args[0][0]
        assert "uvicorn" in args

    def test_writes_pid_file(self, tmp_path: Path) -> None:
        self._setup_runtime(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 1234

        with patch("subprocess.Popen", return_value=mock_proc):
            _start_server(tmp_path, port=8052)

        pid_file = tmp_path / ".sdd" / "runtime" / "server.pid"
        assert pid_file.read_text() == "1234"

    def test_raises_if_server_already_running(self, tmp_path: Path) -> None:
        self._setup_runtime(tmp_path)
        pid_file = tmp_path / ".sdd" / "runtime" / "server.pid"
        pid_file.write_text(str(os.getpid()))

        with patch("bernstein.core.bootstrap._is_alive", return_value=True):
            with pytest.raises(RuntimeError, match="already running"):
                _start_server(tmp_path, port=8052)

    def test_uses_specified_port(self, tmp_path: Path) -> None:
        self._setup_runtime(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 99

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            _start_server(tmp_path, port=9999)

        args = mock_popen.call_args[0][0]
        assert "9999" in args


# ---------------------------------------------------------------------------
# _wait_for_server
# ---------------------------------------------------------------------------

class TestWaitForServer:
    def test_returns_true_when_server_responds(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.get", return_value=mock_resp):
            with patch("time.sleep"):
                result = _wait_for_server(8052)

        assert result is True

    def test_returns_false_on_timeout(self) -> None:
        # Always raise ConnectError, simulate time advancing past deadline
        call_count = 0

        def fake_monotonic() -> float:
            nonlocal call_count
            call_count += 1
            # First call: deadline setup; subsequent calls: already past deadline
            return 0.0 if call_count == 1 else 999.0

        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            with patch("time.sleep"):
                with patch("time.monotonic", side_effect=fake_monotonic):
                    result = _wait_for_server(8052)

        assert result is False

    def test_retries_on_connect_error_then_succeeds(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        side_effects = [httpx.ConnectError("refused"), mock_resp]

        monotonic_values = iter([0.0, 1.0, 2.0, 100.0])

        with patch("httpx.get", side_effect=side_effects):
            with patch("time.sleep"):
                with patch("time.monotonic", side_effect=monotonic_values):
                    result = _wait_for_server(8052)

        assert result is True


# ---------------------------------------------------------------------------
# _start_spawner
# ---------------------------------------------------------------------------

class TestStartSpawner:
    def _setup_runtime(self, workdir: Path) -> None:
        (workdir / ".sdd" / "runtime").mkdir(parents=True)

    def test_spawns_orchestrator_process(self, tmp_path: Path) -> None:
        self._setup_runtime(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 77

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            pid = _start_spawner(tmp_path, port=8052)

        assert pid == 77
        args = mock_popen.call_args[0][0]
        assert "orchestrator" in " ".join(args)

    def test_writes_spawner_pid_file(self, tmp_path: Path) -> None:
        self._setup_runtime(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 555

        with patch("subprocess.Popen", return_value=mock_proc):
            _start_spawner(tmp_path, port=8052)

        pid_file = tmp_path / ".sdd" / "runtime" / "spawner.pid"
        assert pid_file.read_text() == "555"
