"""Focused tests for agent IPC helpers."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from bernstein.core.agent_ipc import (
    _stdin_pipes,
    broadcast_message,
    has_stdin_pipe,
    register_stdin_pipe,
    send_message,
    shutdown_all,
    unregister_stdin_pipe,
)


def test_register_and_unregister_stdin_pipe_toggle_registry() -> None:
    """register_stdin_pipe and unregister_stdin_pipe maintain the pipe registry."""
    _stdin_pipes.clear()
    pipe = MagicMock()

    register_stdin_pipe("A-1", pipe)
    assert has_stdin_pipe("A-1") is True

    unregister_stdin_pipe("A-1")
    assert has_stdin_pipe("A-1") is False


def test_send_message_writes_json_payload_to_pipe() -> None:
    """send_message serializes the IPC message as one JSON line to the registered pipe."""
    _stdin_pipes.clear()
    pipe = MagicMock()
    register_stdin_pipe("A-1", pipe)

    assert send_message("A-1", "hello") is True
    pipe.write.assert_called_once()
    pipe.flush.assert_called_once()


def test_send_message_unregisters_pipe_after_broken_pipe() -> None:
    """send_message returns false and drops the pipe when the write fails."""
    _stdin_pipes.clear()
    pipe = MagicMock()
    pipe.write.side_effect = BrokenPipeError("gone")
    register_stdin_pipe("A-1", pipe)

    assert send_message("A-1", "hello") is False
    assert has_stdin_pipe("A-1") is False


def test_broadcast_message_uses_pipe_first_and_file_fallback(tmp_path: Path) -> None:
    """broadcast_message delivers to pipe-backed agents first, then to signal dirs without pipes."""
    _stdin_pipes.clear()
    signals = tmp_path / ".sdd" / "runtime" / "signals"
    (signals / "A-2").mkdir(parents=True)
    pipe = MagicMock()
    register_stdin_pipe("A-1", pipe)

    with patch("bernstein.core.agents.agent_signals.AgentSignalManager") as mock_mgr:
        mock_mgr.return_value.write_command_signal.return_value = True
        result = broadcast_message("wake up", workdir=tmp_path)

    assert result == {"A-1": "pipe", "A-2": "file"}


def test_shutdown_all_wraps_broadcast_with_shutdown_message() -> None:
    """shutdown_all delegates to broadcast_message with a shutdown-prefixed instruction."""
    with patch("bernstein.core.agents.agent_ipc.broadcast_message", return_value={"A-1": "pipe"}) as mock_broadcast:
        result = shutdown_all("maintenance", workdir=Path("/tmp/work"))

    assert result == {"A-1": "pipe"}
    assert "SHUTDOWN: maintenance" in mock_broadcast.call_args.args[0]
