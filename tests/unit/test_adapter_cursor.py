"""Unit tests for CursorAdapter.spawn()."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.cursor import CursorAdapter
from bernstein.core.models import ModelConfig

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    return m


def _inner_cmd(full_cmd: list[str]) -> list[str]:
    """Extract the actual CLI command after the '--' worker separator."""
    sep = full_cmd.index("--")
    return full_cmd[sep + 1 :]


def _spawn(
    tmp_path: Path,
    model: str = "claude-sonnet-4-6",
    prompt: str = "do work",
    mcp_config: dict | None = None,
) -> tuple[list[str], MagicMock]:
    adapter = CursorAdapter()
    proc_mock = _make_popen_mock(pid=500)
    with patch("bernstein.adapters.cursor.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt=prompt,
            workdir=tmp_path,
            model_config=ModelConfig(model=model, effort="high"),
            session_id="sess-cursor",
            mcp_config=mcp_config,
        )
    cmd: list[str] = popen.call_args.args[0]
    return cmd, proc_mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCursorAdapterSpawn:
    """CursorAdapter.spawn() builds correct command and delegates to subprocess."""

    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        cmd, _ = _spawn(tmp_path)
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.worker"]

    def test_inner_cmd_starts_with_cursor_agent(self, tmp_path: Path) -> None:
        cmd, _ = _spawn(tmp_path)
        inner = _inner_cmd(cmd)
        assert inner[0] == "cursor"
        assert inner[1] == "agent"

    def test_user_data_dir_set(self, tmp_path: Path) -> None:
        cmd, _ = _spawn(tmp_path)
        inner = _inner_cmd(cmd)
        assert "--user-data-dir" in inner
        idx = inner.index("--user-data-dir")
        data_dir = inner[idx + 1]
        assert "cursor" in data_dir
        assert "sess-cursor" in data_dir

    def test_prompt_appended(self, tmp_path: Path) -> None:
        cmd, _ = _spawn(tmp_path, prompt="unique-prompt-string")
        inner = _inner_cmd(cmd)
        assert inner[-1] == "unique-prompt-string"

    def test_mcp_config_injected(self, tmp_path: Path) -> None:
        mcp = {"mcpServers": {"test": {"command": "echo"}}}
        cmd, _ = _spawn(tmp_path, mcp_config=mcp)
        inner = _inner_cmd(cmd)
        assert "--add-mcp" in inner
        idx = inner.index("--add-mcp")
        parsed = json.loads(inner[idx + 1])
        assert parsed == mcp

    def test_no_mcp_flag_without_config(self, tmp_path: Path) -> None:
        cmd, _ = _spawn(tmp_path, mcp_config=None)
        inner = _inner_cmd(cmd)
        assert "--add-mcp" not in inner

    def test_creates_log_file(self, tmp_path: Path) -> None:
        _spawn(tmp_path)
        log_path = tmp_path / ".sdd" / "runtime" / "sess-cursor.log"
        assert log_path.exists()

    def test_creates_data_dir(self, tmp_path: Path) -> None:
        _spawn(tmp_path)
        data_dir = tmp_path / ".sdd" / "runtime" / "cursor" / "sess-cursor"
        assert data_dir.is_dir()

    def test_returns_correct_pid(self, tmp_path: Path) -> None:
        adapter = CursorAdapter()
        proc_mock = _make_popen_mock(pid=999)
        with patch("bernstein.adapters.cursor.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="claude-sonnet-4-6", effort="high"),
                session_id="sess-pid",
            )
        assert result.pid == 999

    def test_file_not_found_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = CursorAdapter()
        with patch("bernstein.adapters.cursor.subprocess.Popen", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="cursor not found in PATH"):
                adapter.spawn(
                    prompt="hello",
                    workdir=tmp_path,
                    model_config=ModelConfig(model="claude-sonnet-4-6", effort="high"),
                    session_id="sess-err",
                )

    def test_name(self) -> None:
        assert CursorAdapter().name() == "Cursor"
