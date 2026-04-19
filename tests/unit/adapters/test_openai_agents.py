"""Unit tests for the OpenAI Agents SDK v2 adapter."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ApiTier, ModelConfig, ProviderType

from bernstein.adapters import openai_agents as adapter_module
from bernstein.adapters.openai_agents import OpenAIAgentsAdapter
from bernstein.adapters.openai_agents_runner import (
    EXIT_GENERIC,
    EXIT_MANIFEST_ERROR,
    EXIT_OK,
    EXIT_RATE_LIMIT,
    EXIT_SDK_MISSING,
    RunnerManifest,
    _build_agent_kwargs,
    _build_run_config,
    _is_rate_limit,
    emit_event,
    load_manifest,
    main,
    run,
)
from bernstein.adapters.plugin_sdk import AdapterCapability
from bernstein.adapters.registry import get_adapter

if TYPE_CHECKING:
    from pathlib import Path


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
# health_check — tolerates missing SDK
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
# spawn() — command construction
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
# spawn() — env isolation
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


# ---------------------------------------------------------------------------
# spawn() — missing binary / PermissionError
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
# spawn() — warnings
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

    def test_build_run_config_copies_mcp_servers(self) -> None:
        manifest = RunnerManifest(
            session_id="s",
            prompt="p",
            workdir="/abs",
            model="gpt-5",
            sandbox_provider="e2b",
            mcp_servers={"bernstein": {"command": "python"}},
        )
        cfg = _build_run_config(manifest)
        assert cfg["sandbox_provider"] == "e2b"
        assert cfg["workdir"] == "/abs"
        assert cfg["mcp_servers"] == {"bernstein": {"command": "python"}}
        # Defensive copy — mutating the output must not mutate manifest state.
        cfg["mcp_servers"]["other"] = {"command": "x"}
        assert "other" not in manifest.mcp_servers

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
# Runner.run — SDK lifecycle (mocked)
# ---------------------------------------------------------------------------


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int, tool_calls: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.tool_calls = tool_calls


class _FakeResult:
    def __init__(self, summary: str = "done", usage: _FakeUsage | None = None) -> None:
        self.final_output = summary
        self.usage = usage


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

    def test_run_without_usage_still_emits_completion(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_runner = MagicMock()
        fake_runner.run_sync.return_value = _FakeResult(summary="ok", usage=None)
        fake_sdk = MagicMock(Agent=MagicMock(), Runner=fake_runner)
        with patch.dict(sys.modules, {"agents": fake_sdk}):
            rc = run(self._manifest())
        assert rc == EXIT_OK
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        assert any(e["type"] == "completion" for e in events)
        assert not any(e["type"] == "usage" for e in events)

    def test_run_emits_sdk_missing_when_import_fails(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Simulate ImportError for the agents package by registering a
        # placeholder whose attribute access raises ImportError — we must
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


# ---------------------------------------------------------------------------
# main() — manifest validation
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
# Pricing — gpt-5 family is priced
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
