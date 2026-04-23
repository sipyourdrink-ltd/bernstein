"""Unit tests for ForgeAdapter spawn and metadata."""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.forge import ForgeAdapter

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _no_watchdog_threads() -> Generator[None, None, None]:
    """Disable watchdog threads to avoid 'can't start new thread' on CI."""
    with patch("bernstein.adapters.base.CLIAdapter._start_timeout_watchdog", return_value=None):
        yield


def _make_popen_mock(pid: int) -> MagicMock:
    """Return a ``subprocess.Popen`` mock with the given PID."""
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    return m


def _inner_cmd(full_cmd: list[str]) -> list[str]:
    """Extract the actual CLI command after the '--' worker separator."""
    sep = full_cmd.index("--")
    return full_cmd[sep + 1 :]


class TestForgeAdapterSpawn:
    """ForgeAdapter.spawn() builds the correct command."""

    def test_spawn_builds_run_command(self, tmp_path: Path) -> None:
        adapter = ForgeAdapter()
        proc_mock = _make_popen_mock(pid=700)
        with patch("bernstein.adapters.forge.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="forge-s1",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner == ["forge", "-p", "fix the bug"]

    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = ForgeAdapter()
        with (
            patch(
                "bernstein.adapters.forge.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match=r"forge not found.*forgecode\.dev"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="forge-missing",
            )


class TestForgeAdapterName:
    """ForgeAdapter.name() returns the human-readable label."""

    def test_name(self) -> None:
        assert ForgeAdapter().name() == "Forge"
