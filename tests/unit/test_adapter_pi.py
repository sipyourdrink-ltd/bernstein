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


def pi_cmd(full_cmd: list[str]) -> list[str]:
    """The pi argv inside ``python -m bernstein.adapters.pi_json_launcher -- ...``."""
    inner = inner_cmd(full_cmd)
    assert inner[1:4] == ["-m", "bernstein.adapters.pi_json_launcher", "--"], inner
    return inner[4:]


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
        inner = pi_cmd(popen.call_args.args[0])
        assert inner == [
            "pi",
            "--no-extensions",
            "--mode",
            "json",
            "--model",
            "provider/model-name",
            "--",
            "fix the bug",
        ]

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
        inner = pi_cmd(popen.call_args.args[0])
        assert inner == ["pi", "--no-extensions", "--mode", "json", "--", "fix the bug"]

    def test_pi_spawn_includes_mcp_isolation_flags(self, tmp_path: Path) -> None:
        """Pi reaches MCP servers only through extensions, and it discovers the
        user's own from ~/.pi/agent/extensions/. A spawned agent must not load
        them, whatever MCP config the task carries (#5965)."""
        adapter = PiAdapter()
        proc_mock = make_popen_mock(pid=802)
        with patch("bernstein.adapters.pi.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="pi-s3",
                mcp_config={"mcpServers": {"bernstein": {"command": "bernstein-mcp"}}},
            )
        inner = pi_cmd(popen.call_args.args[0])
        assert "--no-extensions" in inner
        assert inner[-1] == "fix the bug"

    def test_spawn_streams_json_events_so_the_log_grows_while_pi_works(self, tmp_path: Path) -> None:
        """Redirected stdout makes pi fall back to print mode, which writes nothing
        until the run ends; the log is the only liveness signal pi gives, so an
        agent mid-turn was killed by heartbeat escalation (#6943)."""
        adapter = PiAdapter()
        proc_mock = make_popen_mock(pid=803)
        with patch("bernstein.adapters.pi.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="pi-s4",
            )
        inner = pi_cmd(popen.call_args.args[0])
        mode = inner.index("--mode")
        assert inner[mode + 1] == "json"
        assert "-p" not in inner
        assert "--print" not in inner
        assert mode < inner.index("--")

    def test_spawn_passes_a_dash_prompt_as_the_prompt(self, tmp_path: Path) -> None:
        """A task prompt that starts with ``-`` must not be parsed as a flag."""
        adapter = PiAdapter()
        proc_mock = make_popen_mock(pid=804)
        with patch("bernstein.adapters.pi.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="--model is not a flag here, fix the failing test",
                workdir=tmp_path,
                model_config=ModelConfig(model="provider/m", effort="high"),
                session_id="pi-s5",
            )
        inner = pi_cmd(popen.call_args.args[0])
        assert inner[-2:] == ["--", "--model is not a flag here, fix the failing test"]

    def test_spawn_runs_pi_under_the_exit_code_launcher(self, tmp_path: Path) -> None:
        """JSON mode exits 0 after a failed turn; the launcher restores text mode's exit code."""
        import sys

        adapter = PiAdapter()
        proc_mock = make_popen_mock(pid=805)
        with patch("bernstein.adapters.pi.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="pi-s6",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner[:4] == [sys.executable, "-m", "bernstein.adapters.pi_json_launcher", "--"]
        assert inner[4] == "pi"

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
