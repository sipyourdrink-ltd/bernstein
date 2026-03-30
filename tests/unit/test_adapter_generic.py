"""Unit tests for GenericAdapter spawn/kill/is_alive."""

from __future__ import annotations

import signal
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.generic import GenericAdapter
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


# ---------------------------------------------------------------------------
# GenericAdapter.spawn() — command construction
# ---------------------------------------------------------------------------


class TestGenericAdapterSpawn:
    """GenericAdapter.spawn() respects configuration passed at construction."""

    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="my-agent")
        proc_mock = _make_popen_mock(pid=700)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen-s1",
            )
        cmd = popen.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.worker"]
        inner = _inner_cmd(cmd)
        assert inner[0] == "my-agent"

    def test_custom_binary_path(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="/opt/agents/custom-agent")
        proc_mock = _make_popen_mock(pid=701)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen-s2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[0] == "/opt/agents/custom-agent"

    def test_model_flag_included(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="agent", model_flag="--model")
        proc_mock = _make_popen_mock(pid=702)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen-s3",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--model" in inner
        assert inner[inner.index("--model") + 1] == "gpt-4o"

    def test_model_flag_omitted_when_none(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="agent", model_flag=None)
        proc_mock = _make_popen_mock(pid=703)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen-s4",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--model" not in inner

    def test_custom_model_flag(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="agent", model_flag="-m")
        proc_mock = _make_popen_mock(pid=704)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="llama3", effort="high"),
                session_id="gen-s5",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "-m" in inner
        assert inner[inner.index("-m") + 1] == "llama3"

    def test_default_prompt_flag(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="agent")
        proc_mock = _make_popen_mock(pid=705)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="do-this",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen-s6",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--prompt" in inner
        assert inner[inner.index("--prompt") + 1] == "do-this"

    def test_custom_prompt_flag(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="agent", prompt_flag="--message")
        proc_mock = _make_popen_mock(pid=706)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="my-task",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen-s7",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--message" in inner
        assert inner[inner.index("--message") + 1] == "my-task"

    def test_extra_args_included(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="agent", extra_args=["--yes", "--verbose", "--no-git"])
        proc_mock = _make_popen_mock(pid=707)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen-s8",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--yes" in inner
        assert "--verbose" in inner
        assert "--no-git" in inner

    def test_extra_args_default_empty(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="agent")
        proc_mock = _make_popen_mock(pid=708)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen-s9",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        # Should just be: cli_command --model model --prompt prompt
        assert inner[0] == "agent"

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="agent")
        proc_mock = _make_popen_mock(pid=709)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen-s10",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_spawn_result_pid(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="agent")
        proc_mock = _make_popen_mock(pid=710)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen-s11",
            )
        assert result.pid == 710

    def test_log_path_uses_session_id(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="agent")
        proc_mock = _make_popen_mock(pid=711)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="my-generic-session",
            )
        assert result.log_path.name == "my-generic-session.log"

    def test_start_new_session_enabled(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="agent")
        proc_mock = _make_popen_mock(pid=712)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen-s12",
            )
        kwargs = popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True

    def test_model_passed_raw_no_mapping(self, tmp_path: Path) -> None:
        """GenericAdapter passes model_config.model as-is, no model mapping."""
        adapter = GenericAdapter(cli_command="agent")
        proc_mock = _make_popen_mock(pid=713)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="some-exotic-model", effort="high"),
                session_id="gen-s13",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[inner.index("--model") + 1] == "some-exotic-model"


# ---------------------------------------------------------------------------
# GenericAdapter.name()
# ---------------------------------------------------------------------------


class TestGenericAdapterName:
    def test_default_display_name(self) -> None:
        adapter = GenericAdapter(cli_command="agent")
        assert adapter.name() == "Generic CLI"

    def test_custom_display_name(self) -> None:
        adapter = GenericAdapter(cli_command="aider", display_name="Aider Agent")
        assert adapter.name() == "Aider Agent"


# ---------------------------------------------------------------------------
# Env isolation
# ---------------------------------------------------------------------------


class TestGenericEnvIsolation:
    """spawn() uses build_filtered_env() with no specific keys."""

    def test_env_includes_path(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="agent")
        proc_mock = _make_popen_mock(pid=720)
        with (
            patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen-env1",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "PATH" in env

    def test_env_excludes_secrets(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="agent")
        proc_mock = _make_popen_mock(pid=721)
        with (
            patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {"PATH": "/usr/bin", "DATABASE_URL": "postgres://x", "SECRET_KEY": "s3cret"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen-env2",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "DATABASE_URL" not in env
        assert "SECRET_KEY" not in env


# ---------------------------------------------------------------------------
# Missing binary / PermissionError
# ---------------------------------------------------------------------------


class TestGenericSpawnMissingBinary:
    def test_file_not_found_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="notarealthing")
        with (
            patch(
                "bernstein.adapters.generic.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="not found in PATH"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="missing",
            )

    def test_permission_error_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="notarealthing")
        with (
            patch(
                "bernstein.adapters.generic.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="perm-denied",
            )

    def test_error_message_includes_command_name(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="my-custom-agent")
        with (
            patch(
                "bernstein.adapters.generic.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="my-custom-agent"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="missing2",
            )


# ---------------------------------------------------------------------------
# is_alive() and kill() — inherited from CLIAdapter base
# ---------------------------------------------------------------------------


class TestGenericIsAlive:
    def test_true_when_process_exists(self) -> None:
        adapter = GenericAdapter(cli_command="agent")
        with patch("bernstein.adapters.base.os.kill", return_value=None) as mock_kill:
            assert adapter.is_alive(1234) is True
        mock_kill.assert_called_once_with(1234, 0)

    def test_false_when_oserror(self) -> None:
        adapter = GenericAdapter(cli_command="agent")
        with patch("bernstein.adapters.base.os.kill", side_effect=OSError("no such process")):
            assert adapter.is_alive(9999) is False


class TestGenericKill:
    def test_calls_killpg(self) -> None:
        adapter = GenericAdapter(cli_command="agent")
        with (
            patch("bernstein.adapters.base.os.getpgid", return_value=555) as mock_getpgid,
            patch("bernstein.adapters.base.os.killpg") as mock_killpg,
        ):
            adapter.kill(555)
        mock_getpgid.assert_called_once_with(555)
        mock_killpg.assert_called_once_with(555, signal.SIGTERM)

    def test_does_not_raise_on_oserror(self) -> None:
        adapter = GenericAdapter(cli_command="agent")
        with (
            patch("bernstein.adapters.base.os.getpgid", return_value=556),
            patch("bernstein.adapters.base.os.killpg", side_effect=OSError("no process")),
        ):
            adapter.kill(556)  # must not raise


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestGenericRegistry:
    def test_generic_from_registry(self) -> None:
        from bernstein.adapters.registry import get_adapter

        adapter = get_adapter("generic")
        assert isinstance(adapter, GenericAdapter)

    def test_generic_name_via_registry(self) -> None:
        from bernstein.adapters.registry import get_adapter

        assert get_adapter("generic").name() == "Generic CLI"
