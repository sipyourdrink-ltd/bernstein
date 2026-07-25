"""Unit tests for the OpenAI Agents SDK v2 adapter."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ApiTier, ModelConfig, ProviderType

from bernstein.adapters import openai_agents as adapter_module
from bernstein.adapters import openai_agents_runner as runner_module
from bernstein.adapters.openai_agents import TOOL_SOURCE_ENV_VAR, OpenAIAgentsAdapter
from bernstein.adapters.openai_agents_runner import (
    EXIT_GENERIC,
    EXIT_MANIFEST_ERROR,
    EXIT_OK,
    EXIT_RATE_LIMIT,
    EXIT_SDK_MISSING,
    MAX_TURNS_ENV_VAR,
    RunnerManifest,
    _append_tokens_sidecar,
    _build_agent_kwargs,
    _build_model_settings_kwargs,
    _is_rate_limit,
    _resolve_client_kwargs,
    _resolve_heartbeat_dir,
    _resolve_max_turns,
    _resolve_tokens_sidecar_path,
    _start_heartbeat,
    emit_event,
    load_manifest,
    main,
    run,
    validate_api_key_env_name,
)
from bernstein.adapters.plugin_sdk import (
    AdapterCapability,
    ensure_sampling_params_supported,
)
from bernstein.adapters.registry import get_adapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_popen_mock(pid: int) -> MagicMock:
    """Return a Popen-like stub that pretends the process is still running."""
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.wait.return_value = None
    return m


def _inner_cmd(full_cmd: list[str]) -> list[str]:
    """Extract the CLI command portion from a bernstein-worker wrapped command."""
    sep = full_cmd.index("--")
    return full_cmd[sep + 1 :]


# ---------------------------------------------------------------------------
# Plugin metadata
# ---------------------------------------------------------------------------


class TestPluginInfo:
    def test_name_is_openai_agents(self) -> None:
        info = OpenAIAgentsAdapter().plugin_info()
        assert info.name == "openai_agents"

    def test_version_is_set(self) -> None:
        info = OpenAIAgentsAdapter().plugin_info()
        assert info.version == "0.1.0"

    def test_capabilities_include_tool_use_and_streaming(self) -> None:
        info = OpenAIAgentsAdapter().plugin_info()
        assert AdapterCapability.STREAMING in info.capabilities
        assert AdapterCapability.TOOL_USE in info.capabilities
        assert AdapterCapability.MULTI_MODEL in info.capabilities
        assert AdapterCapability.RATE_LIMIT_DETECTION in info.capabilities
        assert AdapterCapability.STRUCTURED_OUTPUT in info.capabilities
        assert AdapterCapability.SUPPORTS_SAMPLING_PARAMS in info.capabilities

    def test_sampling_gate_passes_for_openai_agents(self) -> None:
        ensure_sampling_params_supported(
            OpenAIAgentsAdapter(),
            {"temperature": 0.5, "base_url": "http://localhost:8000/v1"},
        )

    def test_display_name(self) -> None:
        assert OpenAIAgentsAdapter().name() == "OpenAI Agents SDK"

    def test_supported_models_lists_launch_skus(self) -> None:
        models = OpenAIAgentsAdapter().supported_models()
        assert "gpt-5" in models
        assert "gpt-5-mini" in models
        assert "o4" in models

    def test_scoped_credential_keys(self) -> None:
        keys = OpenAIAgentsAdapter().scoped_credential_keys()
        assert keys == (
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_ORGANIZATION",
            "OPENAI_PROJECT",
        )


# ---------------------------------------------------------------------------
# Registry discovery
# ---------------------------------------------------------------------------


class TestRegistryDiscovery:
    def test_get_adapter_returns_openai_agents_instance(self) -> None:
        adapter = get_adapter("openai_agents")
        assert isinstance(adapter, OpenAIAgentsAdapter)


# ---------------------------------------------------------------------------
# health_check - tolerates missing SDK
# ---------------------------------------------------------------------------


class TestHealthCheck:
    def test_false_when_sdk_not_installed(self) -> None:
        adapter = OpenAIAgentsAdapter()
        with patch("importlib.util.find_spec", return_value=None):
            assert adapter.health_check() is False

    def test_true_when_sdk_present(self) -> None:
        adapter = OpenAIAgentsAdapter()
        with patch("importlib.util.find_spec", return_value=MagicMock()):
            assert adapter.health_check() is True


# ---------------------------------------------------------------------------
# spawn() - command construction
# ---------------------------------------------------------------------------


class TestSpawnCommand:
    def test_wrapped_with_bernstein_worker(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1001)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5-mini", effort="high"),
                session_id="oai-s1",
            )
        cmd = popen.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.orchestration.worker"]
        inner = _inner_cmd(cmd)
        assert inner[0] == sys.executable
        assert inner[1:3] == ["-m", "bernstein.adapters.openai_agents_runner"]
        assert "--manifest" in inner

    def test_manifest_path_is_passed(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1002)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-s2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        manifest_idx = inner.index("--manifest")
        manifest_path = inner[manifest_idx + 1]
        assert manifest_path.endswith("oai-s2.manifest.json")

    def test_manifest_file_written_with_spawn_params(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1003)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="explain module",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5-mini", effort="medium"),
                session_id="oai-s3",
                task_scope="small",
                budget_multiplier=2.0,
                system_addendum="do not run git push",
            )
        manifest = json.loads(
            (tmp_path / ".sdd" / "runtime" / "oai-s3.manifest.json").read_text(),
        )
        assert manifest["prompt"] == "explain module"
        assert manifest["model"] == "gpt-5-mini"
        assert manifest["effort"] == "medium"
        assert manifest["task_scope"] == "small"
        assert manifest["budget_multiplier"] == pytest.approx(2.0)
        assert manifest["system_addendum"] == "do not run git push"
        assert manifest["sandbox_provider"] == "unix_local"

    def test_manifest_honours_sandbox_provider_override(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1004)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="run tests",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-s4",
                mcp_config={"sandbox_provider": "e2b", "tools": [{"name": "file_read"}]},
            )
        manifest = json.loads(
            (tmp_path / ".sdd" / "runtime" / "oai-s4.manifest.json").read_text(),
        )
        assert manifest["sandbox_provider"] == "e2b"
        assert manifest["tools"] == [{"name": "file_read"}]

    def test_manifest_tool_source_defaults_to_gateway(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1104)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="run tests",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-tsg",
            )
        manifest = json.loads(
            (tmp_path / ".sdd" / "runtime" / "oai-tsg.manifest.json").read_text(),
        )
        assert manifest["tool_source"] == "gateway"

    def test_manifest_carries_builtin_tool_source(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1105)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="run tests",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-tsb",
                mcp_config={"tool_source": "builtin"},
            )
        manifest = json.loads(
            (tmp_path / ".sdd" / "runtime" / "oai-tsb.manifest.json").read_text(),
        )
        assert manifest["tool_source"] == "builtin"

    def test_env_var_enables_builtin_tool_source(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """BERNSTEIN_OPENAI_AGENTS_TOOL_SOURCE=builtin flips the default."""
        monkeypatch.setenv(TOOL_SOURCE_ENV_VAR, "builtin")
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1106)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="run tests",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-tse",
            )
        manifest = json.loads(
            (tmp_path / ".sdd" / "runtime" / "oai-tse.manifest.json").read_text(),
        )
        assert manifest["tool_source"] == "builtin"

    def test_env_var_applies_when_mcp_config_lacks_tool_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TOOL_SOURCE_ENV_VAR, "builtin")
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1107)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="run tests",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-tsf",
                mcp_config={"sandbox_provider": "e2b"},
            )
        manifest = json.loads(
            (tmp_path / ".sdd" / "runtime" / "oai-tsf.manifest.json").read_text(),
        )
        assert manifest["tool_source"] == "builtin"

    def test_explicit_mcp_config_tool_source_wins_over_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit mcp_config tool_source beats the env var."""
        monkeypatch.setenv(TOOL_SOURCE_ENV_VAR, "builtin")
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1108)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="run tests",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-tsx",
                mcp_config={"tool_source": "gateway"},
            )
        manifest = json.loads(
            (tmp_path / ".sdd" / "runtime" / "oai-tsx.manifest.json").read_text(),
        )
        assert manifest["tool_source"] == "gateway"

    def test_non_builtin_env_value_keeps_gateway(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TOOL_SOURCE_ENV_VAR, "gateway")
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1109)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="run tests",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-tsy",
            )
        manifest = json.loads(
            (tmp_path / ".sdd" / "runtime" / "oai-tsy.manifest.json").read_text(),
        )
        assert manifest["tool_source"] == "gateway"

    def test_manifest_includes_sampling_overrides(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1007)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="run tests",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-s6",
                mcp_config={
                    "temperature": 0.2,
                    "top_p": 0.9,
                    "top_k": 40,
                    "max_tokens": 1234,
                    "base_url": "http://localhost:8000/v1",
                    "api_key_env": "OPENROUTER_API_KEY",
                },
            )
        manifest = json.loads(
            (tmp_path / ".sdd" / "runtime" / "oai-s6.manifest.json").read_text(),
        )
        assert manifest["temperature"] == pytest.approx(0.2)
        assert manifest["top_p"] == pytest.approx(0.9)
        assert manifest["top_k"] == 40
        # mode-profile max_tokens must reach the manifest, not be overwritten
        # by the model_config fallback (regression: the override was dropped).
        assert manifest["max_tokens"] == 1234
        assert manifest["base_url"] == "http://localhost:8000/v1"
        assert manifest["api_key_env"] == "OPENROUTER_API_KEY"

    def test_manifest_max_tokens_falls_back_to_model_config(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1017)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="run tests",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high", max_tokens=4096),
                session_id="oai-s6b",
                mcp_config={"temperature": 0.2},
            )
        manifest = json.loads(
            (tmp_path / ".sdd" / "runtime" / "oai-s6b.manifest.json").read_text(),
        )
        # No mcp_config max_tokens -> model_config value is the fallback.
        assert manifest["max_tokens"] == 4096

    def test_manifest_omits_absent_sampling_fields(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1008)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="run tests",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-s7",
            )
        manifest = json.loads(
            (tmp_path / ".sdd" / "runtime" / "oai-s7.manifest.json").read_text(),
        )
        for key in ("temperature", "top_p", "top_k", "base_url", "api_key_env"):
            assert key not in manifest

    def test_manifest_carries_spawner_injected_heartbeat_dir(self, tmp_path: Path) -> None:
        """The spawner-injected heartbeat_dir must reach the runner manifest.

        Under default worktree isolation the spawn workdir is NOT the
        orchestrator root, so the manifest must carry the orchestrator-root
        heartbeat directory the HeartbeatMonitor polls.
        """
        orchestrator_root = tmp_path / "project"
        worktree = orchestrator_root / ".sdd" / "worktrees" / "oai-hb1"
        worktree.mkdir(parents=True)
        heartbeat_dir = str(orchestrator_root / ".sdd" / "runtime" / "heartbeats")
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1009)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="run tests",
                workdir=worktree,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-hb1",
                mcp_config={"heartbeat_dir": heartbeat_dir},
            )
        manifest = json.loads(
            (worktree / ".sdd" / "runtime" / "oai-hb1.manifest.json").read_text(),
        )
        assert manifest["heartbeat_dir"] == heartbeat_dir
        assert manifest["workdir"] == str(worktree)

    def test_sampling_overrides_survive_real_mcp_merge_into_manifest(self, tmp_path: Path) -> None:
        """End-to-end: sampling keys survive the MCPManager merge to the manifest.

        Mirrors the spawner flow where the operator-provided MCP config is
        rebuilt by ``MCPManager.build_mcp_config_for_task`` before it
        reaches the adapter. The merged config must still deliver the
        sampling/endpoint overrides into the runner manifest.
        """
        from bernstein.core.mcp_manager import MCPManager, MCPServerConfig

        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        with patch(
            "bernstein.core.protocols.mcp_manager.subprocess.Popen",
            return_value=mock_proc,
        ):
            mgr = MCPManager([MCPServerConfig(name="github", command=["npx"])])
            mgr.start_all()
            base = {
                "mcpServers": {"tavily": {"command": "npx", "args": ["tavily"]}},
                "temperature": 0.2,
                "top_p": 0.9,
                "top_k": 40,
                "base_url": "http://localhost:8000/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            }
            merged = mgr.build_mcp_config_for_task(
                task_mcp_servers=["github"],
                base_config=base,
            )

        assert merged is not None
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1010)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="run tests",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-merge1",
                mcp_config=merged,
            )
        manifest = json.loads(
            (tmp_path / ".sdd" / "runtime" / "oai-merge1.manifest.json").read_text(),
        )
        assert manifest["temperature"] == pytest.approx(0.2)
        assert manifest["top_p"] == pytest.approx(0.9)
        assert manifest["top_k"] == 40
        assert manifest["base_url"] == "http://localhost:8000/v1"
        assert manifest["api_key_env"] == "OPENROUTER_API_KEY"
        assert set(manifest["mcp_servers"]) == {"tavily", "github"}

    def test_spawn_rejects_non_credential_api_key_env(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        with (
            patch("bernstein.adapters.openai_agents.subprocess.Popen") as popen,
            pytest.raises(RuntimeError, match="PATH"),
        ):
            adapter.spawn(
                prompt="run tests",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-bad-env",
                mcp_config={"api_key_env": "PATH"},
            )
        popen.assert_not_called()

    def test_log_path_uses_session_id(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1005)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5-mini", effort="high"),
                session_id="oai-named-session",
            )
        assert result.log_path.name == "oai-named-session.log"

    def test_start_new_session_enabled(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1006)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-s5",
            )
        assert popen.call_args.kwargs.get("start_new_session") is True


