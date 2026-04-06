"""Unit tests for Codex, Gemini, Qwen, and Generic adapter spawn/kill/is_alive."""

from __future__ import annotations

import signal
import subprocess
import sys
from collections.abc import Callable, Generator
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.codex import CodexAdapter
from bernstein.adapters.gemini import GeminiAdapter
from bernstein.adapters.generic import GenericAdapter
from bernstein.adapters.qwen import QwenAdapter
from bernstein.core.models import ModelConfig

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.adapters.base import CLIAdapter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_watchdog_threads() -> Generator[None, None, None]:
    """Disable watchdog threads in tests to avoid 'can't start new thread' on CI."""
    with patch("bernstein.adapters.base.CLIAdapter._start_timeout_watchdog", return_value=None):
        yield


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


def _spawn_codex(tmp_path: Path, model: str = "gpt-4o") -> tuple[list[str], MagicMock]:
    adapter = CodexAdapter()
    proc_mock = _make_popen_mock(pid=100)
    with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
        with patch("builtins.open", MagicMock()):
            adapter.spawn(
                prompt="do work",
                workdir=tmp_path,
                model_config=ModelConfig(model=model, effort="high"),
                session_id="sess-codex",
            )
    cmd: list[str] = popen.call_args.args[0]
    return cmd, proc_mock


def _spawn_gemini(tmp_path: Path, model: str = "gemini-pro") -> tuple[list[str], MagicMock]:
    GeminiAdapter()
    proc_mock = _make_popen_mock(pid=200)
    with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock):
        with patch("builtins.open", MagicMock()):
            pass
    # Redo properly with real filesystem so log_path.open works
    adapter2 = GeminiAdapter()
    proc_mock2 = _make_popen_mock(pid=200)
    with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock2) as popen:
        adapter2.spawn(
            prompt="do work",
            workdir=tmp_path,
            model_config=ModelConfig(model=model, effort="high"),
            session_id="sess-gemini",
        )
    cmd: list[str] = popen.call_args.args[0]
    return cmd, proc_mock2


# ---------------------------------------------------------------------------
# CodexAdapter
# ---------------------------------------------------------------------------


class TestCodexAdapterSpawn:
    """CodexAdapter.spawn() builds correct command."""

    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=101)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="s1",
            )
        cmd = popen.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.worker"]
        inner = _inner_cmd(cmd)
        assert inner[0] == "codex"

    def test_model_flag(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=102)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4.1", effort="high"),
                session_id="s2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "-m" in inner
        assert inner[inner.index("-m") + 1] == "gpt-4.1"

    def test_exec_mode_full_auto(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=103)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="s3",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[:3] == ["codex", "exec", "--full-auto"]

    def test_json_output_file_flags(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=104)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="s4",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--json" in inner
        assert "-o" in inner
        assert inner[inner.index("-o") + 1].endswith("s4.last-message.txt")

    def test_prompt_appended_last(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=105)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="my-unique-prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="s5",
            )
        cmd = popen.call_args.args[0]
        assert cmd[-1] == "my-unique-prompt"

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=106)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="s6",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_spawn_result_contains_pid(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=107)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="s7",
            )
        assert result.pid == 107

    def test_log_path_uses_session_id(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=108)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="my-session",
            )
        assert result.log_path.name == "my-session.log"


# ---------------------------------------------------------------------------
# GeminiAdapter
# ---------------------------------------------------------------------------


class TestGeminiAdapterSpawn:
    """GeminiAdapter.spawn() builds correct command."""

    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=201)
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-2.0-flash", effort="high"),
                session_id="g1",
            )
        cmd = popen.call_args.args[0]
        inner = _inner_cmd(cmd)
        assert inner[0] == "gemini"

    def test_model_flag(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=202)
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-pro", effort="high"),
                session_id="g2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "-m" in inner
        assert inner[inner.index("-m") + 1] == "gemini-pro"

    def test_yolo_flag(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=203)
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-pro", effort="high"),
                session_id="g3",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--yolo" in inner

    def test_prompt_flag(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=204)
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="do-something",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-pro", effort="high"),
                session_id="g4",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "-p" in inner
        assert inner[inner.index("-p") + 1] == "do-something"

    def test_json_output_flag(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=207)
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-pro", effort="high"),
                session_id="g7",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--output-format" in inner
        assert inner[inner.index("--output-format") + 1] == "json"

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=205)
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-pro", effort="high"),
                session_id="g5",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_spawn_result_pid(self, tmp_path: Path) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=206)
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-pro", effort="high"),
                session_id="g6",
            )
        assert result.pid == 206


# ---------------------------------------------------------------------------
# QwenAdapter
# ---------------------------------------------------------------------------


