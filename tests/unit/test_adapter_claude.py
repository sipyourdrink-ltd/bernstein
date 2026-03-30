"""Unit tests for ClaudeCodeAdapter spawn/kill/is_alive logic."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.claude import ClaudeCodeAdapter, _resolve_env_vars, load_mcp_config
from bernstein.core.models import ModelConfig

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_popen_mock(pid: int) -> MagicMock:
    """Return a minimal Popen mock with a usable stdout."""
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.stdout = MagicMock()
    return m


@pytest.fixture(autouse=True)
def clean_adapter_state() -> None:
    """Clear class-level proc dicts before and after every test."""
    ClaudeCodeAdapter._procs.clear()
    ClaudeCodeAdapter._wrapper_pids.clear()
    yield  # type: ignore[misc]
    ClaudeCodeAdapter._procs.clear()
    ClaudeCodeAdapter._wrapper_pids.clear()


# ---------------------------------------------------------------------------
# spawn() — command-line argument construction
# ---------------------------------------------------------------------------


class TestSpawnCommandArgs:
    """spawn() builds the correct CLI invocation."""

    def _spawn(
        self,
        tmp_path: Path,
        model: str = "sonnet",
        effort: str = "high",
        mcp_config: dict | None = None,
    ) -> tuple[list[str], MagicMock, MagicMock]:
        adapter = ClaudeCodeAdapter()
        claude_mock = _make_popen_mock(pid=200)
        wrapper_mock = _make_popen_mock(pid=201)

        with patch("bernstein.adapters.claude.subprocess.Popen", side_effect=[claude_mock, wrapper_mock]) as popen:
            adapter.spawn(
                prompt="do something",
                workdir=tmp_path,
                model_config=ModelConfig(model=model, effort=effort),
                session_id="sess-1",
                mcp_config=mcp_config,
            )
            claude_cmd: list[str] = popen.call_args_list[0].args[0]

        return claude_cmd, claude_mock, wrapper_mock

    @pytest.mark.parametrize(
        "short, full",
        [
            ("opus", "claude-opus-4-6"),
            ("sonnet", "claude-sonnet-4-6"),
            ("haiku", "claude-haiku-4-5-20251001"),
        ],
    )
    def test_model_mapping(self, tmp_path: Path, short: str, full: str) -> None:
        cmd, _, __ = self._spawn(tmp_path, model=short)
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == full

    def test_unknown_model_passed_through(self, tmp_path: Path) -> None:
        cmd, _, __ = self._spawn(tmp_path, model="gpt-4.1")
        assert cmd[cmd.index("--model") + 1] == "gpt-4.1"

    @pytest.mark.parametrize(
        "effort, expected_turns, expected_effort_flag",
        [
            ("max", "100", "max"),
            ("high", "50", "high"),
            ("medium", "30", "medium"),
            ("normal", "25", "medium"),
            ("low", "15", "low"),
        ],
    )
    def test_effort_and_max_turns(
        self, tmp_path: Path, effort: str, expected_turns: str, expected_effort_flag: str
    ) -> None:
        cmd, _, __ = self._spawn(tmp_path, effort=effort)

        assert "--effort" in cmd
        assert cmd[cmd.index("--effort") + 1] == expected_effort_flag

        assert "--max-turns" in cmd
        assert cmd[cmd.index("--max-turns") + 1] == expected_turns

    def test_fixed_flags_always_present(self, tmp_path: Path) -> None:
        cmd, _, __ = self._spawn(tmp_path)
        assert "--dangerously-skip-permissions" in cmd
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in cmd

    def test_prompt_appended_last(self, tmp_path: Path) -> None:
        adapter = ClaudeCodeAdapter()
        claude_mock = _make_popen_mock(pid=300)
        wrapper_mock = _make_popen_mock(pid=301)

        with patch("bernstein.adapters.claude.subprocess.Popen", side_effect=[claude_mock, wrapper_mock]) as popen:
            adapter.spawn(
                prompt="my-unique-prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="sess-2",
            )
            cmd: list[str] = popen.call_args_list[0].args[0]

        assert "-p" in cmd
        assert cmd[cmd.index("-p") + 1] == "my-unique-prompt"

    def test_mcp_config_flag_included(self, tmp_path: Path) -> None:
        mcp = {"mcpServers": {"my-server": {"command": "npx"}}}
        cmd, _, __ = self._spawn(tmp_path, mcp_config=mcp)

        assert "--mcp-config" in cmd
        parsed = json.loads(cmd[cmd.index("--mcp-config") + 1])
        assert parsed == mcp

    def test_no_mcp_flag_when_none(self, tmp_path: Path) -> None:
        cmd, _, __ = self._spawn(tmp_path, mcp_config=None)
        assert "--mcp-config" not in cmd


# ---------------------------------------------------------------------------
# spawn() — two-process pipeline wiring
# ---------------------------------------------------------------------------


class TestSpawnPipeline:
    """spawn() creates the correct two-Popen wrapper pipeline."""

    def test_two_popen_calls(self, tmp_path: Path) -> None:
        adapter = ClaudeCodeAdapter()
        claude_mock = _make_popen_mock(pid=400)
        wrapper_mock = _make_popen_mock(pid=401)

        with patch("bernstein.adapters.claude.subprocess.Popen", side_effect=[claude_mock, wrapper_mock]) as popen:
            adapter.spawn(
                prompt="p",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="s",
            )

        assert popen.call_count == 2

    def test_claude_proc_stdout_piped(self, tmp_path: Path) -> None:
        """First Popen (claude) must use stdout=PIPE."""
        adapter = ClaudeCodeAdapter()
        claude_mock = _make_popen_mock(pid=500)
        wrapper_mock = _make_popen_mock(pid=501)

        with patch("bernstein.adapters.claude.subprocess.Popen", side_effect=[claude_mock, wrapper_mock]) as popen:
            adapter.spawn(
                prompt="p",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="s",
            )

        first_call_kwargs = popen.call_args_list[0].kwargs
        assert first_call_kwargs.get("stdout") == subprocess.PIPE

    def test_wrapper_stdin_is_claude_stdout(self, tmp_path: Path) -> None:
        """Second Popen (wrapper) must receive claude_proc.stdout as its stdin."""
        adapter = ClaudeCodeAdapter()
        claude_mock = _make_popen_mock(pid=600)
        wrapper_mock = _make_popen_mock(pid=601)

        with patch("bernstein.adapters.claude.subprocess.Popen", side_effect=[claude_mock, wrapper_mock]) as popen:
            adapter.spawn(
                prompt="p",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="s",
            )

        second_call_kwargs = popen.call_args_list[1].kwargs
        assert second_call_kwargs.get("stdin") is claude_mock.stdout

    def test_claude_stdout_closed_after_spawn(self, tmp_path: Path) -> None:
        """claude_proc.stdout.close() must be called to allow SIGPIPE."""
        adapter = ClaudeCodeAdapter()
        claude_mock = _make_popen_mock(pid=700)
        wrapper_mock = _make_popen_mock(pid=701)

        with patch("bernstein.adapters.claude.subprocess.Popen", side_effect=[claude_mock, wrapper_mock]):
            adapter.spawn(
                prompt="p",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="s",
            )

        claude_mock.stdout.close.assert_called_once()

    def test_spawn_result_contains_claude_pid(self, tmp_path: Path) -> None:
        adapter = ClaudeCodeAdapter()
        claude_mock = _make_popen_mock(pid=800)
        wrapper_mock = _make_popen_mock(pid=801)

        with patch("bernstein.adapters.claude.subprocess.Popen", side_effect=[claude_mock, wrapper_mock]):
            result = adapter.spawn(
                prompt="p",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="s",
            )

        assert result.pid == 800

    def test_spawn_records_procs(self, tmp_path: Path) -> None:
        """Both _procs and _wrapper_pids are populated after spawn."""
        adapter = ClaudeCodeAdapter()
        claude_mock = _make_popen_mock(pid=900)
        wrapper_mock = _make_popen_mock(pid=901)

        with patch("bernstein.adapters.claude.subprocess.Popen", side_effect=[claude_mock, wrapper_mock]):
            adapter.spawn(
                prompt="p",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="s",
            )

        assert 900 in ClaudeCodeAdapter._procs
        assert ClaudeCodeAdapter._wrapper_pids[900] == 901

    def test_wrapper_uses_python_executable(self, tmp_path: Path) -> None:
        """Wrapper command uses the same Python interpreter as the current process."""
        adapter = ClaudeCodeAdapter()
        claude_mock = _make_popen_mock(pid=1000)
        wrapper_mock = _make_popen_mock(pid=1001)

        with patch("bernstein.adapters.claude.subprocess.Popen", side_effect=[claude_mock, wrapper_mock]) as popen:
            adapter.spawn(
                prompt="p",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="s",
            )

        wrapper_cmd = popen.call_args_list[1].args[0]
        assert wrapper_cmd[0] == sys.executable
        assert wrapper_cmd[1] == "-c"


# ---------------------------------------------------------------------------
# is_alive()
# ---------------------------------------------------------------------------


class TestIsAlive:
    """is_alive() correctly detects running vs terminated processes."""

    def test_true_when_poll_returns_none(self) -> None:
        adapter = ClaudeCodeAdapter()
        proc = MagicMock()
        proc.poll.return_value = None
        ClaudeCodeAdapter._procs[42] = proc

        assert adapter.is_alive(42) is True

    def test_false_when_poll_returns_returncode(self) -> None:
        adapter = ClaudeCodeAdapter()
        proc = MagicMock()
        proc.poll.return_value = 0
        ClaudeCodeAdapter._procs[43] = proc

        assert adapter.is_alive(43) is False

    def test_false_when_poll_returns_nonzero(self) -> None:
        adapter = ClaudeCodeAdapter()
        proc = MagicMock()
        proc.poll.return_value = 1
        ClaudeCodeAdapter._procs[44] = proc

        assert adapter.is_alive(44) is False

    def test_fallback_true_when_pid_not_tracked(self) -> None:
        """For un-tracked PIDs, falls back to os.kill(pid, 0)."""
        adapter = ClaudeCodeAdapter()

        with patch("bernstein.adapters.claude.os.kill") as mock_kill:
            mock_kill.return_value = None  # no exception → process exists
            assert adapter.is_alive(9999) is True

        mock_kill.assert_called_once_with(9999, 0)

    def test_fallback_false_when_oserror(self) -> None:
        """Fallback returns False when os.kill raises OSError (no such process)."""
        adapter = ClaudeCodeAdapter()

        with patch("bernstein.adapters.claude.os.kill", side_effect=OSError("no such process")):
            assert adapter.is_alive(9998) is False


# ---------------------------------------------------------------------------
# kill()
# ---------------------------------------------------------------------------


class TestKill:
    """kill() terminates both the claude process and the wrapper process."""

    def test_calls_killpg_on_claude_process(self) -> None:
        adapter = ClaudeCodeAdapter()
        ClaudeCodeAdapter._procs[50] = MagicMock()
        ClaudeCodeAdapter._wrapper_pids[50] = 51

        with (
            patch("bernstein.adapters.claude.os.getpgid", return_value=50) as mock_getpgid,
            patch("bernstein.adapters.claude.os.killpg") as mock_killpg,
            patch("bernstein.adapters.claude.os.kill"),
        ):
            adapter.kill(50)

        mock_getpgid.assert_called_once_with(50)
        mock_killpg.assert_called_once_with(50, signal.SIGTERM)

    def test_kills_wrapper_process(self) -> None:
        adapter = ClaudeCodeAdapter()
        ClaudeCodeAdapter._procs[60] = MagicMock()
        ClaudeCodeAdapter._wrapper_pids[60] = 61

        with (
            patch("bernstein.adapters.claude.os.getpgid", return_value=60),
            patch("bernstein.adapters.claude.os.killpg"),
            patch("bernstein.adapters.claude.os.kill") as mock_kill,
        ):
            adapter.kill(60)

        mock_kill.assert_called_once_with(61, signal.SIGTERM)

    def test_removes_pid_from_tracking(self) -> None:
        adapter = ClaudeCodeAdapter()
        ClaudeCodeAdapter._procs[70] = MagicMock()
        ClaudeCodeAdapter._wrapper_pids[70] = 71

        with (
            patch("bernstein.adapters.claude.os.getpgid", return_value=70),
            patch("bernstein.adapters.claude.os.killpg"),
            patch("bernstein.adapters.claude.os.kill"),
        ):
            adapter.kill(70)

        assert 70 not in ClaudeCodeAdapter._procs
        assert 70 not in ClaudeCodeAdapter._wrapper_pids

    def test_handles_oserror_from_killpg(self) -> None:
        """kill() must not raise if the claude process is already dead."""
        adapter = ClaudeCodeAdapter()
        ClaudeCodeAdapter._procs[80] = MagicMock()
        ClaudeCodeAdapter._wrapper_pids[80] = 81

        with (
            patch("bernstein.adapters.claude.os.getpgid", return_value=80),
            patch("bernstein.adapters.claude.os.killpg", side_effect=OSError("no process")),
            patch("bernstein.adapters.claude.os.kill"),
        ):
            adapter.kill(80)  # must not raise

    def test_handles_oserror_from_wrapper_kill(self) -> None:
        """kill() must not raise if the wrapper process is already dead."""
        adapter = ClaudeCodeAdapter()
        ClaudeCodeAdapter._procs[90] = MagicMock()
        ClaudeCodeAdapter._wrapper_pids[90] = 91

        with (
            patch("bernstein.adapters.claude.os.getpgid", return_value=90),
            patch("bernstein.adapters.claude.os.killpg"),
            patch("bernstein.adapters.claude.os.kill", side_effect=OSError("no process")),
        ):
            adapter.kill(90)  # must not raise

    def test_kill_without_tracked_wrapper(self) -> None:
        """kill() works even if no wrapper PID was recorded."""
        adapter = ClaudeCodeAdapter()
        ClaudeCodeAdapter._procs[100] = MagicMock()
        # _wrapper_pids intentionally not set for pid 100

        with (
            patch("bernstein.adapters.claude.os.getpgid", return_value=100),
            patch("bernstein.adapters.claude.os.killpg"),
            patch("bernstein.adapters.claude.os.kill") as mock_kill,
        ):
            adapter.kill(100)

        mock_kill.assert_not_called()


# ---------------------------------------------------------------------------
# load_mcp_config() — merge logic
# ---------------------------------------------------------------------------


class TestLoadMcpConfigMerge:
    """load_mcp_config() correctly merges global and project-level MCP configs."""

    def test_project_wins_on_name_conflict(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "mcp.json").write_text(json.dumps({"mcpServers": {"tool": {"command": "old-command"}}}))

        with patch("bernstein.adapters.claude.Path.home", return_value=tmp_path):
            result = load_mcp_config(project_servers={"tool": {"command": "new-command"}})

        assert result is not None
        assert result["mcpServers"]["tool"]["command"] == "new-command"

    def test_both_sources_merged_without_conflict(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "mcp.json").write_text(json.dumps({"mcpServers": {"global-tool": {"command": "g"}}}))

        with patch("bernstein.adapters.claude.Path.home", return_value=tmp_path):
            result = load_mcp_config(project_servers={"project-tool": {"command": "p"}})

        assert result is not None
        assert "global-tool" in result["mcpServers"]
        assert "project-tool" in result["mcpServers"]

    def test_returns_none_when_no_servers(self, tmp_path: Path) -> None:
        with patch("bernstein.adapters.claude.Path.home", return_value=tmp_path):
            assert load_mcp_config() is None

    def test_env_vars_resolved_in_project_servers(self, tmp_path: Path) -> None:
        with (
            patch("bernstein.adapters.claude.Path.home", return_value=tmp_path),
            patch.dict(os.environ, {"MY_TOKEN": "tok-secret"}),
        ):
            result = load_mcp_config(project_servers={"srv": {"env": {"TOKEN": "${MY_TOKEN}"}}})

        assert result is not None
        assert result["mcpServers"]["srv"]["env"]["TOKEN"] == "tok-secret"


# ---------------------------------------------------------------------------
# _resolve_env_vars()
# ---------------------------------------------------------------------------


class TestResolveEnvVars:
    """_resolve_env_vars() expands ${VAR} references recursively."""

    def test_top_level_string(self) -> None:
        with patch.dict(os.environ, {"KEY": "value"}):
            assert _resolve_env_vars("${KEY}") == "value"

    def test_missing_var_returns_original(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert _resolve_env_vars("${MISSING}") == "${MISSING}"

    def test_nested_dict(self) -> None:
        with patch.dict(os.environ, {"TOKEN": "abc123"}):
            result = _resolve_env_vars({"outer": {"inner": "${TOKEN}"}})
        assert result == {"outer": {"inner": "abc123"}}

    def test_list_items(self) -> None:
        with patch.dict(os.environ, {"A": "1", "B": "2"}):
            result = _resolve_env_vars(["${A}", "literal", "${B}"])
        assert result == ["1", "literal", "2"]

    def test_non_env_string_unchanged(self) -> None:
        assert _resolve_env_vars("plain") == "plain"

    def test_non_string_scalars_unchanged(self) -> None:
        assert _resolve_env_vars(42) == 42
        assert _resolve_env_vars(True) is True
        assert _resolve_env_vars(None) is None


# ---------------------------------------------------------------------------
# spawn() — missing CLI binary / PermissionError
# ---------------------------------------------------------------------------


class TestSpawnMissingBinary:
    """ClaudeCodeAdapter.spawn() raises RuntimeError when 'claude' binary is missing."""

    def test_file_not_found_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = ClaudeCodeAdapter()
        with (
            patch(
                "bernstein.adapters.claude.subprocess.Popen",
                side_effect=FileNotFoundError("No such file or directory: 'claude'"),
            ),
            pytest.raises(RuntimeError, match="not found in PATH"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="claude-missing",
            )

    def test_permission_error_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = ClaudeCodeAdapter()
        with (
            patch(
                "bernstein.adapters.claude.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="claude-perm",
            )