# ---------------------------------------------------------------------------
# spawn() - env isolation
# ---------------------------------------------------------------------------


class TestSpawnEnvIsolation:
    def test_env_contains_openai_keys(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=2001)
        with (
            patch(
                "bernstein.adapters.openai_agents.subprocess.Popen",
                return_value=proc_mock,
            ) as popen,
            patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "sk-test",
                    "OPENAI_ORGANIZATION": "org-123",
                    "OPENAI_PROJECT": "proj-abc",
                    "OPENAI_BASE_URL": "https://api.openai.com/v1",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5-mini", effort="high"),
                session_id="oai-env1",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env["OPENAI_API_KEY"] == "sk-test"
        assert env["OPENAI_ORGANIZATION"] == "org-123"
        assert env["OPENAI_PROJECT"] == "proj-abc"
        assert env["OPENAI_BASE_URL"] == "https://api.openai.com/v1"

    def test_env_excludes_unrelated_keys(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=2002)
        with (
            patch(
                "bernstein.adapters.openai_agents.subprocess.Popen",
                return_value=proc_mock,
            ) as popen,
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
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-env2",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "ANTHROPIC_API_KEY" not in env
        assert "DATABASE_URL" not in env

    def test_env_passes_api_key_env_override_through(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=2003)
        with (
            patch(
                "bernstein.adapters.openai_agents.subprocess.Popen",
                return_value=proc_mock,
            ) as popen,
            patch.dict(
                "os.environ",
                {"OPENROUTER_API_KEY": "sk-proxy", "PATH": "/usr/bin"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5-mini", effort="high"),
                session_id="oai-env3",
                mcp_config={"api_key_env": "OPENROUTER_API_KEY"},
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env["OPENROUTER_API_KEY"] == "sk-proxy"


# ---------------------------------------------------------------------------
# spawn() - missing binary / PermissionError
# ---------------------------------------------------------------------------


class TestSpawnMissingBinary:
    def test_file_not_found_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        with (
            patch(
                "bernstein.adapters.openai_agents.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="not found"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5-mini", effort="high"),
                session_id="missing",
            )

    def test_permission_error_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        with (
            patch(
                "bernstein.adapters.openai_agents.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="perm-denied",
            )


# ---------------------------------------------------------------------------
# spawn() - warnings
# ---------------------------------------------------------------------------


class TestSpawnWarnings:
    def test_warns_when_openai_api_key_missing(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=3001)
        with (
            patch(
                "bernstein.adapters.openai_agents.subprocess.Popen",
                return_value=proc_mock,
            ),
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            caplog.at_level("WARNING"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5-mini", effort="high"),
                session_id="warn-missing-key",
            )
        assert "OPENAI_API_KEY is not set" in caplog.text

    def test_fast_exit_rate_limit_raises(self, tmp_path: Path) -> None:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=3002)
        proc_mock.wait.return_value = 1
        with (
            patch(
                "bernstein.adapters.openai_agents.subprocess.Popen",
                return_value=proc_mock,
            ),
            patch.object(
                OpenAIAgentsAdapter,
                "_read_last_lines",
                return_value=["429 rate limit exceeded"],
            ),
            pytest.raises(RuntimeError, match="rate-limited"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oai-fast-exit",
            )


# ---------------------------------------------------------------------------
# detect_tier()
# ---------------------------------------------------------------------------


class TestDetectTier:
    def test_returns_none_without_api_key(self) -> None:
        adapter = OpenAIAgentsAdapter()
        with patch.dict("os.environ", {}, clear=True):
            assert adapter.detect_tier() is None

    def test_enterprise_with_org_id(self) -> None:
        adapter = OpenAIAgentsAdapter()
        with patch.dict(
            "os.environ",
            {"OPENAI_API_KEY": "sk-test", "OPENAI_ORGANIZATION": "org-123"},
            clear=True,
        ):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.ENTERPRISE
        assert info.provider == ProviderType.CODEX

    def test_pro_with_sk_proj_key(self) -> None:
        adapter = OpenAIAgentsAdapter()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-proj-abc"}, clear=True):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.PRO

    def test_plus_with_sk_key(self) -> None:
        adapter = OpenAIAgentsAdapter()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-abc"}, clear=True):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.PLUS

    def test_free_with_unknown_key_format(self) -> None:
        adapter = OpenAIAgentsAdapter()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "random-key"}, clear=True):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.FREE

    def test_legacy_openai_org_id_also_marks_enterprise(self) -> None:
        adapter = OpenAIAgentsAdapter()
        with patch.dict(
            "os.environ",
            {"OPENAI_API_KEY": "sk-test", "OPENAI_ORG_ID": "org-legacy"},
            clear=True,
        ):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.ENTERPRISE


# ---------------------------------------------------------------------------
# Runner manifest
# ---------------------------------------------------------------------------


class TestRunnerManifest:
    def test_from_dict_uses_defaults(self) -> None:
        manifest = RunnerManifest.from_dict(
            {
                "session_id": "s1",
                "prompt": "hi",
                "workdir": "/workspace",
                "model": "gpt-5-mini",
            },
        )
        assert manifest.effort == "high"
        assert manifest.sandbox_provider == "unix_local"
        assert manifest.timeout_seconds == 1800
        assert manifest.tools == []
        assert manifest.mcp_servers == {}

    def test_from_dict_ignores_unknown_keys(self) -> None:
        manifest = RunnerManifest.from_dict(
            {
                "session_id": "s1",
                "prompt": "hi",
                "workdir": "/workspace",
                "model": "gpt-5-mini",
                "future_field": "ignored",
            },
        )
        assert manifest.session_id == "s1"

    def test_load_manifest_roundtrip(self, tmp_path: Path) -> None:
        payload = {
            "session_id": "s1",
            "prompt": "hi",
            "workdir": str(tmp_path),
            "model": "gpt-5-mini",
            "sandbox_provider": "docker",
            "tools": [{"name": "file_read"}],
        }
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        manifest = load_manifest(path)
        assert manifest.sandbox_provider == "docker"
        assert manifest.tools == [{"name": "file_read"}]

    def test_load_manifest_rejects_non_object_root(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(TypeError):
            load_manifest(path)

    def test_sampling_fields_default_to_none(self) -> None:
        manifest = RunnerManifest.from_dict(
            {
                "session_id": "s1",
                "prompt": "hi",
                "workdir": "/workspace",
                "model": "gpt-5-mini",
            },
        )
        assert manifest.temperature is None
        assert manifest.top_p is None
        assert manifest.top_k is None
        assert manifest.base_url is None
        assert manifest.api_key_env is None

    def test_from_dict_parses_sampling_fields(self) -> None:
        manifest = RunnerManifest.from_dict(
            {
                "session_id": "s1",
                "prompt": "hi",
                "workdir": "/workspace",
                "model": "gpt-5-mini",
                "temperature": 0.2,
                "top_p": 0.9,
                "top_k": 40,
                "base_url": "http://localhost:8000/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            },
        )
        assert manifest.temperature == pytest.approx(0.2)
        assert manifest.top_p == pytest.approx(0.9)
        assert manifest.top_k == 40
        assert manifest.base_url == "http://localhost:8000/v1"
        assert manifest.api_key_env == "OPENROUTER_API_KEY"

    def test_tool_source_defaults_to_gateway(self) -> None:
        manifest = RunnerManifest.from_dict(
            {
                "session_id": "s1",
                "prompt": "hi",
                "workdir": "/workspace",
                "model": "gpt-5-mini",
            },
        )
        assert manifest.tool_source == "gateway"

    def test_tool_source_parses_builtin(self) -> None:
        manifest = RunnerManifest.from_dict(
            {
                "session_id": "s1",
                "prompt": "hi",
                "workdir": "/workspace",
                "model": "gpt-5-mini",
                "tool_source": "builtin",
            },
        )
        assert manifest.tool_source == "builtin"


# ---------------------------------------------------------------------------
# Runner helpers
# ---------------------------------------------------------------------------


class TestRunnerHelpers:
    def test_build_agent_kwargs_includes_instructions(self) -> None:
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/workspace",
            model="gpt-5",
            system_addendum="be terse",
            tools=[{"name": "file_read"}],
        )
        kwargs = _build_agent_kwargs(manifest)
        assert kwargs["name"] == "bernstein-s"
        assert kwargs["model"] == "gpt-5"
        assert kwargs["instructions"] == "be terse"
        assert kwargs["tools"] == [{"name": "file_read"}]

    def test_build_agent_kwargs_omits_optional(self) -> None:
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/workspace",
            model="gpt-5",
        )
        kwargs = _build_agent_kwargs(manifest)
        assert "instructions" not in kwargs
        assert "tools" not in kwargs

    def test_build_agent_kwargs_omits_gateway_tools_for_builtin_source(self) -> None:
        # When builtins are selected the gateway descriptors are not attached
        # here; the runner constructs SDK builtins later in ``_run_session``.
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/workspace",
            model="gpt-5",
            tools=[{"name": "file_read"}],
            tool_source="builtin",
        )
        kwargs = _build_agent_kwargs(manifest)
        assert "tools" not in kwargs

    def test_build_agent_kwargs_keeps_gateway_tools_by_default(self) -> None:
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/workspace",
            model="gpt-5",
            tools=[{"name": "file_read"}],
        )
        kwargs = _build_agent_kwargs(manifest)
        assert kwargs["tools"] == [{"name": "file_read"}]

    def test_run_sync_not_called_with_dict_run_config(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Regression: a plain-dict ``run_config`` crashes the SDK.

        ``agents.run`` accesses ``run_config.session_input_callback`` on
        whatever is passed, so handing it a dict raised AttributeError on
        every spawn. The runner must not pass ``run_config`` at all unless
        it is a real ``agents.RunConfig`` instance.
        """
        fake_agent = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(Agent=MagicMock(return_value=fake_agent), Runner=fake_runner)
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/abs",
            model="MiniMax-M3",
            sandbox_provider="e2b",
            mcp_servers={"bernstein": {"command": "python"}},
        )
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(manifest)
        assert rc == EXIT_OK
        _, kwargs = fake_runner.run_sync.call_args
        assert not isinstance(kwargs.get("run_config"), dict), (
            "run_config must never be a plain dict - the SDK requires a real agents.RunConfig instance"
        )
        assert "run_config" not in kwargs

    def test_resolve_max_turns_defaults_to_30(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bug 13: the SDK default of 10 killed builtin-tool workflows - the
        shipped default is now 30 (tuning.agent.max_turns)."""
        from bernstein.core import defaults as core_defaults

        monkeypatch.delenv(MAX_TURNS_ENV_VAR, raising=False)
        core_defaults.reset()
        assert _resolve_max_turns() == 30

    def test_resolve_max_turns_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MAX_TURNS_ENV_VAR, "200")
        assert _resolve_max_turns() == 200

    def test_resolve_max_turns_tuning_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bernstein.core import defaults as core_defaults

        monkeypatch.delenv(MAX_TURNS_ENV_VAR, raising=False)
        core_defaults.override("agent", {"max_turns": 80})
        try:
            assert _resolve_max_turns() == 80
        finally:
            core_defaults.reset()

    def test_resolve_max_turns_env_beats_tuning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bernstein.core import defaults as core_defaults

        core_defaults.override("agent", {"max_turns": 80})
        monkeypatch.setenv(MAX_TURNS_ENV_VAR, "200")
        try:
            assert _resolve_max_turns() == 200
        finally:
            core_defaults.reset()

    def test_resolve_max_turns_unparseable_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bernstein.core import defaults as core_defaults

        core_defaults.reset()
        monkeypatch.setenv(MAX_TURNS_ENV_VAR, "notanumber")
        assert _resolve_max_turns() == 30

    def test_resolve_max_turns_zero_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 0 max_turns would fail every run on turn one - reject, don't forward."""
        from bernstein.core import defaults as core_defaults

        core_defaults.reset()
        monkeypatch.setenv(MAX_TURNS_ENV_VAR, "0")
        assert _resolve_max_turns() == 30

    def test_resolve_max_turns_negative_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bernstein.core import defaults as core_defaults

        core_defaults.reset()
        monkeypatch.setenv(MAX_TURNS_ENV_VAR, "-5")
        assert _resolve_max_turns() == 30

    def test_resolve_max_turns_negative_env_falls_back_to_tuning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-positive env value falls through to tuning.agent.max_turns, not just None."""
        from bernstein.core import defaults as core_defaults

        monkeypatch.setenv(MAX_TURNS_ENV_VAR, "-5")
        core_defaults.override("agent", {"max_turns": 80})
        try:
            assert _resolve_max_turns() == 80
        finally:
            core_defaults.reset()

    def test_log_max_turns_exceeded_reports_turns_and_completed_work(self, caplog: pytest.LogCaptureFixture) -> None:
        """Bug 13: hitting the cap must WARN with the configured cap, turns
        actually used, and whether the agent had already POSTed /complete."""
        from types import SimpleNamespace

        from bernstein.adapters.openai_agents_runner import _log_max_turns_exceeded

        run_data = SimpleNamespace(
            raw_responses=[SimpleNamespace(usage=None) for _ in range(10)],
            new_items=[
                SimpleNamespace(
                    raw_item=SimpleNamespace(
                        arguments='{"command": "curl -X POST http://localhost:8052/tasks/abc/complete"}'
                    )
                )
            ],
        )
        exc = RuntimeError("Max turns (10) exceeded")
        exc.run_data = run_data  # type: ignore[attr-defined]

        with caplog.at_level("WARNING", logger="bernstein.adapters.openai_agents_runner"):
            _log_max_turns_exceeded(exc, 10)

        assert any(
            "max_turns=10" in rec.getMessage()
            and "turns_used=10" in rec.getMessage()
            and "work_already_completed=yes" in rec.getMessage()
            for rec in caplog.records
        ), f"missing/incomplete MaxTurns warning: {[r.getMessage() for r in caplog.records]}"

    def test_log_max_turns_exceeded_detects_cli_completion(self, caplog: pytest.LogCaptureFixture) -> None:
        """A completion run through the CLI must still read as completed work.

        Agents mark tasks done with `bernstein task complete`, which carries no
        `/complete` path. Matching only the path shape would report finished
        work as lost and send an operator hunting a failure that did not happen.
        """
        from types import SimpleNamespace

        from bernstein.adapters.openai_agents_runner import _log_max_turns_exceeded

        run_data = SimpleNamespace(
            raw_responses=[SimpleNamespace(usage=None) for _ in range(10)],
            new_items=[
                SimpleNamespace(
                    raw_item=SimpleNamespace(
                        arguments='{"command": "bernstein task complete abc --summary \\"done\\""}'
                    )
                )
            ],
        )
        exc = RuntimeError("Max turns (10) exceeded")
        exc.run_data = run_data  # type: ignore[attr-defined]

        with caplog.at_level("WARNING", logger="bernstein.adapters.openai_agents_runner"):
            _log_max_turns_exceeded(exc, 10)

        assert any("work_already_completed=yes" in rec.getMessage() for rec in caplog.records), (
            f"CLI completion not detected: {[r.getMessage() for r in caplog.records]}"
        )

    def test_log_max_turns_exceeded_handles_missing_run_data(self, caplog: pytest.LogCaptureFixture) -> None:
        """No run_data on the exception must still produce a WARNING with
        unknowns, never a crash."""
        from bernstein.adapters.openai_agents_runner import _log_max_turns_exceeded

        with caplog.at_level("WARNING", logger="bernstein.adapters.openai_agents_runner"):
            _log_max_turns_exceeded(RuntimeError("Max turns (30) exceeded"), None)

        assert any(
            "max_turns=SDK-default" in rec.getMessage()
            and "turns_used=unknown" in rec.getMessage()
            and "work_already_completed=unknown" in rec.getMessage()
            for rec in caplog.records
        )

    def test_build_model_settings_kwargs_empty_when_absent(self) -> None:
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/workspace",
            model="gpt-5",
        )
        assert _build_model_settings_kwargs(manifest) == {}

    def test_build_model_settings_kwargs_maps_params(self) -> None:
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/workspace",
            model="gpt-5",
            temperature=0.2,
            top_p=0.9,
            top_k=40,
        )
        kwargs = _build_model_settings_kwargs(manifest)
        assert kwargs["temperature"] == pytest.approx(0.2)
        assert kwargs["top_p"] == pytest.approx(0.9)
        # top_k travels via extra_args - the OpenAI API has no first-class
        # top_k field but OpenAI-compatible endpoints accept it.
        assert kwargs["extra_args"] == {"top_k": 40}

    def test_build_model_settings_kwargs_omits_max_tokens_without_sdk(self) -> None:
        # Without an SDK class to probe, max_tokens is not forwarded so the
        # runner never passes a kwarg the installed SDK might reject.
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/workspace",
            model="gpt-5",
            max_tokens=4096,
        )
        assert "max_tokens" not in _build_model_settings_kwargs(manifest)

    def test_build_model_settings_kwargs_forwards_max_tokens_when_sdk_accepts(self) -> None:
        import dataclasses

        @dataclasses.dataclass
        class _FakeModelSettings:
            max_tokens: int | None = None

        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/workspace",
            model="gpt-5",
            max_tokens=4096,
        )
        kwargs = _build_model_settings_kwargs(manifest, model_settings_cls=_FakeModelSettings)
        assert kwargs["max_tokens"] == 4096

    def test_build_model_settings_kwargs_skips_max_tokens_when_sdk_lacks_field(self) -> None:
        import dataclasses

        @dataclasses.dataclass
        class _FakeModelSettingsNoMax:
            temperature: float | None = None

        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/workspace",
            model="gpt-5",
            max_tokens=4096,
        )
        kwargs = _build_model_settings_kwargs(manifest, model_settings_cls=_FakeModelSettingsNoMax)
        assert "max_tokens" not in kwargs

    def test_resolve_client_kwargs_empty_by_default(self) -> None:
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/workspace",
            model="gpt-5",
        )
        assert _resolve_client_kwargs(manifest) == {}

    def test_resolve_client_kwargs_reads_key_from_env(self) -> None:
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/workspace",
            model="gpt-5",
            base_url="http://localhost:8000/v1",
            api_key_env="OPENROUTER_API_KEY",
        )
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-proxy"}, clear=True):
            kwargs = _resolve_client_kwargs(manifest)
        assert kwargs == {"base_url": "http://localhost:8000/v1", "api_key": "sk-proxy"}

    def test_resolve_client_kwargs_raises_when_env_var_missing(self) -> None:
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/workspace",
            model="gpt-5",
            api_key_env="OPENROUTER_API_KEY",
        )
        with (
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"),
        ):
            _resolve_client_kwargs(manifest)

    @pytest.mark.parametrize(
        "name",
        [
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "HF_TOKEN",
            "DEEPSEEK_API_KEY",
            "MISTRAL_API_KEY",
        ],
    )
    def test_validate_api_key_env_name_accepts_allowlisted_names(self, name: str) -> None:
        validate_api_key_env_name(name)

    def test_validate_api_key_env_name_accepts_operator_allowed_name(self) -> None:
        """Names outside the built-in set pass only with the host override."""
        with patch.dict("os.environ", {"BERNSTEIN_ALLOWED_API_KEY_ENVS": "MY_PROXY_KEY, OTHER_KEY"}):
            validate_api_key_env_name("MY_PROXY_KEY")
            validate_api_key_env_name("OTHER_KEY")

    @pytest.mark.parametrize(
        "name",
        [
            "PATH",
            "HOME",
            "LD_PRELOAD",
            "my_proxy_key",
            "OPENAI-API-KEY",
            "1KEY_TOKEN",
            "_API_KEY",
            "SSH_AUTH_SOCK",
            "KEY",
            "TOKEN",
            "GITHUB_TOKEN",
            "AWS_SESSION_TOKEN",
            "STRIPE_SECRET_KEY",
            "MY_PROXY_KEY",
        ],
    )
    def test_validate_api_key_env_name_rejects_non_credential_names(self, name: str) -> None:
        with pytest.raises(RuntimeError, match="api_key_env"):
            validate_api_key_env_name(name)

    def test_resolve_client_kwargs_rejects_non_credential_name(self) -> None:
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/workspace",
            model="gpt-5",
            api_key_env="PATH",
        )
        with (
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            pytest.raises(RuntimeError, match="PATH"),
        ):
            _resolve_client_kwargs(manifest)

    def test_is_rate_limit_detects_429_message(self) -> None:
        assert _is_rate_limit(RuntimeError("429 Too Many Requests")) is True

    def test_is_rate_limit_detects_class_name(self) -> None:
        class RateLimitError(Exception):
            pass

        assert _is_rate_limit(RateLimitError("boom")) is True

    def test_is_rate_limit_negative(self) -> None:
        assert _is_rate_limit(RuntimeError("unrelated bug")) is False


class TestRunnerEmitEvent:
    def test_emit_event_writes_single_line(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        emit_event({"type": "start", "session_id": "s"})
        out = capsys.readouterr().out
        assert out.endswith("\n")
        parsed = json.loads(out.strip())
        assert parsed == {"type": "start", "session_id": "s"}

    def test_emit_event_handles_non_serializable(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        emit_event({"type": "oops", "obj": object()})
        out = capsys.readouterr().out
        parsed = json.loads(out.strip())
        assert parsed["type"] == "error"


# ---------------------------------------------------------------------------
# Runner.run - SDK lifecycle (mocked)
# ---------------------------------------------------------------------------


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int, tool_calls: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.tool_calls = tool_calls


class _FakeResult:
    def __init__(
        self,
        summary: str = "done",
        usage: _FakeUsage | None = None,
        raw_responses: list[Any] | None = None,
    ) -> None:
        self.final_output = summary
        self.usage = usage
        self.raw_responses = raw_responses if raw_responses is not None else []


class _FakeRawResponseUsage:
    """Mimics ``agents.usage.Usage`` as embedded on ``ModelResponse.usage``."""

    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeRawResponse:
    """Mimics ``agents.items.ModelResponse`` - the ``result.raw_responses`` element."""

    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.usage = _FakeRawResponseUsage(input_tokens, output_tokens)


class TestRunnerRun:
    def _manifest(self) -> RunnerManifest:
        return RunnerManifest(
            session_id="abc",
            prompt="hello",
            workdir="/workspace",
            model="gpt-5-mini",
        )

    def test_run_returns_zero_on_success(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_agent = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(
            summary="ok",
            usage=_FakeUsage(10, 20, 1),
        )
        fake_sdk = MagicMock(Agent=MagicMock(return_value=fake_agent), Runner=fake_runner)
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(self._manifest())
        assert rc == EXIT_OK
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        types = [e["type"] for e in events]
        assert "start" in types
        assert "usage" in types
        assert "completion" in types
        usage_event = next(e for e in events if e["type"] == "usage")
        assert usage_event["input_tokens"] == 10
        assert usage_event["output_tokens"] == 20
        assert usage_event["tool_calls"] == 1

    def test_run_forwards_max_turns_30_by_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Bug 13: unset BERNSTEIN_MAX_TURNS / tuning -> the shipped default
        of 30 is forwarded (the SDK's own default of 10 killed builtin-tool
        workflows mid-flight)."""
        from bernstein.core import defaults as core_defaults

        monkeypatch.delenv(MAX_TURNS_ENV_VAR, raising=False)
        core_defaults.reset()
        fake_agent = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(Agent=MagicMock(return_value=fake_agent), Runner=fake_runner)
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(self._manifest())
        assert rc == EXIT_OK
        _, kwargs = fake_runner.run_sync.call_args
        assert kwargs["max_turns"] == 30

    def test_run_forwards_max_turns_from_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(MAX_TURNS_ENV_VAR, "200")
        fake_agent = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(Agent=MagicMock(return_value=fake_agent), Runner=fake_runner)
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(self._manifest())
        assert rc == EXIT_OK
        _, kwargs = fake_runner.run_sync.call_args
        assert kwargs["max_turns"] == 200

    def test_run_without_usage_still_emits_completion(
        self,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``result.usage`` is None AND there is no ``raw_responses`` fallback data:
        a usage_missing event fires (not a silently dropped usage event) and a
        WARNING is logged - see the raw_responses-fallback tests below for the
        bug-13-followup fallback path itself."""
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok", usage=None)
        fake_sdk = MagicMock(Agent=MagicMock(), Runner=fake_runner)
        with caplog.at_level(logging.WARNING):
            with patch.dict(sys.modules, {"agents": fake_sdk}):
                rc = run(self._manifest())
        assert rc == EXIT_OK
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        assert any(e["type"] == "completion" for e in events)
        usage_event = next(e for e in events if e["type"] == "usage")
        assert usage_event["input_tokens"] == 0
        assert usage_event["output_tokens"] == 0
        assert usage_event["usage_missing"] is True
        assert any("no usage data" in rec.message for rec in caplog.records)

    def test_run_emits_sdk_missing_when_import_fails(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Simulate ImportError for the agents package by registering a
        # placeholder whose attribute access raises ImportError - we must
        # also ensure the import itself fails.
        with patch.dict(sys.modules, {"agents": None}):
            rc = run(self._manifest())
        assert rc == EXIT_SDK_MISSING
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        assert any(e["type"] == "error" and e["kind"] == "sdk_missing" for e in events)

    def test_run_emits_rate_limit_on_429(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_runner = MagicMock()
        fake_runner.run_sync.side_effect = RuntimeError("HTTP 429 rate limit")
        fake_sdk = MagicMock(Agent=MagicMock(), Runner=fake_runner)
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(self._manifest())
        assert rc == EXIT_RATE_LIMIT
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        assert any(e["type"] == "error" and e["kind"] == "rate_limit" for e in events)

    def test_run_emits_runtime_error_on_generic_exception(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_runner = MagicMock()
        fake_runner.run_sync.side_effect = RuntimeError("something else")
        fake_sdk = MagicMock(Agent=MagicMock(), Runner=fake_runner)
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(self._manifest())
        assert rc == EXIT_GENERIC
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        assert any(e["type"] == "error" and e["kind"] == "runtime" for e in events)

    def test_run_start_event_logs_sampling_and_endpoint_params(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        manifest = RunnerManifest(
            session_id="abc",
            prompt="hello",
            workdir="/workspace",
            model="gpt-5-mini",
            temperature=0.2,
            top_p=0.9,
            top_k=40,
            base_url="http://localhost:8000/v1",
            api_key_env="OPENROUTER_API_KEY",
        )
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(Agent=MagicMock(), Runner=fake_runner)
        fake_openai = MagicMock()
        with (
            patch.dict(sys.modules, {"agents": fake_sdk, "openai": fake_openai}),
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-proxy"}, clear=True),
        ):
            rc = run(manifest)
        assert rc == EXIT_OK
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        start = next(e for e in events if e["type"] == "start")
        assert start["temperature"] == pytest.approx(0.2)
        assert start["top_p"] == pytest.approx(0.9)
        assert start["top_k"] == 40
        assert start["base_url"] == "http://localhost:8000/v1"
        # Only the env var NAME is logged - never the key value.
        assert start["api_key_env"] == "OPENROUTER_API_KEY"
        assert "sk-proxy" not in json.dumps(events)

    def test_run_start_event_defaults_are_null(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(Agent=MagicMock(), Runner=fake_runner)
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            run(self._manifest())
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        start = next(e for e in events if e["type"] == "start")
        for key in ("temperature", "top_p", "top_k", "base_url", "api_key_env"):
            assert start[key] is None

    def test_run_wires_model_settings_into_agent(self) -> None:
        manifest = RunnerManifest(
            session_id="abc",
            prompt="hello",
            workdir="/workspace",
            model="gpt-5-mini",
            temperature=0.3,
            top_p=0.8,
            top_k=20,
        )
        settings_sentinel = object()
        fake_agent_cls = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(
            Agent=fake_agent_cls,
            Runner=fake_runner,
            ModelSettings=MagicMock(return_value=settings_sentinel),
        )
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(manifest)
        assert rc == EXIT_OK
        fake_sdk.ModelSettings.assert_called_once_with(
            temperature=0.3,
            top_p=0.8,
            extra_args={"top_k": 20},
        )
        assert fake_agent_cls.call_args.kwargs["model_settings"] is settings_sentinel

    def test_run_attaches_builtin_tools_when_tool_source_builtin(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        manifest = RunnerManifest(
            session_id="abc",
            prompt="hello",
            workdir=str(tmp_path),
            model="gpt-5-mini",
            tools=[{"name": "gateway_tool"}],
            tool_source="builtin",
        )
        fake_agent_cls = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        # Identity decorator so the wrapped function object flows through.
        fake_sdk = MagicMock(
            Agent=fake_agent_cls,
            Runner=fake_runner,
            function_tool=lambda fn: fn,
        )
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(manifest)
        assert rc == EXIT_OK
        # Builtins replace the gateway descriptors entirely. Under the bare
        # local path (``unix_local``) ``run_command`` is withheld: only the
        # three workdir-confined file tools are attached.
        attached = fake_agent_cls.call_args.kwargs["tools"]
        assert [t.__name__ for t in attached] == ["read_file", "write_file", "list_dir"]
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        assert any(e["type"] == "progress" and e.get("tool_source") == "builtin" for e in events)

    def test_run_attaches_run_command_under_os_sandbox_provider(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        manifest = RunnerManifest(
            session_id="abc",
            prompt="hello",
            workdir=str(tmp_path),
            model="gpt-5-mini",
            tool_source="builtin",
            sandbox_provider="docker",
        )
        fake_agent_cls = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(
            Agent=fake_agent_cls,
            Runner=fake_runner,
            function_tool=lambda fn: fn,
        )
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(manifest)
        assert rc == EXIT_OK
        # An OS sandbox provides real filesystem confinement, so run_command
        # is exposed alongside the file tools.
        attached = fake_agent_cls.call_args.kwargs["tools"]
        assert [t.__name__ for t in attached] == [
            "read_file",
            "write_file",
            "list_dir",
            "run_command",
        ]
        capsys.readouterr()

    def test_run_uses_gateway_tools_by_default(self, tmp_path: Path) -> None:
        manifest = RunnerManifest(
            session_id="abc",
            prompt="hello",
            workdir=str(tmp_path),
            model="gpt-5-mini",
            tools=[{"name": "gateway_tool"}],
        )
        fake_agent_cls = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(Agent=fake_agent_cls, Runner=fake_runner)
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(manifest)
        assert rc == EXIT_OK
        assert fake_agent_cls.call_args.kwargs["tools"] == [{"name": "gateway_tool"}]

    def test_run_skips_model_settings_when_no_params(self) -> None:
        fake_agent_cls = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(Agent=fake_agent_cls, Runner=fake_runner)
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            run(self._manifest())
        fake_sdk.ModelSettings.assert_not_called()
        assert "model_settings" not in fake_agent_cls.call_args.kwargs

    def test_run_constructs_client_from_manifest(self) -> None:
        manifest = RunnerManifest(
            session_id="abc",
            prompt="hello",
            workdir="/workspace",
            model="gpt-5-mini",
            base_url="http://localhost:8000/v1",
            api_key_env="OPENROUTER_API_KEY",
        )
        client_sentinel = object()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(Agent=MagicMock(), Runner=fake_runner)
        fake_openai = MagicMock(AsyncOpenAI=MagicMock(return_value=client_sentinel))
        with (
            patch.dict(sys.modules, {"agents": fake_sdk, "openai": fake_openai}),
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-proxy"}, clear=True),
        ):
            rc = run(manifest)
        assert rc == EXIT_OK
        fake_openai.AsyncOpenAI.assert_called_once_with(
            base_url="http://localhost:8000/v1",
            api_key="sk-proxy",
        )
        # With a custom endpoint the client must be excluded from tracing
        # (never send the third-party key to api.openai.com) and the SDK
        # must use the chat-completions API (third-party endpoints do not
        # serve /responses).
        fake_sdk.set_default_openai_client.assert_called_once_with(
            client_sentinel,
            use_for_tracing=False,
        )
        fake_sdk.set_default_openai_api.assert_called_once_with("chat_completions")

    def test_run_keeps_default_api_and_tracing_without_base_url(self) -> None:
        manifest = RunnerManifest(
            session_id="abc",
            prompt="hello",
            workdir="/workspace",
            model="gpt-5-mini",
            api_key_env="OPENROUTER_API_KEY",
        )
        client_sentinel = object()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(Agent=MagicMock(), Runner=fake_runner)
        fake_openai = MagicMock(AsyncOpenAI=MagicMock(return_value=client_sentinel))
        with (
            patch.dict(sys.modules, {"agents": fake_sdk, "openai": fake_openai}),
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-proxy"}, clear=True),
        ):
            rc = run(manifest)
        assert rc == EXIT_OK
        # No base_url: default Responses API and default tracing stay intact.
        fake_sdk.set_default_openai_client.assert_called_once_with(client_sentinel)
        fake_sdk.set_default_openai_api.assert_not_called()

    def test_run_leaves_default_client_alone_without_overrides(self) -> None:
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(Agent=MagicMock(), Runner=fake_runner)
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            run(self._manifest())
        fake_sdk.set_default_openai_client.assert_not_called()

    def test_run_builds_explicit_model_for_slash_containing_id_with_base_url(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """D1 openrouter fix: OpenRouter model ids like 'deepseek/deepseek-chat'
        contain a vendor/model slash. Passed as a bare string, the SDK's
        ``MultiProvider`` (the default ``RunConfig.model_provider``) parses
        everything before the first '/' as an SDK provider prefix and raises
        ``UserError: Unknown prefix: deepseek`` before any tool call
        (agents/models/multi_provider.py:154-173,221 in the installed SDK).
        The fix must build an explicit ``OpenAIChatCompletionsModel`` bound
        to the manifest's endpoint client and hand THAT to ``Agent(model=...)``
        instead - never the bare string - since the SDK never re-resolves
        (and never prefix-parses) a model that is already a ``Model``
        instance (agents/run_internal/turn_preparation.py:126-135)."""
        manifest = RunnerManifest(
            session_id="abc",
            prompt="hello",
            workdir="/workspace",
            model="deepseek/deepseek-chat",
            base_url="https://openrouter.ai/api/v1",
            api_key_env="OPENROUTER_API_KEY",
        )
        client_sentinel = object()
        explicit_model_sentinel = object()
        fake_agent_cls = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_chat_model_cls = MagicMock(return_value=explicit_model_sentinel)
        fake_sdk = MagicMock(
            Agent=fake_agent_cls,
            Runner=fake_runner,
            OpenAIChatCompletionsModel=fake_chat_model_cls,
        )
        fake_openai = MagicMock(AsyncOpenAI=MagicMock(return_value=client_sentinel))
        with (
            patch.dict(sys.modules, {"agents": fake_sdk, "openai": fake_openai}),
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-proxy"}, clear=True),
            caplog.at_level("INFO"),
        ):
            rc = run(manifest)
        assert rc == EXIT_OK
        # The SDK's own prefix-parsing path (a bare model string handed to
        # MultiProvider) must never be exercised: the Agent is built with
        # the pre-resolved Model instance, not "deepseek/deepseek-chat".
        fake_chat_model_cls.assert_called_once_with(
            model="deepseek/deepseek-chat",
            openai_client=client_sentinel,
        )
        assert fake_agent_cls.call_args.kwargs["model"] is explicit_model_sentinel
        assert any(
            "deepseek/deepseek-chat" in rec.message and "https://openrouter.ai/api/v1" in rec.message
            for rec in caplog.records
        )

    def test_run_uses_bare_model_string_without_base_url(self) -> None:
        """No ``base_url`` override: existing behavior (bare model string,
        resolved by whatever ``model_provider`` the SDK defaults to) is
        unchanged - the runner must not construct an explicit
        ``OpenAIChatCompletionsModel`` when there is no custom endpoint."""
        fake_agent_cls = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(Agent=fake_agent_cls, Runner=fake_runner)
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(self._manifest())
        assert rc == EXIT_OK
        assert fake_agent_cls.call_args.kwargs["model"] == "gpt-5-mini"
        fake_sdk.OpenAIChatCompletionsModel.assert_not_called()

    def test_run_builds_explicit_model_even_without_slash_when_base_url_set(self) -> None:
        """A custom endpoint always gets an explicit Model, slash or not -
        the manifest's model id is still whatever the endpoint expects, and
        relying on the SDK's default-client wiring alone previously left
        model resolution to MultiProvider, which is only safe for ids the
        SDK's built-in prefixes happen to tolerate."""
        manifest = RunnerManifest(
            session_id="abc",
            prompt="hello",
            workdir="/workspace",
            model="gpt-5-mini",
            base_url="http://localhost:8000/v1",
            api_key_env="OPENROUTER_API_KEY",
        )
        client_sentinel = object()
        fake_agent_cls = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(Agent=fake_agent_cls, Runner=fake_runner)
        fake_openai = MagicMock(AsyncOpenAI=MagicMock(return_value=client_sentinel))
        with (
            patch.dict(sys.modules, {"agents": fake_sdk, "openai": fake_openai}),
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-proxy"}, clear=True),
        ):
            rc = run(manifest)
        assert rc == EXIT_OK
        fake_sdk.OpenAIChatCompletionsModel.assert_called_once_with(
            model="gpt-5-mini",
            openai_client=client_sentinel,
        )
        assert fake_agent_cls.call_args.kwargs["model"] is fake_sdk.OpenAIChatCompletionsModel.return_value

    def test_run_fails_loudly_when_api_key_env_var_missing(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        manifest = RunnerManifest(
            session_id="abc",
            prompt="hello",
            workdir="/workspace",
            model="gpt-5-mini",
            api_key_env="MISSING_PROXY_KEY",
        )
        with patch.dict("os.environ", {}, clear=True):
            rc = run(manifest)
        assert rc == EXIT_MANIFEST_ERROR
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        error = next(e for e in events if e["type"] == "error")
        assert error["kind"] == "config_invalid"
        assert "MISSING_PROXY_KEY" in error["message"]

    def test_run_fails_loudly_for_non_credential_api_key_env(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        manifest = RunnerManifest(
            session_id="abc",
            prompt="hello",
            workdir="/workspace",
            model="gpt-5-mini",
            base_url="http://localhost:8000/v1",
            api_key_env="LD_PRELOAD",
        )
        with patch.dict("os.environ", {"LD_PRELOAD": "libx.so"}, clear=True):
            rc = run(manifest)
        assert rc == EXIT_MANIFEST_ERROR
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        error = next(e for e in events if e["type"] == "error")
        assert error["kind"] == "config_invalid"
        assert "LD_PRELOAD" in error["message"]
        # The rejected variable's value is never echoed.
        assert "libx.so" not in json.dumps(events)

    def test_run_starts_and_stops_heartbeat(self) -> None:
        stop_event = threading.Event()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(Agent=MagicMock(), Runner=fake_runner)
        with (
            patch.object(
                runner_module,
                "_start_heartbeat",
                return_value=stop_event,
            ) as start_hb,
            patch.dict(sys.modules, {"agents": fake_sdk}),
        ):
            run(self._manifest())
        start_hb.assert_called_once()
        assert stop_event.is_set()

    def test_run_stops_heartbeat_on_sdk_missing(self) -> None:
        stop_event = threading.Event()
        with (
            patch.object(
                runner_module,
                "_start_heartbeat",
                return_value=stop_event,
            ),
            patch.dict(sys.modules, {"agents": None}),
        ):
            rc = run(self._manifest())
        assert rc == EXIT_SDK_MISSING
        assert stop_event.is_set()


# ---------------------------------------------------------------------------
# Runner heartbeat
# ---------------------------------------------------------------------------


def _await_heartbeat_payload(hb_file: Path, timeout_s: float = 5.0) -> dict[str, Any]:
    """Return the first complete heartbeat payload written to *hb_file*.

    The writer uses ``Path.write_text``, which truncates before writing, so
    the file can exist while still being empty or only partially written.
    Waiting for mere existence races that window and reads torn content, so
    poll until the file parses instead.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            text = hb_file.read_text(encoding="utf-8").strip()
        except OSError:  # not created yet
            text = ""
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass  # partial write; retry
        time.sleep(0.01)
    raise AssertionError(f"no complete heartbeat payload at {hb_file} within {timeout_s}s")


class TestRunnerHeartbeat:
    def test_heartbeat_writes_proxy_shaped_payload(self, tmp_path: Path) -> None:
        hb_dir = tmp_path / ".sdd" / "runtime" / "heartbeats"
        stop_event = _start_heartbeat("hb-sess", hb_dir, interval_s=0.05)
        try:
            payload = _await_heartbeat_payload(hb_dir / "hb-sess.json")
        finally:
            stop_event.set()
        # Schema mirrors _start_heartbeat_proxy in spawner_sandbox_session.
        assert set(payload) == {
            "timestamp",
            "phase",
            "progress_pct",
            "current_file",
            "message",
            "status",
            "files_changed",
        }
        assert payload["status"] == "working"
        assert payload["phase"] == "implementing"
        assert isinstance(payload["timestamp"], int)

    def test_heartbeat_stops_after_event_set(self, tmp_path: Path) -> None:
        hb_dir = tmp_path / ".sdd" / "runtime" / "heartbeats"
        stop_event = _start_heartbeat("hb-stop", hb_dir, interval_s=0.05)
        hb_file = hb_dir / "hb-stop.json"
        _await_heartbeat_payload(hb_file)
        stop_event.set()
        time.sleep(0.15)
        hb_file.unlink()
        time.sleep(0.15)
        assert not hb_file.exists()

    def test_resolve_heartbeat_dir_prefers_manifest_field(self) -> None:
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/project/.sdd/worktrees/s",
            model="gpt-5",
            heartbeat_dir="/project/.sdd/runtime/heartbeats",
        )
        assert _resolve_heartbeat_dir(manifest) == Path("/project/.sdd/runtime/heartbeats")

    def test_resolve_heartbeat_dir_falls_back_to_workdir(self) -> None:
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/workspace",
            model="gpt-5",
        )
        assert _resolve_heartbeat_dir(manifest) == Path("/workspace/.sdd/runtime/heartbeats")

    def test_run_writes_heartbeat_where_monitor_reads(self, tmp_path: Path) -> None:
        """Worktree isolation: heartbeats must land at the orchestrator root.

        spawn_cwd is a per-session worktree, so the manifest carries the
        orchestrator-root heartbeat directory. The file the runner writes
        must be exactly the path the HeartbeatMonitor polls:
        ``<orchestrator_workdir>/.sdd/runtime/heartbeats/<session_id>.json``.
        """
        orchestrator_root = tmp_path / "project"
        worktree = orchestrator_root / ".sdd" / "worktrees" / "hb-mon"
        worktree.mkdir(parents=True)
        manifest = RunnerManifest(
            session_id="hb-mon",
            prompt="hello",
            workdir=str(worktree),
            model="gpt-5-mini",
            heartbeat_dir=str(orchestrator_root / ".sdd" / "runtime" / "heartbeats"),
        )
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(Agent=MagicMock(), Runner=fake_runner)
        with patch.object(runner_module, "_start_heartbeat") as start_hb:
            start_hb.return_value = threading.Event()
            with patch.dict(sys.modules, {"agents": fake_sdk}):
                rc = run(manifest)
        assert rc == EXIT_OK
        # Same expression as HeartbeatMonitor: workdir / ".sdd" / "runtime" / "heartbeats"
        monitor_dir = orchestrator_root / ".sdd" / "runtime" / "heartbeats"
        start_hb.assert_called_once_with("hb-mon", monitor_dir)


# ---------------------------------------------------------------------------
# main() - manifest validation
# ---------------------------------------------------------------------------


class TestRunnerMain:
    def test_main_returns_manifest_error_when_missing(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(["--manifest", str(tmp_path / "does-not-exist.json")])
        assert rc == EXIT_MANIFEST_ERROR
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        assert any(e["type"] == "error" and e["kind"] == "manifest_missing" for e in events)

    def test_main_returns_manifest_error_when_invalid_json(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / "bad.json"
        path.write_text("not-json", encoding="utf-8")
        rc = main(["--manifest", str(path)])
        assert rc == EXIT_MANIFEST_ERROR
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        assert any(e["type"] == "error" and e["kind"] == "manifest_invalid" for e in events)


# ---------------------------------------------------------------------------
# Pricing - gpt-5 family is priced
# ---------------------------------------------------------------------------


class TestPricing:
    def test_gpt_5_family_is_priced(self) -> None:
        from bernstein.core.cost.cost import MODEL_COSTS_PER_1M_TOKENS, _model_cost

        assert "gpt-5" in MODEL_COSTS_PER_1M_TOKENS
        assert "gpt-5-mini" in MODEL_COSTS_PER_1M_TOKENS
        assert "o4" in MODEL_COSTS_PER_1M_TOKENS
        # The substring-based lookup must land on the gpt-5 row instead of
        # falling through to the generic 0.005 default.
        assert _model_cost("gpt-5-mini") < _model_cost("gpt-5")


# ---------------------------------------------------------------------------
# Adapter module exposes expected surface
# ---------------------------------------------------------------------------


class TestModuleSurface:
    def test_module_exports_adapter_class(self) -> None:
        assert hasattr(adapter_module, "OpenAIAgentsAdapter")


# ---------------------------------------------------------------------------
# Manifest-carried control knobs (allow_run_command / max_turns)
# ---------------------------------------------------------------------------


class TestManifestControlKnobs:
    """Control env vars are filtered from the runner env; they must ride the manifest."""

    def _spawn_and_read_manifest(
        self,
        tmp_path: Path,
        *,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        adapter = OpenAIAgentsAdapter()
        proc_mock = _make_popen_mock(pid=1200)
        with patch(
            "bernstein.adapters.openai_agents.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="do work",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id=session_id,
                mcp_config=mcp_config,
            )
        return json.loads((tmp_path / ".sdd" / "runtime" / f"{session_id}.manifest.json").read_text())

    def test_allow_run_command_from_parent_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND", "1")
        manifest = self._spawn_and_read_manifest(tmp_path, session_id="oai-arc1")
        assert manifest["allow_run_command"] is True

    def test_allow_run_command_false_when_env_unset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND", raising=False)
        manifest = self._spawn_and_read_manifest(tmp_path, session_id="oai-arc2")
        assert manifest["allow_run_command"] is False

    def test_explicit_mcp_config_allow_run_command_wins_over_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND", "1")
        manifest = self._spawn_and_read_manifest(
            tmp_path, session_id="oai-arc3", mcp_config={"allow_run_command": False}
        )
        assert manifest["allow_run_command"] is False

    def test_max_turns_from_mcp_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MAX_TURNS_ENV_VAR, "200")
        manifest = self._spawn_and_read_manifest(tmp_path, session_id="oai-mt1", mcp_config={"max_turns": 40})
        assert manifest["max_turns"] == 40

    def test_max_turns_from_parent_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MAX_TURNS_ENV_VAR, "77")
        manifest = self._spawn_and_read_manifest(tmp_path, session_id="oai-mt2")
        assert manifest["max_turns"] == 77

    def test_rejected_allow_run_command_override_is_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-bool mcp_config allow_run_command must fall back to the env
        default AND leave a WARNING diagnostic -- a silently-dropped override
        was previously undiagnosable from logs alone."""
        monkeypatch.delenv("BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND", raising=False)
        with caplog.at_level("WARNING", logger="bernstein.adapters.openai_agents"):
            manifest = self._spawn_and_read_manifest(
                tmp_path, session_id="oai-arc4", mcp_config={"allow_run_command": "yes"}
            )
        assert manifest["allow_run_command"] is False
        assert "allow_run_command='yes' must be a bool" in caplog.text

    def test_valid_allow_run_command_override_is_not_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.delenv("BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND", raising=False)
        with caplog.at_level("WARNING", logger="bernstein.adapters.openai_agents"):
            self._spawn_and_read_manifest(tmp_path, session_id="oai-arc5", mcp_config={"allow_run_command": True})
        assert "allow_run_command" not in caplog.text

    def test_rejected_max_turns_override_is_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-positive / wrong-type mcp_config max_turns must fall back to
        env/tuning resolution AND leave a WARNING diagnostic, matching the
        runner-side _resolve_max_turns rejection logging."""
        monkeypatch.setenv(MAX_TURNS_ENV_VAR, "77")
        with caplog.at_level("WARNING", logger="bernstein.adapters.openai_agents"):
            manifest = self._spawn_and_read_manifest(tmp_path, session_id="oai-mt3", mcp_config={"max_turns": 0})
        assert manifest["max_turns"] == 77
        assert "max_turns=0 must be a positive int" in caplog.text

    def test_rejected_bool_max_turns_override_is_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """bool is an int subclass; ``True`` must be rejected (and logged),
        not treated as max_turns=1."""
        monkeypatch.setenv(MAX_TURNS_ENV_VAR, "77")
        with caplog.at_level("WARNING", logger="bernstein.adapters.openai_agents"):
            manifest = self._spawn_and_read_manifest(tmp_path, session_id="oai-mt4", mcp_config={"max_turns": True})
        assert manifest["max_turns"] == 77
        assert "max_turns=True must be a positive int" in caplog.text

    def test_runner_manifest_parses_control_knobs(self) -> None:
        manifest = RunnerManifest.from_dict(
            {
                "session_id": "s",
                "prompt": "p",
                "workdir": "/abs",
                "model": "MiniMax-M3",
                "allow_run_command": True,
                "max_turns": 40,
            }
        )
        assert manifest.allow_run_command is True
        assert manifest.max_turns == 40

    def test_runner_manifest_defaults_to_none(self) -> None:
        manifest = RunnerManifest.from_dict({"session_id": "s", "prompt": "p", "workdir": "/abs", "model": "gpt-5"})
        assert manifest.allow_run_command is None
        assert manifest.max_turns is None

    def test_resolve_max_turns_manifest_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MAX_TURNS_ENV_VAR, "200")
        assert _resolve_max_turns(40) == 40

    def test_resolve_max_turns_invalid_manifest_falls_back_to_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MAX_TURNS_ENV_VAR, "200")
        assert _resolve_max_turns(0) == 200

    def test_run_forwards_manifest_max_turns_to_run_sync(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv(MAX_TURNS_ENV_VAR, raising=False)
        fake_agent = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok")
        fake_sdk = MagicMock(Agent=MagicMock(return_value=fake_agent), Runner=fake_runner)
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/abs",
            model="MiniMax-M3",
            max_turns=40,
        )
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(manifest)
        assert rc == EXIT_OK
        _, kwargs = fake_runner.run_sync.call_args
        assert kwargs["max_turns"] == 40


# ---------------------------------------------------------------------------
# Bug 13: cost metering on the openai_agents path.
#
# Root cause: the runner captured SDK usage and emitted a bare "usage" event
# to its own stdout/log, but never priced it and never wrote the ``.tokens``
# sidecar file (``<orchestrator-root>/.sdd/runtime/<session_id>.tokens``)
# that ``TokenGrowthMonitor.read_tokens()`` tails to populate
# ``AgentSession.tokens_used``. The orchestrator's live cost loop
# (``Orchestrator._record_live_costs``) skips any session whose
# ``tokens_used`` is 0, so the session's spend never reached CostTracker at
# all - a 45-minute MiniMax-M3 run recorded ``spent_usd: 0.0`` with an empty
# ``usages`` list. These tests pin: (1) a fake SDK usage payload flows
# through to a correctly priced + aggregated "usage" event and a populated
# tokens sidecar, and (2) an unrecognized model is priced at an explicit $0
# with a WARNING logged and its tokens still counted, not dropped.
# ---------------------------------------------------------------------------


class TestBug13CostMetering:
    def _manifest(
        self, *, model: str, heartbeat_dir: str | None = None, session_id: str = "sess-bug13"
    ) -> RunnerManifest:
        return RunnerManifest(
            session_id=session_id,
            prompt="hello",
            workdir="/workspace",
            model=model,
            heartbeat_dir=heartbeat_dir,
        )

    def test_priced_model_usage_flows_to_usage_event_and_sidecar(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A fake SDK usage payload for a priced model computes a nonzero
        cost, emits it on the "usage" event, and lands in the .tokens
        sidecar the orchestrator's live cost loop actually reads."""
        heartbeat_dir = tmp_path / ".sdd" / "runtime" / "heartbeats"
        # Uses the model from the actual bug-13 incident (MiniMax-M3) rather
        # than a "gpt-5" family name: several gpt-5* keys are substrings of
        # each other in MODEL_COSTS_PER_1M_TOKENS insertion order (a
        # pre-existing, separate issue from bug 13), which would make this
        # test's expected cost depend on dict iteration order.
        manifest = self._manifest(model="MiniMax-M3", heartbeat_dir=str(heartbeat_dir))

        fake_agent = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(
            summary="ok",
            usage=_FakeUsage(input_tokens=10_000, output_tokens=2_000, tool_calls=3),
        )
        fake_sdk = MagicMock(Agent=MagicMock(return_value=fake_agent), Runner=fake_runner)
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(manifest)
        assert rc == EXIT_OK

        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        usage_event = next(e for e in events if e["type"] == "usage")
        assert usage_event["input_tokens"] == 10_000
        assert usage_event["output_tokens"] == 2_000
        assert usage_event["model"] == "MiniMax-M3"
        assert usage_event["priced"] is True
        # minimax-m3: $0.30/$1.20 per 1M -> (10000/1e6)*0.3 + (2000/1e6)*1.2
        expected_cost = (10_000 / 1_000_000.0) * 0.3 + (2_000 / 1_000_000.0) * 1.2
        assert usage_event["cost_usd"] == pytest.approx(expected_cost)
        assert usage_event["cost_usd"] > 0.0
        assert usage_event["running_total_usd"] == pytest.approx(expected_cost)

        # The bug-13 fix: the tokens sidecar must exist and carry the same
        # counts, at the orchestrator-root path (heartbeat_dir's parent),
        # NOT under the per-session worktree.
        sidecar = heartbeat_dir.parent / f"{manifest.session_id}.tokens"
        assert sidecar.exists()
        record = json.loads(sidecar.read_text().strip().splitlines()[-1])
        assert record["in"] == 10_000
        assert record["out"] == 2_000
        assert "ts" in record

    def test_unpriced_model_logs_warning_and_meters_zero_without_dropping_tokens(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A model with no pricing-table entry must not vanish from cost
        totals: cost is an explicit $0, a WARNING is logged, and the token
        counts still flow through to the usage event and the sidecar."""
        heartbeat_dir = tmp_path / ".sdd" / "runtime" / "heartbeats"
        manifest = self._manifest(model="totally-unknown-model-xyz", heartbeat_dir=str(heartbeat_dir))

        fake_agent = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(
            summary="ok",
            usage=_FakeUsage(input_tokens=5_000, output_tokens=1_000, tool_calls=0),
        )
        fake_sdk = MagicMock(Agent=MagicMock(return_value=fake_agent), Runner=fake_runner)
        with caplog.at_level("WARNING"):
            with patch.dict(sys.modules, {"agents": fake_sdk}):
                rc = run(manifest)
        assert rc == EXIT_OK

        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        usage_event = next(e for e in events if e["type"] == "usage")
        # Tokens are still visible even though the model is unpriced.
        assert usage_event["input_tokens"] == 5_000
        assert usage_event["output_tokens"] == 1_000
        assert usage_event["priced"] is False
        assert usage_event["cost_usd"] == 0.0
        assert usage_event["running_total_usd"] == 0.0

        assert any(
            "no pricing-table entry" in rec.message and "totally-unknown-model-xyz" in rec.message
            for rec in caplog.records
            if rec.levelname == "WARNING"
        )

        sidecar = heartbeat_dir.parent / f"{manifest.session_id}.tokens"
        assert sidecar.exists()
        record = json.loads(sidecar.read_text().strip().splitlines()[-1])
        assert record["in"] == 5_000
        assert record["out"] == 1_000

    def test_none_usage_falls_back_to_raw_responses(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Bug-13 follow-up (2026-07-02): a real MiniMax-M2.7-highspeed run
        proved ``result.usage`` is None on some providers (in fact, the
        installed SDK's ``RunResult`` never has a ``usage`` attribute at
        all). Usage must be recovered by summing ``result.raw_responses``
        (each a fake ``agents.items.ModelResponse`` carrying its own
        ``usage``), priced correctly, and written to the sidecar exactly as
        the primary path does."""
        heartbeat_dir = tmp_path / ".sdd" / "runtime" / "heartbeats"
        manifest = self._manifest(model="MiniMax-M3", heartbeat_dir=str(heartbeat_dir))

        fake_agent = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(
            summary="ok",
            usage=None,
            raw_responses=[
                _FakeRawResponse(input_tokens=3_000, output_tokens=500),
                _FakeRawResponse(input_tokens=2_000, output_tokens=300),
            ],
        )
        fake_sdk = MagicMock(Agent=MagicMock(return_value=fake_agent), Runner=fake_runner)
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(manifest)
        assert rc == EXIT_OK

        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        usage_event = next(e for e in events if e["type"] == "usage")
        assert usage_event["input_tokens"] == 5_000
        assert usage_event["output_tokens"] == 800
        assert usage_event["priced"] is True
        assert "usage_missing" not in usage_event
        expected_cost = (5_000 / 1_000_000.0) * 0.3 + (800 / 1_000_000.0) * 1.2
        assert usage_event["cost_usd"] == pytest.approx(expected_cost)

        sidecar = heartbeat_dir.parent / f"{manifest.session_id}.tokens"
        assert sidecar.exists()
        record = json.loads(sidecar.read_text().strip().splitlines()[-1])
        assert record["in"] == 5_000
        assert record["out"] == 800

    def test_none_usage_and_empty_raw_responses_emits_usage_missing_no_sidecar(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Both ``result.usage`` and the ``raw_responses`` fallback yield
        nothing: a WARNING naming the model must fire, a usage event with
        ``usage_missing: true`` must be emitted, and NOTHING may be written
        to the cost sidecar (no fabricated tokens/cost)."""
        heartbeat_dir = tmp_path / ".sdd" / "runtime" / "heartbeats"
        manifest = self._manifest(model="MiniMax-M2.7-highspeed", heartbeat_dir=str(heartbeat_dir))

        fake_agent = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(
            summary="ok",
            usage=None,
            raw_responses=[_FakeRawResponse(input_tokens=0, output_tokens=0)],
        )
        fake_sdk = MagicMock(Agent=MagicMock(return_value=fake_agent), Runner=fake_runner)
        with caplog.at_level(logging.WARNING):
            with patch.dict(sys.modules, {"agents": fake_sdk}):
                rc = run(manifest)
        assert rc == EXIT_OK

        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        usage_event = next(e for e in events if e["type"] == "usage")
        assert usage_event["input_tokens"] == 0
        assert usage_event["output_tokens"] == 0
        assert usage_event["usage_missing"] is True
        assert "cost_usd" not in usage_event

        assert any(
            "no usage data" in rec.message and "MiniMax-M2.7-highspeed" in rec.message
            for rec in caplog.records
            if rec.levelname == "WARNING"
        )

        sidecar = heartbeat_dir.parent / f"{manifest.session_id}.tokens"
        assert not sidecar.exists()

    def test_resolve_tokens_sidecar_path_uses_heartbeat_dir_parent(self, tmp_path: Path) -> None:
        heartbeat_dir = tmp_path / "orchestrator-root" / ".sdd" / "runtime" / "heartbeats"
        manifest = self._manifest(model="gpt-5", heartbeat_dir=str(heartbeat_dir), session_id="s1")
        path = _resolve_tokens_sidecar_path(manifest)
        assert path == heartbeat_dir.parent / "s1.tokens"

    def test_resolve_tokens_sidecar_path_falls_back_to_workdir(self, tmp_path: Path) -> None:
        manifest = RunnerManifest(
            session_id="s2",
            prompt="p",
            workdir=str(tmp_path / "worktree"),
            model="gpt-5",
        )
        path = _resolve_tokens_sidecar_path(manifest)
        assert path == tmp_path / "worktree" / ".sdd" / "runtime" / "s2.tokens"

    def test_append_tokens_sidecar_skips_zero_usage(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "s3.tokens"
        _append_tokens_sidecar(sidecar, 0, 0)
        assert not sidecar.exists()

    def test_append_tokens_sidecar_survives_unwritable_path(self, caplog: pytest.LogCaptureFixture) -> None:
        # A path under a location that cannot be created (root-owned or
        # already a file) must log a warning, never raise - a sidecar
        # failure must not fail the run.
        unwritable = Path("/this/path/should/not/be/creatable/by/tests") / "s4.tokens"
        with caplog.at_level("WARNING"):
            _append_tokens_sidecar(unwritable, 10, 10)
        assert any("failed to write tokens sidecar" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# D2 MiniMax attempt-3 regression (2026-07-03): usage extraction must be
# reachable on exception paths. Six backend agents each burned 10 real
# MiniMax turns, died on MaxTurnsExceeded, and emitted ZERO usage events -
# the run's cost file read `spent_usd: 0.0, usages: []` despite real spend,
# because the extraction block lived only after the try/except around
# Runner.run_sync. These tests pin: (1) an exception carrying partial run
# data (exc.run_data.raw_responses) still prices and sidecars the real
# usage; (2) an exception with no run data still emits the usage_missing
# marker (downstream sees "unknown", never a silent zero); (3) the
# rate-limit exception branch behaves the same way.
# ---------------------------------------------------------------------------


class _FakeRunData:
    """Mimics ``agents.exceptions.RunErrorDetails`` as carried on
    ``AgentsException.run_data`` - same ``raw_responses`` shape as a
    successful ``RunResult``."""

    def __init__(self, raw_responses: list[Any]) -> None:
        self.raw_responses = raw_responses


class _FakeMaxTurnsExceeded(Exception):
    """Stands in for ``agents.exceptions.MaxTurnsExceeded``: an SDK
    exception that carries the partial run's state on ``run_data``."""

    def __init__(self, message: str, run_data: Any = None) -> None:
        super().__init__(message)
        self.run_data = run_data


class TestUsageOnExceptionPaths:
    def _manifest(self, *, heartbeat_dir: str, session_id: str = "sess-exc") -> RunnerManifest:
        return RunnerManifest(
            session_id=session_id,
            prompt="hello",
            workdir="/workspace",
            model="MiniMax-M3",
            heartbeat_dir=heartbeat_dir,
        )

    def test_max_turns_exceeded_with_run_data_prices_and_sidecars_partial_usage(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The exact D2 MiniMax attempt-3 shape: run_sync raises
        MaxTurnsExceeded after real billable turns. The partial usage on
        exc.run_data.raw_responses must be priced, emitted as a usage
        event, and written to the .tokens sidecar - AND the error event +
        EXIT_GENERIC contract must be preserved."""
        heartbeat_dir = tmp_path / ".sdd" / "runtime" / "heartbeats"
        manifest = self._manifest(heartbeat_dir=str(heartbeat_dir))

        exc = _FakeMaxTurnsExceeded(
            "Max turns (10) exceeded",
            run_data=_FakeRunData(
                raw_responses=[
                    _FakeRawResponse(input_tokens=5_000, output_tokens=700),
                    _FakeRawResponse(input_tokens=6_000, output_tokens=900),
                ],
            ),
        )
        fake_runner = MagicMock()
        fake_runner.run_sync.side_effect = exc
        fake_sdk = MagicMock(Agent=MagicMock(), Runner=fake_runner)
        with caplog.at_level(logging.INFO):
            with patch.dict(sys.modules, {"agents": fake_sdk}):
                rc = run(manifest)
        assert rc == EXIT_GENERIC

        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        # The error contract is unchanged...
        error_event = next(e for e in events if e["type"] == "error")
        assert error_event["kind"] == "runtime"
        assert "Max turns" in error_event["message"]
        # ...but a real usage event now precedes it.
        usage_event = next(e for e in events if e["type"] == "usage")
        assert usage_event["input_tokens"] == 11_000
        assert usage_event["output_tokens"] == 1_600
        assert usage_event["priced"] is True
        expected_cost = (11_000 / 1_000_000.0) * 0.3 + (1_600 / 1_000_000.0) * 1.2
        assert usage_event["cost_usd"] == pytest.approx(expected_cost)
        assert usage_event["cost_usd"] > 0.0
        assert usage_event["usage_source"] == "_FakeMaxTurnsExceeded.run_data"
        # Usage event must be emitted before the error event (extraction
        # happens before the early return).
        assert events.index(usage_event) < events.index(error_event)
        # No completion event on the exception path.
        assert not any(e["type"] == "completion" for e in events)

        # The sidecar carries the recovered partial usage.
        sidecar = heartbeat_dir.parent / f"{manifest.session_id}.tokens"
        assert sidecar.exists()
        record = json.loads(sidecar.read_text().strip().splitlines()[-1])
        assert record["in"] == 11_000
        assert record["out"] == 1_600

        # The INFO line names what was recovered and from where.
        assert any(
            "llm_call" in rec.message and "_FakeMaxTurnsExceeded.run_data" in rec.message
            for rec in caplog.records
            if rec.levelname == "INFO"
        )

    def test_exception_without_run_data_emits_usage_missing_marker(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A plain exception with no run_data: no fabricated tokens, no
        sidecar, but a usage_missing event fires so downstream can tell
        "unknown" from "zero"."""
        heartbeat_dir = tmp_path / ".sdd" / "runtime" / "heartbeats"
        manifest = self._manifest(heartbeat_dir=str(heartbeat_dir))

        fake_runner = MagicMock()
        fake_runner.run_sync.side_effect = RuntimeError("something else")
        fake_sdk = MagicMock(Agent=MagicMock(), Runner=fake_runner)
        with caplog.at_level(logging.WARNING):
            with patch.dict(sys.modules, {"agents": fake_sdk}):
                rc = run(manifest)
        assert rc == EXIT_GENERIC

        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        usage_event = next(e for e in events if e["type"] == "usage")
        assert usage_event["input_tokens"] == 0
        assert usage_event["output_tokens"] == 0
        assert usage_event["usage_missing"] is True
        assert usage_event["usage_source"] == "RuntimeError.run_data"
        assert "cost_usd" not in usage_event
        assert any(e["type"] == "error" and e["kind"] == "runtime" for e in events)

        sidecar = heartbeat_dir.parent / f"{manifest.session_id}.tokens"
        assert not sidecar.exists()

        assert any("no usage data" in rec.message for rec in caplog.records if rec.levelname == "WARNING")

    def test_rate_limit_exception_with_run_data_recovers_usage_and_keeps_exit_code(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A rate-limit abort mid-run still recovers the spend already
        incurred and still returns EXIT_RATE_LIMIT."""
        heartbeat_dir = tmp_path / ".sdd" / "runtime" / "heartbeats"
        manifest = self._manifest(heartbeat_dir=str(heartbeat_dir))

        exc = _FakeMaxTurnsExceeded(
            "HTTP 429 rate limit",
            run_data=_FakeRunData(raw_responses=[_FakeRawResponse(input_tokens=2_000, output_tokens=300)]),
        )
        fake_runner = MagicMock()
        fake_runner.run_sync.side_effect = exc
        fake_sdk = MagicMock(Agent=MagicMock(), Runner=fake_runner)
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(manifest)
        assert rc == EXIT_RATE_LIMIT

        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        assert any(e["type"] == "error" and e["kind"] == "rate_limit" for e in events)
        usage_event = next(e for e in events if e["type"] == "usage")
        assert usage_event["input_tokens"] == 2_000
        assert usage_event["output_tokens"] == 300
        assert usage_event["cost_usd"] > 0.0

        sidecar = heartbeat_dir.parent / f"{manifest.session_id}.tokens"
        assert sidecar.exists()
