"""Unit tests for OllamaAdapter spawn/name/plugin_info.

No test file previously existed for this adapter (verified via
``find tests -iname '*ollama*'`` before writing this file). Pattern
matched from ``test_adapter_qwen.py`` / ``test_adapter_mistral.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.ollama import OllamaAdapter
from bernstein.adapters.plugin_sdk import AdapterCapability
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


class TestOllamaAdapterSpawn:
    """OllamaAdapter.spawn() builds the expected aider command."""

    def test_spawn_builds_aider_command(self, tmp_path: Path) -> None:
        adapter = OllamaAdapter()
        proc_mock = make_popen_mock(pid=800)
        with patch("bernstein.adapters.ollama.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen2.5-coder:7b", effort="high"),
                session_id="ollama-s1",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner[0] == "aider"
        assert "--model" in inner
        assert inner[inner.index("--model") + 1] == "ollama/qwen2.5-coder:7b"
        assert "--message" in inner
        assert inner[inner.index("--message") + 1] == "fix the bug"

    def test_name(self) -> None:
        assert OllamaAdapter().name() == "Ollama (local)"


class TestOllamaAdapterPluginInfo:
    def test_declares_no_sampling_capability(self) -> None:
        """Aider exposes no sampling flags, so none may be advertised.

        Declaring one lets ``ensure_sampling_params_supported`` admit a spawn
        that then builds an argv aider rejects with
        ``unrecognized arguments``.
        """
        info = OllamaAdapter().plugin_info()
        assert set(info.capabilities) == set()

    def test_does_not_declare_coarse_or_others(self) -> None:
        info = OllamaAdapter().plugin_info()
        assert AdapterCapability.SUPPORTS_SAMPLING_PARAMS not in info.capabilities
        assert AdapterCapability.SUPPORTS_TEMPERATURE not in info.capabilities
        assert AdapterCapability.SUPPORTS_TOP_P not in info.capabilities
        assert AdapterCapability.SUPPORTS_TOP_K not in info.capabilities
        assert AdapterCapability.SUPPORTS_MAX_TOKENS not in info.capabilities


class TestOllamaAdapterSamplingParams:
    """No sampling param may reach the built aider argv.

    Aider has no ``--temperature`` flag: passing one makes argparse exit
    with ``unrecognized arguments: --temperature``, killing the spawn.
    """

    def test_temperature_never_reaches_argv(self, tmp_path: Path) -> None:
        adapter = OllamaAdapter()
        proc_mock = make_popen_mock(pid=801)
        with patch("bernstein.adapters.ollama.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen2.5-coder:7b", effort="high"),
                session_id="ollama-sampling1",
                mcp_config={"temperature": 0.15},
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert "--temperature" not in inner

    def test_no_mcp_config_omits_temperature_flag(self, tmp_path: Path) -> None:
        adapter = OllamaAdapter()
        proc_mock = make_popen_mock(pid=802)
        with patch("bernstein.adapters.ollama.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen2.5-coder:7b", effort="high"),
                session_id="ollama-sampling2",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert "--temperature" not in inner

    def test_top_p_top_k_max_tokens_never_reach_argv(self, tmp_path: Path) -> None:
        adapter = OllamaAdapter()
        proc_mock = make_popen_mock(pid=803)
        with patch("bernstein.adapters.ollama.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="qwen2.5-coder:7b", effort="high"),
                session_id="ollama-sampling3",
                mcp_config={"top_p": 0.9, "top_k": 40, "max_tokens": 4096},
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert "--top-p" not in inner
        assert "--top-k" not in inner
        assert "--max-tokens" not in inner