def _make_llm_settings(**kwargs: object) -> MagicMock:
    """Build a LLMSettings mock with all fields defaulting to None."""
    defaults = dict(
        openrouter_api_key_paid=None,
        openrouter_api_key_free=None,
        oxen_api_key=None,
        oxen_base_url="https://hub.oxen.ai/api",
        togetherai_user_key=None,
        g4f_api_key=None,
        g4f_base_url="https://g4f.space/v1",
        openai_api_key=None,
        openai_base_url=None,
        tavily_api_key=None,
    )
    defaults.update(kwargs)
    m = MagicMock()
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


class TestQwenAdapterSpawn:
    """QwenAdapter.spawn() builds correct command with provider routing."""

    def test_default_provider_uses_qwen_command(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=301)
        settings_mock = _make_llm_settings()
        with (
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings_mock),
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="q1",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[0] == "qwen"

    def test_yolo_flag_always_present(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=302)
        settings_mock = _make_llm_settings()
        with (
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings_mock),
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="q2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "-y" in inner

    def test_default_provider_maps_sonnet_to_qwen_plus(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=303)
        settings_mock = _make_llm_settings()
        with (
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings_mock),
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="q3",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--model" in inner
        assert inner[inner.index("--model") + 1] == "qwen3.6-plus"

    def test_default_provider_maps_opus_to_qwen_plus(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=304)
        settings_mock = _make_llm_settings()
        with (
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings_mock),
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="opus", effort="high"),
                session_id="q4",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[inner.index("--model") + 1] == "qwen3.6-plus"

    def test_openrouter_provider_sets_auth_type(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=305)
        settings_mock = _make_llm_settings(openrouter_api_key_paid="or-key-123")
        model_config = MagicMock(spec=ModelConfig)
        model_config.model = "qwen/qwen-turbo"
        model_config.effort = "high"
        model_config.provider = "openrouter"
        with (
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings_mock),
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=model_config,
                session_id="q5",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--auth-type" in inner
        assert inner[inner.index("--auth-type") + 1] == "openai"

    def test_openrouter_provider_sets_env_vars(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=306)
        settings_mock = _make_llm_settings(openrouter_api_key_paid="or-key-abc")
        model_config = MagicMock(spec=ModelConfig)
        model_config.model = "qwen/qwen-turbo"
        model_config.effort = "high"
        model_config.provider = "openrouter"
        with (
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings_mock),
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=model_config,
                session_id="q6",
            )
        kwargs = popen.call_args.kwargs
        env = kwargs.get("env", {})
        assert env.get("OPENAI_API_KEY") == "or-key-abc"
        assert env.get("OPENAI_BASE_URL") == "https://openrouter.ai/api/v1"

    def test_tavily_flags_when_key_present(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=307)
        settings_mock = _make_llm_settings(tavily_api_key="tv-key-xyz")
        with (
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings_mock),
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="q7",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--tavily-api-key" in inner
        assert inner[inner.index("--tavily-api-key") + 1] == "tv-key-xyz"
        assert "--web-search-default" in inner

    def test_spawn_result_pid(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=308)
        settings_mock = _make_llm_settings()
        with (
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings_mock),
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock),
        ):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="q8",
            )
        assert result.pid == 308

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=309)
        settings_mock = _make_llm_settings()
        with (
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings_mock),
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="q9",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()


# ---------------------------------------------------------------------------
# GenericAdapter
# ---------------------------------------------------------------------------


class TestGenericAdapterSpawn:
    """GenericAdapter.spawn() respects configuration passed at construction."""

    def test_cli_command_used(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="aider", display_name="Aider")
        proc_mock = _make_popen_mock(pid=401)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen1",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[0] == "aider"

    def test_model_flag_included(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="aider", model_flag="--model")
        proc_mock = _make_popen_mock(pid=402)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--model" in inner
        assert inner[inner.index("--model") + 1] == "gpt-4o"

    def test_model_flag_omitted_when_none(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="mytool", model_flag=None)
        proc_mock = _make_popen_mock(pid=403)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen3",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        # Worker has its own --model flag; only check the inner command
        assert "--model" not in inner

    def test_custom_prompt_flag(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="cursor", prompt_flag="--message")
        proc_mock = _make_popen_mock(pid=404)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="do-this",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen4",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--message" in inner
        assert inner[inner.index("--message") + 1] == "do-this"

    def test_extra_args_included(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="mytool", extra_args=["--yes", "--verbose"])
        proc_mock = _make_popen_mock(pid=405)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen5",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--yes" in inner
        assert "--verbose" in inner

    def test_spawn_result_pid(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="mytool")
        proc_mock = _make_popen_mock(pid=406)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen6",
            )
        assert result.pid == 406

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        adapter = GenericAdapter(cli_command="mytool")
        proc_mock = _make_popen_mock(pid=407)
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="gen7",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()


