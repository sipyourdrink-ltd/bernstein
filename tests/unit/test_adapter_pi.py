"""Unit tests for PiAdapter."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.pi import PiAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


class TestPiAdapterSpawn:
    """PiAdapter.spawn() builds correct command."""

    def test_spawn_builds_run_command(self, tmp_path: Path) -> None:
        adapter = PiAdapter()
        proc_mock = make_popen_mock(pid=800)
        with patch("bernstein.adapters.pi.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="provider/model-name", effort="high"),
                session_id="pi-s1",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner == ["pi", "-ne", "--model", "provider/model-name", "fix the bug"]

    def test_spawn_leaves_model_selection_to_pi_for_auto(self, tmp_path: Path) -> None:
        adapter = PiAdapter()
        proc_mock = make_popen_mock(pid=801)
        with patch("bernstein.adapters.pi.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="pi-s2",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner == ["pi", "-ne", "fix the bug"]

    def test_pi_spawn_includes_mcp_isolation_flags(self, tmp_path: Path) -> None:
        """A spawned session must not inherit the operator's user-global MCP servers.

        Without `-ne` every spawn launches a private instance of every server the
        operator has installed -- dozens of background processes across concurrent
        runs -- and fills the agent's context with tools the task never asked for
        (#5965). It has to precede the positional prompt, which `pi` reads as the
        first non-flag argument.
        """
        adapter = PiAdapter()
        proc_mock = make_popen_mock(pid=802)
        with patch("bernstein.adapters.pi.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="pi-s3",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert "-ne" in inner
        assert inner.index("-ne") < inner.index("fix the bug")

    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = PiAdapter()
        with (
            patch(
                "bernstein.adapters.pi.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="pi not found") as excinfo,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="pi-missing",
            )
        assert "npm install -g @mariozechner/pi-coding-agent" in str(excinfo.value)


class TestPiAdapterName:
    def test_name(self) -> None:
        assert PiAdapter().name() == "Pi"
