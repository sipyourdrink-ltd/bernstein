"""Focused tests for server supervisor restart and health logic."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from bernstein.core import server_supervisor


def _state(tmp_path: Path) -> server_supervisor._SupervisorState:
    """Build a supervisor state rooted in a temporary workspace."""
    (tmp_path / ".sdd" / "runtime").mkdir(parents=True, exist_ok=True)
    return server_supervisor._SupervisorState(
        workdir=tmp_path,
        port=8052,
        bind_host="127.0.0.1",
        cluster_enabled=False,
        auth_token=None,
        evolve_mode=False,
    )


def test_supervised_server_launches_server_and_threads(tmp_path: Path) -> None:
    """supervised_server starts the server once and launches monitor threads."""
    thread = MagicMock()
    thread.start.return_value = None

    with (
        patch("bernstein.core.server.server_supervisor._launch_server", return_value=101) as mock_launch,
        patch("bernstein.core.server.server_supervisor.write_supervisor_state") as mock_write,
        patch("bernstein.core.server.server_supervisor.threading.Thread", return_value=thread) as mock_thread,
    ):
        pid = server_supervisor.supervised_server(tmp_path, 8052)

    assert pid == 101
    mock_launch.assert_called_once()
    mock_write.assert_called_once()
    assert mock_thread.call_count == 2
    assert thread.start.call_count == 2


def test_launch_server_writes_pid_and_uses_expected_command(tmp_path: Path) -> None:
    """_launch_server writes server.pid and builds the uvicorn command correctly."""
    state = _state(tmp_path)
    proc = SimpleNamespace(pid=222)

    with (
        patch("bernstein.core.server.server_supervisor.rotate_log_file") as mock_rotate,
        patch("bernstein.core.server.server_supervisor.write_supervisor_state") as mock_write,
        patch("bernstein.core.server.server_supervisor.subprocess.Popen", return_value=proc) as mock_popen,
    ):
        pid = server_supervisor._launch_server(state)

    assert pid == 222
    assert (tmp_path / ".sdd" / "runtime" / "server.pid").read_text(encoding="utf-8") == "222"
    mock_rotate.assert_called_once_with(tmp_path / ".sdd" / "runtime" / "server.log")
    popen_args = mock_popen.call_args.args[0]
    assert popen_args[:3] == [server_supervisor.sys.executable, "-m", "uvicorn"]
    assert "--host" in popen_args
    assert "--port" in popen_args
    mock_write.assert_called()


def test_supervisor_loop_restarts_dead_server_with_backoff(tmp_path: Path) -> None:
    """_supervisor_loop restarts a dead server after the exponential backoff delay."""
    state = _state(tmp_path)
    state.current_pid = 100

    def _fake_launch(current: server_supervisor._SupervisorState) -> int:
        current.stopped = True
        return 200

    with (
        patch("bernstein.core.server.server_supervisor._is_alive", return_value=False),
        patch("bernstein.core.server.server_supervisor._launch_server", side_effect=_fake_launch) as mock_launch,
        patch("bernstein.core.server.server_supervisor.time.sleep") as mock_sleep,
        patch("bernstein.core.server.server_supervisor.write_supervisor_state"),
    ):
        server_supervisor._supervisor_loop(state)

    assert state.restart_count == 1
    assert state.current_pid == 200
    mock_launch.assert_called_once()
    assert mock_sleep.call_args_list[:2] == [call(1), call(2)]


def test_supervisor_loop_stops_after_restart_budget_is_exhausted(tmp_path: Path) -> None:
    """_supervisor_loop gives up once the restart budget within the time window is exhausted."""
    state = _state(tmp_path)
    state.current_pid = 100
    now = 1000.0
    state.restart_timestamps = [now - 1] * server_supervisor.MAX_RESTARTS

    with (
        patch("bernstein.core.server.server_supervisor._is_alive", return_value=False),
        patch("bernstein.core.server.server_supervisor.time.sleep"),
        patch("bernstein.core.server.server_supervisor.time.monotonic", return_value=now),
        patch("bernstein.core.server.server_supervisor._launch_server") as mock_launch,
    ):
        server_supervisor._supervisor_loop(state)

    assert state.stopped is True
    mock_launch.assert_not_called()


def test_health_check_loop_kills_unresponsive_server_after_consecutive_failures(tmp_path: Path) -> None:
    """_health_check_loop sends SIGTERM and SIGKILL after repeated failed health checks."""
    state = _state(tmp_path)
    state.current_pid = 4321

    def _kill(pid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            state.stopped = True

    with (
        patch("httpx.get", side_effect=RuntimeError("down")),
        patch("bernstein.core.server.server_supervisor.time.sleep"),
        patch("bernstein.core.server.server_supervisor._is_alive", return_value=True),
        patch("bernstein.core.server.server_supervisor.os.kill", side_effect=_kill) as mock_kill,
    ):
        with patch.object(server_supervisor, "MAX_CONSECUTIVE_FAILURES", 1):
            server_supervisor._health_check_loop(state)

    assert mock_kill.call_args_list == [
        call(4321, signal.SIGTERM),
        call(4321, signal.SIGKILL),
    ]
