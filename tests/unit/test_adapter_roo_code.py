"""Unit tests for RooCodeAdapter spawn/kill/is_alive."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.models import ModelConfig


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    return m


def _inner_cmd(full_cmd: list[str]) -> list[str]:
    """Extract the actual CLI command after the '--' worker separator."""
    sep = full_cmd.index("--")
    return full_cmd[sep + 1 :]


# ---------------------------------------------------------------------------
# RooCodeAdapter
# ---------------------------------------------------------------------------


class TestRooCodeAdapterSpawn:
    """RooCodeAdapter.spawn() builds correct command."""

    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        from bernstein.adapters.roo_code import RooCodeAdapter

        adapter = RooCodeAdapter()
        proc_mock = _make_popen_mock(pid=500)
        with patch("bernstein.adapters.roo_code.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="roo1",
            )
        cmd = popen.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.worker"]
        inner = _inner_cmd(cmd)
        assert inner[0] == "roo-code"

    def test_task_flag(self, tmp_path: Path) -> None:
        from bernstein.adapters.roo_code import RooCodeAdapter

        adapter = RooCodeAdapter()
        proc_mock = _make_popen_mock(pid=501)
        with patch("bernstein.adapters.roo_code.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix-the-bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="roo2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--task" in inner
        assert inner[inner.index("--task") + 1] == "fix-the-bug"

    def test_model_flag(self, tmp_path: Path) -> None:
        from bernstein.adapters.roo_code import RooCodeAdapter

        adapter = RooCodeAdapter()
        proc_mock = _make_popen_mock(pid=502)
        with patch("bernstein.adapters.roo_code.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="roo3",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--model" in inner
        # sonnet should map to a Claude sonnet model ID
        model_val = inner[inner.index("--model") + 1]
        assert "sonnet" in model_val.lower()

    def test_model_opus_mapping(self, tmp_path: Path) -> None:
        from bernstein.adapters.roo_code import RooCodeAdapter

        adapter = RooCodeAdapter()
        proc_mock = _make_popen_mock(pid=503)
        with patch("bernstein.adapters.roo_code.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="opus", effort="high"),
                session_id="roo4",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        model_val = inner[inner.index("--model") + 1]
        assert "opus" in model_val.lower()

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        from bernstein.adapters.roo_code import RooCodeAdapter

        adapter = RooCodeAdapter()
        proc_mock = _make_popen_mock(pid=504)
        with patch("bernstein.adapters.roo_code.subprocess.Popen", return_value=proc_mock):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="roo5",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_spawn_result_pid(self, tmp_path: Path) -> None:
        from bernstein.adapters.roo_code import RooCodeAdapter

        adapter = RooCodeAdapter()
        proc_mock = _make_popen_mock(pid=505)
        with patch("bernstein.adapters.roo_code.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="roo6",
            )
        assert result.pid == 505

    def test_log_path_uses_session_id(self, tmp_path: Path) -> None:
        from bernstein.adapters.roo_code import RooCodeAdapter

        adapter = RooCodeAdapter()
        proc_mock = _make_popen_mock(pid=506)
        with patch("bernstein.adapters.roo_code.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="roo-session-abc",
            )
        assert result.log_path.name == "roo-session-abc.log"

    def test_output_format_json(self, tmp_path: Path) -> None:
        from bernstein.adapters.roo_code import RooCodeAdapter

        adapter = RooCodeAdapter()
        proc_mock = _make_popen_mock(pid=507)
        with patch("bernstein.adapters.roo_code.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="roo7",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--output-format" in inner
        assert inner[inner.index("--output-format") + 1] == "json"


class TestRooCodeAdapterName:
    """RooCodeAdapter.name() returns 'Roo Code'."""

    def test_name(self) -> None:
        from bernstein.adapters.roo_code import RooCodeAdapter

        adapter = RooCodeAdapter()
        assert adapter.name() == "Roo Code"


class TestRooCodeAdapterMissingBinary:
    """spawn() raises RuntimeError with a clear message when binary is missing."""

    def test_file_not_found_raises_runtime_error(self, tmp_path: Path) -> None:
        from bernstein.adapters.roo_code import RooCodeAdapter

        adapter = RooCodeAdapter()
        with (
            patch(
                "bernstein.adapters.roo_code.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="not found in PATH"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="roo-missing",
            )

    def test_permission_error_raises_runtime_error(self, tmp_path: Path) -> None:
        from bernstein.adapters.roo_code import RooCodeAdapter

        adapter = RooCodeAdapter()
        with (
            patch(
                "bernstein.adapters.roo_code.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="roo-perm",
            )


class TestRooCodeAdapterRegistered:
    """RooCodeAdapter is accessible via the adapter registry."""

    def test_registered_under_roo_code(self) -> None:
        from bernstein.adapters.registry import get_adapter
        from bernstein.adapters.roo_code import RooCodeAdapter

        adapter = get_adapter("roo-code")
        assert isinstance(adapter, RooCodeAdapter)
