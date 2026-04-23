"""Unit tests for MistralAdapter spawn/name."""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.mistral import MistralAdapter

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _no_watchdog_threads() -> Generator[None, None, None]:
    """Disable watchdog threads to avoid 'can't start new thread' on CI."""
    with patch("bernstein.adapters.base.CLIAdapter._start_timeout_watchdog", return_value=None):
        yield


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    return m


def _inner_cmd(full_cmd: list[str]) -> list[str]:
    """Extract the actual CLI command after the '--' worker separator."""
    sep = full_cmd.index("--")
    return full_cmd[sep + 1 :]


class TestMistralAdapterSpawn:
    """MistralAdapter.spawn() builds the expected command."""

    def test_spawn_builds_run_command(self, tmp_path: Path) -> None:
        adapter = MistralAdapter()
        proc_mock = _make_popen_mock(pid=700)
        with patch("bernstein.adapters.mistral.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="mistral-large", effort="high"),
                session_id="mistral-s1",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner == ["vibe", "--auto-approve", "--prompt", "fix the bug"]

    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = MistralAdapter()
        with (
            patch(
                "bernstein.adapters.mistral.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match=r"vibe not found.*mistral\.ai"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="mistral-large", effort="high"),
                session_id="mistral-missing",
            )


class TestMistralAdapterName:
    def test_name(self) -> None:
        assert MistralAdapter().name() == "Mistral Vibe"
