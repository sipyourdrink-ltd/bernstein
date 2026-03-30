"""Tests for `bernstein test-adapter`."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from bernstein.cli.main import cli


def test_test_adapter_success(tmp_path: Path) -> None:
    adapter = MagicMock()
    adapter.spawn.return_value = SimpleNamespace(pid=123, log_path=tmp_path / "run.log", timeout_timer=None)
    adapter.kill.return_value = None

    runner = CliRunner()
    with patch("bernstein.cli.adapter_cmd.get_adapter", return_value=adapter):
        result = runner.invoke(cli, ["test-adapter", "codex", "--workdir", str(tmp_path)])

    assert result.exit_code == 0
    assert "Adapter OK" in result.output
    adapter.spawn.assert_called_once()
    adapter.kill.assert_called_once_with(123)


def test_test_adapter_failure_exits_nonzero(tmp_path: Path) -> None:
    adapter = MagicMock()
    adapter.spawn.side_effect = RuntimeError("spawn failed")

    runner = CliRunner()
    with patch("bernstein.cli.adapter_cmd.get_adapter", return_value=adapter):
        result = runner.invoke(cli, ["test-adapter", "gemini", "--workdir", str(tmp_path)])

    assert result.exit_code == 1
    assert "Adapter failed" in result.output
