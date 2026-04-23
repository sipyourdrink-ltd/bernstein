"""Unit tests for CopilotAdapter."""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.copilot import CopilotAdapter

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _no_watchdog_threads() -> Generator[None, None, None]:
    """Disable watchdog threads to avoid 'can't start new thread' on CI."""
    with patch("bernstein.adapters.base.CLIAdapter._start_timeout_watchdog", return_value=None):
        yield


def _make_popen_mock(pid: int) -> MagicMock:
    mock = MagicMock(spec=subprocess.Popen)
    mock.pid = pid
    mock.wait.return_value = None
    return mock


def _inner_cmd(full_cmd: list[str]) -> list[str]:
    sep = full_cmd.index("--")
    return full_cmd[sep + 1 :]


def test_spawn_builds_run_command(tmp_path: Path) -> None:
    adapter = CopilotAdapter()
    proc_mock = _make_popen_mock(800)

    with patch("bernstein.adapters.copilot.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="copilot-s1",
        )

    cmd = popen.call_args.args[0]
    inner = _inner_cmd(cmd)
    assert inner[:3] == ["copilot", "--allow-all-tools", "-i"]
    assert inner[-1] == "fix the bug"


def test_spawn_translates_missing_cli(tmp_path: Path) -> None:
    adapter = CopilotAdapter()
    with (
        patch(
            "bernstein.adapters.copilot.subprocess.Popen",
            side_effect=FileNotFoundError("No such file"),
        ),
        pytest.raises(RuntimeError, match="copilot not found"),
    ):
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="copilot-missing",
        )


def test_name() -> None:
    assert CopilotAdapter().name() == "GitHub Copilot"