class TestGenericAdapterName:
    """GenericAdapter.name() returns display_name."""

    def test_default_display_name(self) -> None:
        adapter = GenericAdapter(cli_command="mytool")
        assert adapter.name() == "Generic CLI"

    def test_custom_display_name(self) -> None:
        adapter = GenericAdapter(cli_command="aider", display_name="Aider Agent")
        assert adapter.name() == "Aider Agent"


# ---------------------------------------------------------------------------
# is_alive() — all adapters share the same pattern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_factory",
    [
        lambda: CodexAdapter(),
        lambda: GeminiAdapter(),
        lambda: QwenAdapter(),
        lambda: GenericAdapter(cli_command="mytool"),
    ],
    ids=["codex", "gemini", "qwen", "generic"],
)
class TestIsAlive:
    """is_alive() returns True/False based on process_alive(pid)."""

    def test_true_when_process_exists(self, adapter_factory: Callable[[], CLIAdapter]) -> None:
        adapter = adapter_factory()
        with patch("bernstein.adapters.base.process_alive", return_value=True) as mock_alive:
            assert adapter.is_alive(1234) is True
        mock_alive.assert_called_once_with(1234)

    def test_false_when_oserror(self, adapter_factory: Callable[[], CLIAdapter]) -> None:
        adapter = adapter_factory()
        with patch("bernstein.adapters.base.process_alive", return_value=False):
            assert adapter.is_alive(9999) is False


# ---------------------------------------------------------------------------
# kill() — all adapters call killpg
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_factory",
    [
        lambda: CodexAdapter(),
        lambda: GeminiAdapter(),
        lambda: QwenAdapter(),
        lambda: GenericAdapter(cli_command="mytool"),
    ],
    ids=["codex", "gemini", "qwen", "generic"],
)
class TestKill:
    """kill() calls kill_process_group with SIGTERM and handles failure gracefully."""

    def test_calls_killpg(self, adapter_factory: Callable[[], CLIAdapter]) -> None:
        adapter = adapter_factory()
        with patch("bernstein.adapters.base.kill_process_group") as mock_killpg:
            adapter.kill(555)
        # PID is used directly as PGID (start_new_session=True)
        mock_killpg.assert_called_once_with(555, signal.SIGTERM)

    def test_does_not_raise_on_oserror(self, adapter_factory: Callable[[], CLIAdapter]) -> None:
        adapter = adapter_factory()
        with patch("bernstein.adapters.base.kill_process_group", return_value=False):
            adapter.kill(556)  # must not raise


# ---------------------------------------------------------------------------
# spawn() — missing CLI binary / PermissionError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_factory,popen_path",
    [
        (lambda: CodexAdapter(), "bernstein.adapters.codex.subprocess.Popen"),
        (lambda: GeminiAdapter(), "bernstein.adapters.gemini.subprocess.Popen"),
        (
            lambda: GenericAdapter(cli_command="notarealthing"),
            "bernstein.adapters.generic.subprocess.Popen",
        ),
    ],
    ids=["codex", "gemini", "generic"],
)
class TestSpawnMissingBinary:
    """spawn() raises RuntimeError with a clear message when binary is missing."""

    def test_file_not_found_raises_runtime_error(
        self, adapter_factory: Callable[[], CLIAdapter], popen_path: str, tmp_path: Path
    ) -> None:
        adapter = adapter_factory()
        with patch(popen_path, side_effect=FileNotFoundError("No such file")):
            with pytest.raises(RuntimeError, match="not found in PATH"):
                adapter.spawn(
                    prompt="hello",
                    workdir=tmp_path,
                    model_config=ModelConfig(model="gpt-4o", effort="high"),
                    session_id="missing-bin",
                )

    def test_permission_error_raises_runtime_error(
        self, adapter_factory: Callable[[], CLIAdapter], popen_path: str, tmp_path: Path
    ) -> None:
        adapter = adapter_factory()
        with patch(popen_path, side_effect=PermissionError("Permission denied")):
            with pytest.raises(RuntimeError, match="[Pp]ermission"):
                adapter.spawn(
                    prompt="hello",
                    workdir=tmp_path,
                    model_config=ModelConfig(model="gpt-4o", effort="high"),
                    session_id="perm-denied",
                )


class TestQwenSpawnMissingBinary:
    """QwenAdapter.spawn() raises RuntimeError when binary is missing."""

    def test_file_not_found_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        settings_mock = _make_llm_settings()
        with (
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings_mock),
            patch(
                "bernstein.adapters.qwen.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="not found in PATH"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="qwen-missing",
            )

    def test_permission_error_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        settings_mock = _make_llm_settings()
        with (
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings_mock),
            patch(
                "bernstein.adapters.qwen.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="qwen-perm",
            )
