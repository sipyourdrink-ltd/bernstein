"""Unit tests for GeminiAdapter spawn/kill/is_alive."""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.gemini import GeminiAdapter
from bernstein.core.models import ApiTier, ModelConfig, ProviderType

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
# GeminiAdapter.spawn() — command construction
# ---------------------------------------------------------------------------


class TestGeminiAdapterSpawn:
    """GeminiAdapter.spawn() builds correct command."""

    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=100)
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-2.5-pro", effort="high"),
                session_id="gemini-s1",
            )
        cmd = popen.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.worker"]
        inner = _inner_cmd(cmd)
        assert inner[0] == "gemini"

    def test_model_flag_passthrough(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=101)
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-2.5-flash", effort="high"),
                session_id="gemini-s2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--model" in inner
        assert inner[inner.index("--model") + 1] == "gemini-2.5-flash"

    def test_sandbox_none_flag(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=102)
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-2.5-pro", effort="high"),
                session_id="gemini-s3",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--sandbox" in inner
        assert inner[inner.index("--sandbox") + 1] == "none"

    def test_prompt_flag_used(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=103)
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="my-unique-prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-2.5-pro", effort="high"),
                session_id="gemini-s4",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--prompt" in inner
        assert inner[inner.index("--prompt") + 1] == "my-unique-prompt"

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=104)
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-2.5-pro", effort="high"),
                session_id="gemini-s5",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_spawn_result_pid(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=105)
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-2.5-pro", effort="high"),
                session_id="gemini-s6",
            )
        assert result.pid == 105

    def test_log_path_uses_session_id(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=106)
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-2.5-pro", effort="high"),
                session_id="my-gemini-session",
            )
        assert result.log_path.name == "my-gemini-session.log"

    def test_start_new_session_enabled(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=107)
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-2.5-pro", effort="high"),
                session_id="gemini-s7",
            )
        kwargs = popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True


# ---------------------------------------------------------------------------
# spawn() — env isolation
# ---------------------------------------------------------------------------


class TestGeminiEnvIsolation:
    """spawn() passes only Google-specific keys to subprocess."""

    def test_env_contains_google_keys(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=200)
        with (
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {"GOOGLE_API_KEY": "AIza-test", "GOOGLE_CLOUD_PROJECT": "my-proj", "PATH": "/usr/bin"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-2.5-pro", effort="high"),
                session_id="gemini-env1",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "GOOGLE_API_KEY" in env
        assert env["GOOGLE_API_KEY"] == "AIza-test"

    def test_env_excludes_unrelated_keys(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=201)
        with (
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {
                    "GOOGLE_API_KEY": "AIza-test",
                    "ANTHROPIC_API_KEY": "ant-secret",
                    "OPENAI_API_KEY": "sk-secret",
                    "DATABASE_URL": "postgres://x",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-2.5-pro", effort="high"),
                session_id="gemini-env2",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "ANTHROPIC_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
        assert "DATABASE_URL" not in env

    def test_env_includes_path(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=202)
        with (
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict("os.environ", {"PATH": "/usr/bin", "GOOGLE_API_KEY": "AIza-x"}, clear=True),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-2.5-pro", effort="high"),
                session_id="gemini-env3",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "PATH" in env


# ---------------------------------------------------------------------------
# GeminiAdapter.name()
# ---------------------------------------------------------------------------


class TestGeminiAdapterName:
    def test_name(self) -> None:
        assert GeminiAdapter().name() == "Gemini"


# ---------------------------------------------------------------------------
# Missing binary / PermissionError
# ---------------------------------------------------------------------------


class TestGeminiSpawnMissingBinary:
    def test_file_not_found_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        with (
            patch(
                "bernstein.adapters.gemini.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="not found in PATH"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-2.5-pro", effort="high"),
                session_id="missing",
            )

    def test_permission_error_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        with (
            patch(
                "bernstein.adapters.gemini.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-2.5-pro", effort="high"),
                session_id="perm-denied",
            )


# ---------------------------------------------------------------------------
# is_alive() and kill() — inherited from CLIAdapter base
# ---------------------------------------------------------------------------


class TestGeminiIsAlive:
    def test_true_when_process_exists(self) -> None:
        adapter = GeminiAdapter()
        with patch("bernstein.adapters.base.os.kill", return_value=None) as mock_kill:
            assert adapter.is_alive(1234) is True
        mock_kill.assert_called_once_with(1234, 0)

    def test_false_when_oserror(self) -> None:
        adapter = GeminiAdapter()
        with patch("bernstein.adapters.base.os.kill", side_effect=OSError("no such process")):
            assert adapter.is_alive(9999) is False


class TestGeminiKill:
    def test_calls_killpg(self) -> None:
        adapter = GeminiAdapter()
        with (
            patch("bernstein.adapters.base.os.getpgid", return_value=555) as mock_getpgid,
            patch("bernstein.adapters.base.os.killpg") as mock_killpg,
        ):
            adapter.kill(555)
        mock_getpgid.assert_called_once_with(555)
        mock_killpg.assert_called_once_with(555, signal.SIGTERM)

    def test_does_not_raise_on_oserror(self) -> None:
        adapter = GeminiAdapter()
        with (
            patch("bernstein.adapters.base.os.getpgid", return_value=556),
            patch("bernstein.adapters.base.os.killpg", side_effect=OSError("no process")),
        ):
            adapter.kill(556)  # must not raise


# ---------------------------------------------------------------------------
# detect_tier()
# ---------------------------------------------------------------------------


class TestGeminiDetectTier:
    def test_returns_none_without_api_key(self) -> None:
        adapter = GeminiAdapter()
        with patch.dict("os.environ", {}, clear=True):
            assert adapter.detect_tier() is None

    def test_enterprise_with_gcp_project(self) -> None:
        adapter = GeminiAdapter()
        with patch.dict(
            "os.environ",
            {"GOOGLE_API_KEY": "AIza-test", "GOOGLE_CLOUD_PROJECT": "my-project"},
            clear=True,
        ):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.ENTERPRISE
        assert info.provider == ProviderType.GEMINI

    def test_pro_with_aiza_key(self) -> None:
        adapter = GeminiAdapter()
        with patch.dict("os.environ", {"GOOGLE_API_KEY": "AIzaSyB-test-key"}, clear=True):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.PRO

    def test_free_with_unknown_key_format(self) -> None:
        adapter = GeminiAdapter()
        with patch.dict("os.environ", {"GOOGLE_API_KEY": "random-key"}, clear=True):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.FREE
