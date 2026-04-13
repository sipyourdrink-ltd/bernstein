"""Unit tests for AmpAdapter spawn/kill/is_alive."""

from __future__ import annotations

import signal
import subprocess
import sys
from collections.abc import Generator
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.amp import AmpAdapter


@pytest.fixture(autouse=True)
def _no_watchdog_threads() -> Generator[None, None, None]:
    """Disable watchdog threads to avoid 'can't start new thread' on CI."""
    with patch("bernstein.adapters.base.CLIAdapter._start_timeout_watchdog", return_value=None):
        yield


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
# AmpAdapter.spawn()
# ---------------------------------------------------------------------------


class TestAmpAdapterSpawn:
    """AmpAdapter.spawn() builds correct command."""

    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        proc_mock = _make_popen_mock(pid=600)
        with patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="amp-s1",
            )
        cmd = popen.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.worker"]
        inner = _inner_cmd(cmd)
        assert inner[0] == "amp"

    def test_model_flag_with_mapped_model(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        proc_mock = _make_popen_mock(pid=601)
        with patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="amp-s2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--model" in inner
        assert inner[inner.index("--model") + 1] == "anthropic:claude-sonnet-4-6"

    def test_model_map_opus(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        proc_mock = _make_popen_mock(pid=602)
        with patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="opus", effort="high"),
                session_id="amp-s3",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[inner.index("--model") + 1] == "anthropic:claude-opus-4-6"

    def test_model_map_haiku(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        proc_mock = _make_popen_mock(pid=603)
        with patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="haiku", effort="high"),
                session_id="amp-s4",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[inner.index("--model") + 1] == "anthropic:claude-haiku-4-5-20251001"

    def test_unknown_model_passes_through(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        proc_mock = _make_popen_mock(pid=604)
        with patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="some-custom-model", effort="high"),
                session_id="amp-s5",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[inner.index("--model") + 1] == "some-custom-model"

    def test_prompt_flag_used(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        proc_mock = _make_popen_mock(pid=605)
        with patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="my-unique-task",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="amp-s6",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--prompt" in inner
        assert inner[inner.index("--prompt") + 1] == "my-unique-task"

    def test_headless_flag_present(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        proc_mock = _make_popen_mock(pid=606)
        with patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="amp-s7",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--headless" in inner

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        proc_mock = _make_popen_mock(pid=607)
        with patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="amp-s8",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_spawn_result_pid(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        proc_mock = _make_popen_mock(pid=608)
        with patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="amp-s9",
            )
        assert result.pid == 608

    def test_log_path_uses_session_id(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        proc_mock = _make_popen_mock(pid=609)
        with patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="my-amp-session",
            )
        assert result.log_path.name == "my-amp-session.log"

    def test_start_new_session_enabled(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        proc_mock = _make_popen_mock(pid=610)
        with patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="amp-s10",
            )
        kwargs = popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True


# ---------------------------------------------------------------------------
# AmpAdapter.name()
# ---------------------------------------------------------------------------


class TestAmpAdapterName:
    def test_name(self) -> None:
        assert AmpAdapter().name() == "Amp"


# ---------------------------------------------------------------------------
# Env isolation
# ---------------------------------------------------------------------------


class TestAmpEnvIsolation:
    """spawn() passes only expected API keys to subprocess."""

    def test_env_contains_anthropic_key(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        proc_mock = _make_popen_mock(pid=620)
        with (
            patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {"ANTHROPIC_API_KEY": "ant-test", "PATH": "/usr/bin"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="amp-env1",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env.get("ANTHROPIC_API_KEY") == "ant-test"

    def test_env_contains_sourcegraph_keys(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        proc_mock = _make_popen_mock(pid=621)
        with (
            patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {
                    "SRC_ENDPOINT": "https://sourcegraph.example.com",
                    "SRC_ACCESS_TOKEN": "sgp-token",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="amp-env2",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env.get("SRC_ENDPOINT") == "https://sourcegraph.example.com"
        assert env.get("SRC_ACCESS_TOKEN") == "sgp-token"

    def test_env_excludes_unrelated_keys(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        proc_mock = _make_popen_mock(pid=622)
        with (
            patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock) as popen,
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
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="amp-env3",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "DATABASE_URL" not in env
        assert "SECRET_KEY" not in env

    def test_env_includes_path(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        proc_mock = _make_popen_mock(pid=623)
        with (
            patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="amp-env4",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "PATH" in env


# ---------------------------------------------------------------------------
# Missing binary / PermissionError
# ---------------------------------------------------------------------------


class TestAmpSpawnMissingBinary:
    def test_file_not_found_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        with (
            patch(
                "bernstein.adapters.amp.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="not found in PATH"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="missing",
            )

    def test_permission_error_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = AmpAdapter()
        with (
            patch(
                "bernstein.adapters.amp.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="perm-denied",
            )


# ---------------------------------------------------------------------------
# is_alive() and kill() — inherited from CLIAdapter base
# ---------------------------------------------------------------------------


class TestAmpIsAlive:
    def test_true_when_process_exists(self) -> None:
        adapter = AmpAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=True) as mock_alive:
            assert adapter.is_alive(1234) is True
        mock_alive.assert_called_once_with(1234)

    def test_false_when_oserror(self) -> None:
        adapter = AmpAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=False):
            assert adapter.is_alive(9999) is False


class TestAmpKill:
    def test_calls_killpg(self) -> None:
        adapter = AmpAdapter()
        with patch("bernstein.adapters.base.kill_process_group") as mock_killpg:
            adapter.kill(555)
        mock_killpg.assert_called_once_with(555, signal.SIGTERM)

    def test_does_not_raise_on_oserror(self) -> None:
        adapter = AmpAdapter()
        with patch("bernstein.adapters.base.kill_process_group", return_value=False):
            adapter.kill(556)  # must not raise


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestAmpRegistry:
    def test_amp_in_registry(self) -> None:
        from bernstein.adapters.registry import get_adapter

        adapter = get_adapter("amp")
        assert isinstance(adapter, AmpAdapter)

    def test_amp_name_via_registry(self) -> None:
        from bernstein.adapters.registry import get_adapter

        assert get_adapter("amp").name() == "Amp"
