"""Tests for `bernstein test-adapter`."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from bernstein.cli.main import cli

if TYPE_CHECKING:
    pass


def test_test_adapter_success(tmp_path: Path) -> None:
    adapter = MagicMock()
    # Mock return value for spawn
    proc_mock = MagicMock()
    proc_mock.wait.return_value = 0
    
    log_file = tmp_path / "test-session.log"
    log_file.write_text("All tests passed\nResult written to file test.txt\n", encoding="utf-8")
    
    result_mock = SimpleNamespace(
        pid=123, 
        log_path=log_file, 
        proc=proc_mock
    )
    adapter.spawn.return_value = result_mock
    
    runner = CliRunner()
    # Mock Path.cwd() to return tmp_path so worktree is created there
    with (
        patch("bernstein.cli.adapter_cmd.get_adapter", return_value=adapter),
        patch("bernstein.cli.adapter_cmd.Path.cwd", return_value=tmp_path),
        patch("bernstein.cli.adapter_cmd.CLIAdapter.cancel_timeout"),
    ):
        result = runner.invoke(cli, ["test-adapter", "--adapter", "gemini", "--task", "create file test.txt"])

    assert result.exit_code == 0
    assert "Testing adapter: gemini" in result.output
    assert "Exit code: 0" in result.output
    adapter.spawn.assert_called_once()


def test_test_adapter_timeout(tmp_path: Path) -> None:
    adapter = MagicMock()
    proc_mock = MagicMock()
    # Raise timeout on wait
    proc_mock.wait.side_effect = subprocess.TimeoutExpired(cmd="gemini", timeout=1)
    
    log_file = tmp_path / "timeout-session.log"
    log_file.touch()
    
    result_mock = SimpleNamespace(
        pid=123, 
        log_path=log_file, 
        proc=proc_mock
    )
    adapter.spawn.return_value = result_mock
    
    runner = CliRunner()
    with (
        patch("bernstein.cli.adapter_cmd.get_adapter", return_value=adapter),
        patch("bernstein.cli.adapter_cmd.Path.cwd", return_value=tmp_path),
        patch("bernstein.cli.adapter_cmd.CLIAdapter.cancel_timeout"),
    ):
        result = runner.invoke(cli, ["test-adapter", "--adapter", "gemini", "--task", "long task", "--timeout", "1"])

    assert "Timeout after 1s" in result.output
    assert "Exit code: timed out" in result.output
    adapter.kill.assert_called_once_with(123)


def test_test_adapter_failure_exits_nonzero(tmp_path: Path) -> None:
    adapter = MagicMock()
    adapter.spawn.side_effect = RuntimeError("spawn failed")

    runner = CliRunner()
    with (
        patch("bernstein.cli.adapter_cmd.get_adapter", return_value=adapter),
        patch("bernstein.cli.adapter_cmd.Path.cwd", return_value=tmp_path),
    ):
        result = runner.invoke(cli, ["test-adapter", "--adapter", "gemini", "--task", "fail"])

    assert result.exit_code == 1
    assert "Error during adapter test: spawn failed" in result.output
