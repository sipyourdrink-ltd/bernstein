"""Unit tests for QwenAdapter spawn/kill/is_alive."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.llm import LLMSettings
from bernstein.core.models import ApiTier, ModelConfig, ProviderType

from bernstein.adapters.plugin_sdk import AdapterCapability
from bernstein.adapters.qwen import QwenAdapter

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


def _default_settings(**overrides: str | None) -> LLMSettings:
    """Build an LLMSettings with all keys cleared except overrides."""
    defaults: dict[str, str | None] = {
        "openrouter_api_key_paid": None,
        "openrouter_api_key_free": None,
        "togetherai_user_key": None,
        "oxen_api_key": None,
        "g4f_api_key": None,
        "openai_api_key": None,
        "openai_base_url": None,
        "tavily_api_key": None,
    }
    defaults.update(overrides)
    return LLMSettings(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# QwenAdapter.spawn() - command construction
# ---------------------------------------------------------------------------


class TestQwenAdapterSpawn:
    """QwenAdapter.spawn() builds correct command."""

    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=100)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-s1",
            )
        cmd = popen.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.orchestration.worker"]
        inner = _inner_cmd(cmd)
        assert inner[0] == "qwen"

    def test_model_flag_passthrough(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=101)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-s2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--model" in inner
        assert inner[inner.index("--model") + 1] == "qwen-max"

    def test_approval_mode_flag_present(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=102)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-s3",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--approval-mode" in inner
        assert inner[inner.index("--approval-mode") + 1] == "yolo"
        assert "-y" not in inner
        assert "--yolo" not in inner

    def test_prompt_appended_last(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=103)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="my-unique-prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-s4",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[-1] == "my-unique-prompt"

    def test_openrouter_provider_auth_type(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=104)
        settings = _default_settings(openrouter_api_key_paid="or-key-123")
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="some-model", effort="high"),
                session_id="qwen-s5",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--auth-type" in inner
        assert inner[inner.index("--auth-type") + 1] == "openai"

    def test_default_provider_no_auth_type(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=105)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-s6",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--auth-type" not in inner

    def test_opus_maps_to_qwen_max_on_default(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=106)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="opus", effort="high"),
                session_id="qwen-s7",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[inner.index("--model") + 1] == "qwen3.6-plus"

    def test_sonnet_maps_to_coder_model_on_default(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=107)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="qwen-s8",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[inner.index("--model") + 1] == "qwen3.6-plus"

    def test_tavily_flags_when_key_set(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=108)
        settings = _default_settings(tavily_api_key="tvly-test")
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-s9",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        # The non-sensitive selector flag is still on argv.
        assert "--web-search-default" in inner
        assert inner[inner.index("--web-search-default") + 1] == "tavily"
        # SECURITY: the Tavily key must NEVER reach argv (visible via ps).
        assert "--tavily-api-key" not in inner
        assert "tvly-test" not in inner
        # It is forwarded via the environment instead.
        env_arg = popen.call_args.kwargs["env"]
        assert env_arg["TAVILY_API_KEY"] == "tvly-test"

    def test_tavily_argv_omitted_when_key_unset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Strip any inherited TAVILY_API_KEY from the parent environment so
        # the test asserts on the adapter's own injection, not host state.
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=118)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-s9b",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--web-search-default" not in inner
        env_arg = popen.call_args.kwargs["env"]
        assert "TAVILY_API_KEY" not in env_arg

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=109)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock),
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-s10",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_spawn_result_pid(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=110)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock),
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-s11",
            )
        assert result.pid == 110

    def test_log_path_uses_session_id(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=111)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock),
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="my-qwen-session",
            )
        assert result.log_path.name == "my-qwen-session.log"


# ---------------------------------------------------------------------------
# PR3: sampling params (temperature/top_p) wired via mcp_config
# ---------------------------------------------------------------------------


class TestQwenAdapterPluginInfo:
    def test_declares_temperature_and_top_p_only(self) -> None:
        info = QwenAdapter().plugin_info()
        assert set(info.capabilities) == {
            AdapterCapability.SUPPORTS_TEMPERATURE,
            AdapterCapability.SUPPORTS_TOP_P,
        }

    def test_does_not_declare_coarse_or_top_k_or_max_tokens(self) -> None:
        info = QwenAdapter().plugin_info()
        assert AdapterCapability.SUPPORTS_SAMPLING_PARAMS not in info.capabilities
        assert AdapterCapability.SUPPORTS_TOP_K not in info.capabilities
        assert AdapterCapability.SUPPORTS_MAX_TOKENS not in info.capabilities


class TestQwenAdapterSamplingParams:
    """mcp_config temperature/top_p must reach the built CLI argv."""

    def test_temperature_reaches_argv(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=300)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-sampling1",
                mcp_config={"temperature": 0.3},
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--temperature" in inner
        assert inner[inner.index("--temperature") + 1] == "0.3"

    def test_top_p_reaches_argv(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=301)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-sampling2",
                mcp_config={"top_p": 0.85},
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--top-p" in inner
        assert inner[inner.index("--top-p") + 1] == "0.85"

    def test_both_reach_argv_together(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=302)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-sampling3",
                mcp_config={"temperature": 0.1, "top_p": 0.95},
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[inner.index("--temperature") + 1] == "0.1"
        assert inner[inner.index("--top-p") + 1] == "0.95"

    def test_no_mcp_config_omits_sampling_flags(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=303)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-sampling4",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--temperature" not in inner
        assert "--top-p" not in inner

    def test_top_k_and_max_tokens_never_reach_argv(self, tmp_path: Path) -> None:
        """Not declared in plugin_info -> not wired, even if present in
        mcp_config (the spawn-time gate is expected to have refused this
        spawn upstream; this test proves the adapter itself never fakes
        support for a flag the real CLI does not have)."""
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=304)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-sampling5",
                mcp_config={"top_k": 40, "max_tokens": 4096},
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--top-k" not in inner
        assert "--max-tokens" not in inner


# ---------------------------------------------------------------------------
# spawn() - env isolation
# ---------------------------------------------------------------------------


class TestQwenEnvIsolation:
    """spawn() passes only OPENAI-compatible keys to subprocess."""

    def test_env_sets_api_key_from_provider(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=200)
        settings = _default_settings(openrouter_api_key_paid="or-key-abc")
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-env1",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env.get("OPENAI_API_KEY") == "or-key-abc"
        assert env.get("OPENAI_BASE_URL") == "https://openrouter.ai/api/v1"

    def test_env_excludes_unrelated_keys(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=201)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
            patch.dict(
                "os.environ",
                {"ANTHROPIC_API_KEY": "ant-secret", "DATABASE_URL": "pg://x", "PATH": "/usr/bin"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-env2",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "ANTHROPIC_API_KEY" not in env
        assert "DATABASE_URL" not in env

    def test_env_includes_path(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=202)
        settings = _default_settings()
        with (
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="qwen-env3",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "PATH" in env


# ---------------------------------------------------------------------------
# QwenAdapter.name()
# ---------------------------------------------------------------------------


class TestQwenAdapterName:
    def test_name(self) -> None:
        assert QwenAdapter().name() == "Qwen CLI"


# ---------------------------------------------------------------------------
# Missing binary / PermissionError
# ---------------------------------------------------------------------------


class TestQwenSpawnMissingBinary:
    def test_file_not_found_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        settings = _default_settings()
        with (
            patch(
                "bernstein.adapters.qwen.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
            pytest.raises(RuntimeError, match="not found in PATH"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="missing",
            )

    def test_permission_error_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        settings = _default_settings()
        with (
            patch(
                "bernstein.adapters.qwen.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen-max", effort="high"),
                session_id="perm-denied",
            )


# ---------------------------------------------------------------------------
# is_alive() and kill() - inherited from CLIAdapter base
# ---------------------------------------------------------------------------


class TestQwenIsAlive:
    def test_true_when_process_exists(self) -> None:
        adapter = QwenAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=True) as mock_alive:
            assert adapter.is_alive(1234) is True
        mock_alive.assert_called_once_with(1234)

    def test_false_when_oserror(self) -> None:
        adapter = QwenAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=False):
            assert adapter.is_alive(9999) is False


class TestQwenKill:
    def test_calls_killpg(self) -> None:
        adapter = QwenAdapter()
        with patch("bernstein.adapters.base.kill_process_group_graceful") as mock_killpg:
            adapter.kill(555)
        mock_killpg.assert_called_once_with(555)

    def test_does_not_raise_on_oserror(self) -> None:
        adapter = QwenAdapter()
        with patch("bernstein.adapters.base.kill_process_group_graceful", return_value=False):
            adapter.kill(556)  # must not raise


# ---------------------------------------------------------------------------
# detect_tier()
# ---------------------------------------------------------------------------


class TestQwenDetectTier:
    def test_returns_none_without_any_keys(self) -> None:
        adapter = QwenAdapter()
        settings = _default_settings()
        with patch("bernstein.adapters.qwen.LLMSettings", return_value=settings):
            assert adapter.detect_tier() is None

    def test_pro_with_openrouter_paid(self) -> None:
        adapter = QwenAdapter()
        settings = _default_settings(openrouter_api_key_paid="or-key")
        with patch("bernstein.adapters.qwen.LLMSettings", return_value=settings):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.PRO
        assert info.provider == ProviderType.QWEN

    def test_free_with_openrouter_free(self) -> None:
        adapter = QwenAdapter()
        settings = _default_settings(openrouter_api_key_free="or-free-key")
        with patch("bernstein.adapters.qwen.LLMSettings", return_value=settings):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.FREE

    def test_plus_with_together(self) -> None:
        adapter = QwenAdapter()
        settings = _default_settings(togetherai_user_key="tog-key")
        with patch("bernstein.adapters.qwen.LLMSettings", return_value=settings):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.PLUS

    def test_default_with_openai_key(self) -> None:
        adapter = QwenAdapter()
        settings = _default_settings(openai_api_key="sk-test")
        with patch("bernstein.adapters.qwen.LLMSettings", return_value=settings):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.PLUS


# ---------------------------------------------------------------------------
# _detect_provider() / _resolve_provider_config()
# ---------------------------------------------------------------------------


class TestQwenProviderDetection:
    def test_openrouter_paid_takes_priority(self) -> None:
        adapter = QwenAdapter()
        settings = _default_settings(
            openrouter_api_key_paid="or-paid",
            togetherai_user_key="tog",
        )
        assert adapter._detect_provider(settings) == "openrouter"

    def test_openrouter_free_second(self) -> None:
        adapter = QwenAdapter()
        settings = _default_settings(openrouter_api_key_free="or-free")
        assert adapter._detect_provider(settings) == "openrouter_free"

    def test_together_third(self) -> None:
        adapter = QwenAdapter()
        settings = _default_settings(togetherai_user_key="tog-key")
        assert adapter._detect_provider(settings) == "together"

    def test_oxen_fourth(self) -> None:
        adapter = QwenAdapter()
        settings = _default_settings(oxen_api_key="oxen-key")
        assert adapter._detect_provider(settings) == "oxen"

    def test_g4f_fifth(self) -> None:
        adapter = QwenAdapter()
        settings = _default_settings(g4f_api_key="g4f-key")
        assert adapter._detect_provider(settings) == "g4f"

    def test_default_fallback(self) -> None:
        adapter = QwenAdapter()
        settings = _default_settings()
        assert adapter._detect_provider(settings) == "default"

    def test_resolve_openrouter_config(self) -> None:
        adapter = QwenAdapter()
        settings = _default_settings(openrouter_api_key_paid="or-paid")
        key, url = adapter._resolve_provider_config("openrouter", settings)
        assert key == "or-paid"
        assert "openrouter.ai" in url

    def test_resolve_together_config(self) -> None:
        adapter = QwenAdapter()
        settings = _default_settings(togetherai_user_key="tog-key")
        key, url = adapter._resolve_provider_config("together", settings)
        assert key == "tog-key"
        assert "together" in url
