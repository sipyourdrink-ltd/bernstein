"""Unit tests for CloudflareAgentsAdapter spawn/name/env filtering."""

from __future__ import annotations

import signal
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.cloudflare_agents import CloudflareAgentsAdapter

if TYPE_CHECKING:
    from pathlib import Path


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
# CloudflareAgentsAdapter.name()
# ---------------------------------------------------------------------------


class TestCloudflareAdapterName:
    def test_name(self) -> None:
        assert CloudflareAgentsAdapter().name() == "Cloudflare Agents"


# ---------------------------------------------------------------------------
# CloudflareAgentsAdapter.spawn() — command construction
# ---------------------------------------------------------------------------


class TestCloudflareAdapterSpawn:
    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        proc_mock = _make_popen_mock(pid=100)
        with patch("bernstein.adapters.cloudflare_agents.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="cf-s1",
            )
        cmd = popen.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.orchestration.worker"]
        inner = _inner_cmd(cmd)
        assert inner[0] == "npx"
        assert inner[1] == "wrangler"
        assert inner[2] == "dev"

    def test_model_passed_as_var(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        proc_mock = _make_popen_mock(pid=101)
        with patch("bernstein.adapters.cloudflare_agents.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="claude-sonnet-4-20250514", effort="medium"),
                session_id="cf-s2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        var_indices = [i for i, v in enumerate(inner) if v == "--var"]
        model_vars = [inner[i + 1] for i in var_indices if inner[i + 1].startswith("AGENT_MODEL:")]
        assert len(model_vars) == 1
        assert model_vars[0] == "AGENT_MODEL:claude-sonnet-4-20250514"

    def test_prompt_passed_as_var(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        proc_mock = _make_popen_mock(pid=102)
        with patch("bernstein.adapters.cloudflare_agents.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="my-unique-prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="cf-s3",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        var_indices = [i for i, v in enumerate(inner) if v == "--var"]
        prompt_vars = [inner[i + 1] for i in var_indices if inner[i + 1].startswith("AGENT_PROMPT:")]
        assert len(prompt_vars) == 1
        assert "my-unique-prompt" in prompt_vars[0]

    def test_session_id_passed_as_var(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        proc_mock = _make_popen_mock(pid=103)
        with patch("bernstein.adapters.cloudflare_agents.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="cf-s4",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        var_indices = [i for i, v in enumerate(inner) if v == "--var"]
        session_vars = [inner[i + 1] for i in var_indices if inner[i + 1].startswith("AGENT_SESSION:")]
        assert len(session_vars) == 1
        assert session_vars[0] == "AGENT_SESSION:cf-s4"

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        proc_mock = _make_popen_mock(pid=104)
        with patch("bernstein.adapters.cloudflare_agents.subprocess.Popen", return_value=proc_mock):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="cf-s5",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_spawn_result_pid(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        proc_mock = _make_popen_mock(pid=106)
        with patch("bernstein.adapters.cloudflare_agents.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="cf-s6",
            )
        assert result.pid == 106

    def test_log_path_uses_session_id(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        proc_mock = _make_popen_mock(pid=107)
        with patch("bernstein.adapters.cloudflare_agents.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="my-cf-session",
            )
        assert result.log_path.name == "my-cf-session.log"

    def test_start_new_session_enabled(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        proc_mock = _make_popen_mock(pid=108)
        with patch("bernstein.adapters.cloudflare_agents.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="cf-s8",
            )
        kwargs = popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True

    def test_system_addendum_appended(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        proc_mock = _make_popen_mock(pid=109)
        with patch("bernstein.adapters.cloudflare_agents.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="do work",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="cf-s9",
                system_addendum="extra instructions",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        var_indices = [i for i, v in enumerate(inner) if v == "--var"]
        prompt_vars = [inner[i + 1] for i in var_indices if inner[i + 1].startswith("AGENT_PROMPT:")]
        assert "do work" in prompt_vars[0]
        assert "extra instructions" in prompt_vars[0]


# ---------------------------------------------------------------------------
# spawn() — env isolation
# ---------------------------------------------------------------------------


class TestCloudflareEnvIsolation:
    def test_env_contains_cf_keys(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        proc_mock = _make_popen_mock(pid=200)
        with (
            patch("bernstein.adapters.cloudflare_agents.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {
                    "CLOUDFLARE_ACCOUNT_ID": "abc123",
                    "CLOUDFLARE_API_TOKEN": "tok-secret",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="cf-env1",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "CLOUDFLARE_ACCOUNT_ID" in env
        assert env["CLOUDFLARE_ACCOUNT_ID"] == "abc123"
        assert "CLOUDFLARE_API_TOKEN" in env

    def test_env_excludes_unrelated_keys(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        proc_mock = _make_popen_mock(pid=201)
        with (
            patch("bernstein.adapters.cloudflare_agents.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {
                    "CLOUDFLARE_API_TOKEN": "tok-secret",
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
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="cf-env2",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "ANTHROPIC_API_KEY" not in env
        assert "DATABASE_URL" not in env

    def test_env_includes_path(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        proc_mock = _make_popen_mock(pid=202)
        with (
            patch("bernstein.adapters.cloudflare_agents.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict("os.environ", {"PATH": "/usr/bin", "CLOUDFLARE_API_TOKEN": "x"}, clear=True),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="cf-env3",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "PATH" in env


# ---------------------------------------------------------------------------
# Missing binary / PermissionError
# ---------------------------------------------------------------------------


class TestCloudflareSpawnMissingBinary:
    def test_file_not_found_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        with (
            patch(
                "bernstein.adapters.cloudflare_agents.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="npx not found"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="missing",
            )

    def test_permission_error_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        with (
            patch(
                "bernstein.adapters.cloudflare_agents.subprocess.Popen",
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


# ---------------------------------------------------------------------------
# Warnings and fast-exit
# ---------------------------------------------------------------------------


class TestCloudflareWarningsAndFastExit:
    def test_warns_when_account_id_missing(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        adapter = CloudflareAgentsAdapter()
        proc_mock = _make_popen_mock(pid=301)
        with (
            patch("bernstein.adapters.cloudflare_agents.subprocess.Popen", return_value=proc_mock),
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            caplog.at_level("WARNING"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="warn-missing-id",
            )
        assert "CLOUDFLARE_ACCOUNT_ID is not set" in caplog.text

    def test_warns_when_api_token_missing(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        adapter = CloudflareAgentsAdapter()
        proc_mock = _make_popen_mock(pid=302)
        with (
            patch("bernstein.adapters.cloudflare_agents.subprocess.Popen", return_value=proc_mock),
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            caplog.at_level("WARNING"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="warn-missing-token",
            )
        assert "CLOUDFLARE_API_TOKEN is not set" in caplog.text

    def test_fast_exit_rate_limit_raises(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        proc_mock = _make_popen_mock(pid=303)
        proc_mock.wait.return_value = 1
        with (
            patch("bernstein.adapters.cloudflare_agents.subprocess.Popen", return_value=proc_mock),
            patch.object(CloudflareAgentsAdapter, "_read_last_lines", return_value=["429 rate limit exceeded"]),
        ):
            with pytest.raises(RuntimeError, match="rate-limited"):
                adapter.spawn(
                    prompt="hello",
                    workdir=tmp_path,
                    model_config=ModelConfig(model="gpt-4o", effort="high"),
                    session_id="cf-fast-exit",
                )


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestCloudflareRegistry:
    def test_registered_in_adapter_registry(self) -> None:
        from bernstein.adapters.registry import get_adapter

        adapter = get_adapter("cloudflare")
        assert adapter.name() == "Cloudflare Agents"


# ---------------------------------------------------------------------------
# is_alive() and kill() — inherited from CLIAdapter base
# ---------------------------------------------------------------------------


class TestCloudflareIsAlive:
    def test_true_when_process_exists(self) -> None:
        adapter = CloudflareAgentsAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=True) as mock_alive:
            assert adapter.is_alive(1234) is True
        mock_alive.assert_called_once_with(1234)


class TestCloudflareKill:
    def test_calls_killpg(self) -> None:
        adapter = CloudflareAgentsAdapter()
        with patch("bernstein.adapters.base.kill_process_group") as mock_killpg:
            adapter.kill(555)
        mock_killpg.assert_called_once_with(555, signal.SIGTERM)
