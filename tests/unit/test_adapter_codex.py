"""Unit tests for CodexAdapter spawn/kill/is_alive."""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.codex import CodexAdapter
from bernstein.core.models import ApiTier, ModelConfig, ProviderType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.wait.return_value = None
    return m


def _inner_cmd(full_cmd: list[str]) -> list[str]:
    """Extract the actual CLI command after the '--' worker separator."""
    sep = full_cmd.index("--")
    return full_cmd[sep + 1 :]


# ---------------------------------------------------------------------------
# CodexAdapter.spawn() — command construction
# ---------------------------------------------------------------------------


class TestCodexAdapterSpawn:
    """CodexAdapter.spawn() builds correct command."""

    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=100)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s1",
            )
        cmd = popen.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.worker"]
        inner = _inner_cmd(cmd)
        assert inner[0] == "codex"

    def test_model_flag_passthrough(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=101)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3-mini", effort="high"),
                session_id="codex-s2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "-m" in inner
        assert inner[inner.index("-m") + 1] == "o3-mini"

    def test_full_auto_flag_present(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=102)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s3",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--full-auto" in inner

    def test_json_output_flag_present(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=103)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s4",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--json" in inner

    def test_output_file_flag_present(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=109)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s9",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "-o" in inner
        assert inner[inner.index("-o") + 1].endswith("codex-s9.last-message.txt")

    def test_prompt_appended_last(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=104)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="my-unique-prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s5",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[-1] == "my-unique-prompt"

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=105)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s6",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_spawn_result_pid(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=106)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s7",
            )
        assert result.pid == 106

    def test_log_path_uses_session_id(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=107)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="my-codex-session",
            )
        assert result.log_path.name == "my-codex-session.log"

    def test_start_new_session_enabled(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=108)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s8",
            )
        kwargs = popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True


# ---------------------------------------------------------------------------
# spawn() — env isolation
# ---------------------------------------------------------------------------


class TestCodexEnvIsolation:
    """spawn() passes only OPENAI-specific keys to subprocess."""

    def test_env_contains_openai_keys(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=200)
        with (
            patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {"OPENAI_API_KEY": "sk-test", "OPENAI_ORG_ID": "org-123", "PATH": "/usr/bin"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-env1",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "OPENAI_API_KEY" in env
        assert env["OPENAI_API_KEY"] == "sk-test"

    def test_env_excludes_unrelated_keys(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=201)
        with (
            patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "sk-test",
                    "ANTHROPIC_API_KEY": "ant-secret",
                    "DATABASE_URL": "postgres://x",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-env2",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "ANTHROPIC_API_KEY" not in env
        assert "DATABASE_URL" not in env

    def test_env_includes_path(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=202)
        with (
            patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict("os.environ", {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-x"}, clear=True),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-env3",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "PATH" in env


# ---------------------------------------------------------------------------
# CodexAdapter.name()
# ---------------------------------------------------------------------------


class TestCodexAdapterName:
    def test_name(self) -> None:
        assert CodexAdapter().name() == "Codex"


# ---------------------------------------------------------------------------
# Missing binary / PermissionError
# ---------------------------------------------------------------------------


class TestCodexSpawnMissingBinary:
    def test_file_not_found_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        with (
            patch(
                "bernstein.adapters.codex.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="not found in PATH"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="missing",
            )

    def test_permission_error_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        with (
            patch(
                "bernstein.adapters.codex.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="perm-denied",
            )


class TestCodexWarningsAndFastExit:
    def test_warns_when_openai_api_key_missing(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=301)
        with (
            patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock),
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            caplog.at_level("WARNING"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="warn-missing-key",
            )
        assert "OPENAI_API_KEY is not set" in caplog.text

    def test_fast_exit_rate_limit_raises(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=302)
        proc_mock.wait.return_value = 1
        with (
            patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock),
            patch.object(CodexAdapter, "_read_last_lines", return_value=["429 rate limit exceeded"]),
        ):
            with pytest.raises(RuntimeError, match="rate-limited"):
                adapter.spawn(
                    prompt="hello",
                    workdir=tmp_path,
                    model_config=ModelConfig(model="o3", effort="high"),
                    session_id="codex-fast-exit",
                )


# ---------------------------------------------------------------------------
# is_alive() and kill() — inherited from CLIAdapter base
# ---------------------------------------------------------------------------


class TestCodexIsAlive:
    def test_true_when_process_exists(self) -> None:
        adapter = CodexAdapter()
        with patch("bernstein.adapters.base.os.kill", return_value=None) as mock_kill:
            assert adapter.is_alive(1234) is True
        mock_kill.assert_called_once_with(1234, 0)

    def test_false_when_oserror(self) -> None:
        adapter = CodexAdapter()
        with patch("bernstein.adapters.base.os.kill", side_effect=OSError("no such process")):
            assert adapter.is_alive(9999) is False


class TestCodexKill:
    def test_calls_killpg(self) -> None:
        adapter = CodexAdapter()
        with (
            patch("bernstein.adapters.base.os.getpgid", return_value=555) as mock_getpgid,
            patch("bernstein.adapters.base.os.killpg") as mock_killpg,
        ):
            adapter.kill(555)
        mock_getpgid.assert_called_once_with(555)
        mock_killpg.assert_called_once_with(555, signal.SIGTERM)

    def test_does_not_raise_on_oserror(self) -> None:
        adapter = CodexAdapter()
        with (
            patch("bernstein.adapters.base.os.getpgid", return_value=556),
            patch("bernstein.adapters.base.os.killpg", side_effect=OSError("no process")),
        ):
            adapter.kill(556)  # must not raise


# ---------------------------------------------------------------------------
# detect_tier()
# ---------------------------------------------------------------------------


class TestCodexDetectTier:
    def test_returns_none_without_api_key(self) -> None:
        adapter = CodexAdapter()
        with patch.dict("os.environ", {}, clear=True):
            assert adapter.detect_tier() is None

    def test_enterprise_with_org_id(self) -> None:
        adapter = CodexAdapter()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test", "OPENAI_ORG_ID": "org-123"}, clear=True):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.ENTERPRISE
        assert info.provider == ProviderType.CODEX

    def test_pro_with_sk_proj_key(self) -> None:
        adapter = CodexAdapter()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-proj-abc123"}, clear=True):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.PRO

    def test_plus_with_sk_key(self) -> None:
        adapter = CodexAdapter()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-abc123"}, clear=True):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.PLUS

    def test_free_with_unknown_key_format(self) -> None:
        adapter = CodexAdapter()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "random-key"}, clear=True):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.FREE
