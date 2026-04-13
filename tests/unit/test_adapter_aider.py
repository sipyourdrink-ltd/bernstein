"""Unit tests for AiderAdapter spawn/kill/is_alive."""

from __future__ import annotations

import signal
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.aider import AiderAdapter

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
# AiderAdapter.spawn()
# ---------------------------------------------------------------------------


class TestAiderAdapterSpawn:
    """AiderAdapter.spawn() builds correct command."""

    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=500)
        with patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="aider-s1",
            )
        cmd = popen.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.worker"]
        inner = _inner_cmd(cmd)
        assert inner[0] == "aider"

    def test_model_flag_passthrough(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=501)
        with patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="aider-s2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--model" in inner
        # gpt-5.4 maps to openai/gpt-5.4
        assert inner[inner.index("--model") + 1] == "openai/gpt-5.4"

    def test_model_map_sonnet(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=502)
        with patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="aider-s3",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[inner.index("--model") + 1] == "anthropic/claude-sonnet-4-6"

    def test_model_map_opus(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=503)
        with patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="opus", effort="high"),
                session_id="aider-s4",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[inner.index("--model") + 1] == "anthropic/claude-opus-4-6"

    def test_unknown_model_passes_through(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=504)
        with patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="some-custom-model", effort="high"),
                session_id="aider-s5",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[inner.index("--model") + 1] == "some-custom-model"

    def test_message_flag_used(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=505)
        with patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="my-unique-task",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="aider-s6",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--message" in inner
        assert inner[inner.index("--message") + 1] == "my-unique-task"

    def test_yes_flag_present(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=506)
        with patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="aider-s7",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--yes" in inner

    def test_auto_commits_flag_present(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=510)
        with patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="aider-flags1",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--auto-commits" in inner

    def test_map_tokens_flag_present(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=511)
        with patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="aider-flags2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--map-tokens" in inner
        assert inner[inner.index("--map-tokens") + 1] == "2048"

    def test_no_auto_lint_flag_present(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=512)
        with patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="aider-flags3",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--no-auto-lint" in inner

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=507)
        with patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="aider-s8",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_spawn_result_pid(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=508)
        with patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="aider-s9",
            )
        assert result.pid == 508

    def test_log_path_uses_session_id(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=509)
        with patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="my-aider-session",
            )
        assert result.log_path.name == "my-aider-session.log"


# ---------------------------------------------------------------------------
# AiderAdapter.name()
# ---------------------------------------------------------------------------


class TestAiderAdapterName:
    def test_name(self) -> None:
        assert AiderAdapter().name() == "Aider"


# ---------------------------------------------------------------------------
# Env isolation
# ---------------------------------------------------------------------------


class TestAiderEnvIsolation:
    """spawn() passes only expected API keys to subprocess."""

    def test_env_contains_anthropic_key(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=520)
        with (
            patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {"ANTHROPIC_API_KEY": "ant-test", "PATH": "/usr/bin"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="aider-env1",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env.get("ANTHROPIC_API_KEY") == "ant-test"

    def test_env_contains_openai_key(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=521)
        with (
            patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {"OPENAI_API_KEY": "sk-test", "PATH": "/usr/bin"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="aider-env2",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env.get("OPENAI_API_KEY") == "sk-test"

    def test_env_contains_azure_key(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=522)
        with (
            patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {"AZURE_OPENAI_API_KEY": "az-test", "PATH": "/usr/bin"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="aider-env3",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env.get("AZURE_OPENAI_API_KEY") == "az-test"

    def test_env_excludes_unrelated_keys(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        proc_mock = _make_popen_mock(pid=523)
        with (
            patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_API_KEY": "ant-test",
                    "DATABASE_URL": "postgres://x",
                    "SECRET_KEY": "s3cret",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="aider-env4",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "DATABASE_URL" not in env
        assert "SECRET_KEY" not in env


# ---------------------------------------------------------------------------
# Missing binary / PermissionError
# ---------------------------------------------------------------------------


class TestAiderSpawnMissingBinary:
    def test_file_not_found_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        with (
            patch(
                "bernstein.adapters.aider.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="not found in PATH"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="missing",
            )

    def test_permission_error_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        with (
            patch(
                "bernstein.adapters.aider.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="perm-denied",
            )


# ---------------------------------------------------------------------------
# is_alive() and kill() — inherited from CLIAdapter base
# ---------------------------------------------------------------------------


class TestAiderIsAlive:
    def test_true_when_process_exists(self) -> None:
        adapter = AiderAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=True):
            assert adapter.is_alive(1234) is True

    def test_false_when_oserror(self) -> None:
        adapter = AiderAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=False):
            assert adapter.is_alive(9999) is False


class TestAiderKill:
    def test_calls_killpg_with_pid_as_pgid(self) -> None:
        """kill() uses pid directly as pgid (start_new_session=True)."""
        adapter = AiderAdapter()
        with patch("bernstein.adapters.base.kill_process_group") as mock_kill:
            adapter.kill(555)
        mock_kill.assert_called_once_with(555, signal.SIGTERM)

    def test_does_not_raise_on_oserror(self) -> None:
        adapter = AiderAdapter()
        with patch("bernstein.adapters.base.kill_process_group", return_value=False):
            adapter.kill(556)  # must not raise


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestAiderRegistry:
    def test_aider_in_registry(self) -> None:
        from bernstein.adapters.registry import get_adapter

        adapter = get_adapter("aider")
        assert isinstance(adapter, AiderAdapter)

    def test_aider_name_via_registry(self) -> None:
        from bernstein.adapters.registry import get_adapter

        assert get_adapter("aider").name() == "Aider"
