"""Unit tests for PiAdapter."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.pi import PiAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from typing import Any


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
        assert inner == [
            "pi",
            "--no-extensions",
            "--print",
            "--mode",
            "json",
            "--model",
            "provider/model-name",
            "fix the bug",
        ]
        assert popen.call_args.kwargs["stdin"] == subprocess.DEVNULL

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
        assert inner == ["pi", "--no-extensions", "--print", "--mode", "json", "fix the bug"]
        assert popen.call_args.kwargs["stdin"] == subprocess.DEVNULL

    def test_spawn_completes_with_parent_stdin_pipe_open(self, tmp_path: Path) -> None:
        """The real worker must pass EOF to a CLI that reads stdin before emitting."""
        real_popen = subprocess.Popen
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as pipe_reader, os.fdopen(write_fd, "wb") as pipe_writer:

            def launch_stdin_reader(cmd: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
                # Stand in for Pi's pre-output stdin read; keep the real worker.
                cli_start = cmd.index("--") + 1
                cmd = [
                    *cmd[:cli_start],
                    sys.executable,
                    "-c",
                    "import sys; assert sys.stdin.read() == ''; print('{\"type\":\"agent_start\"}', flush=True)",
                    *cmd[cli_start + 1 :],
                ]
                # Model an inherited, open pipe when the adapter omits stdin.
                kwargs.setdefault("stdin", pipe_reader)
                return real_popen(cmd, **kwargs)

            env = {
                "PATH": os.defpath,
                "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "TMPDIR": str(tmp_path),
            }
            with (
                patch("bernstein.adapters.pi.build_filtered_env", return_value=env),
                patch("bernstein.adapters.pi.subprocess.Popen", side_effect=launch_stdin_reader),
            ):
                result = PiAdapter().spawn(
                    prompt="hello",
                    workdir=tmp_path,
                    model_config=ModelConfig(model="auto", effort="high"),
                    session_id="pi-open-stdin",
                    timeout_seconds=0,
                )
            proc = result.proc
            assert isinstance(proc, real_popen)
            try:
                assert proc.wait(timeout=10) == 0
                assert not pipe_writer.closed
                assert '{"type":"agent_start"}' in result.log_path.read_text()
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)

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
        inner = inner_cmd(popen.call_args.args[0])
        assert "--no-extensions" in inner
        assert inner[-1] == "fix the bug"

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

    def test_spawn_permission_error_does_not_retry_interactively(self, tmp_path: Path) -> None:
        adapter = PiAdapter()
        with (
            patch(
                "bernstein.adapters.pi.subprocess.Popen",
                side_effect=PermissionError("not executable"),
            ) as popen,
            pytest.raises(RuntimeError, match="Permission denied executing pi"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="pi-denied",
            )
        popen.assert_called_once()


class TestPiAdapterName:
    def test_name(self) -> None:
        assert PiAdapter().name() == "Pi"
