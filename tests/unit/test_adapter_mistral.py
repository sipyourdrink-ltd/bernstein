"""Unit tests for MistralAdapter spawn/name."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.mistral import MistralAdapter
from bernstein.adapters.plugin_sdk import AdapterCapability, SamplingParamsRefusal, ensure_sampling_params_supported
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


class TestMistralAdapterSpawn:
    """MistralAdapter.spawn() builds the expected command."""

    def test_spawn_builds_run_command(self, tmp_path: Path) -> None:
        adapter = MistralAdapter()
        proc_mock = make_popen_mock(pid=700)
        with patch("bernstein.adapters.mistral.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="mistral-large", effort="high"),
                session_id="mistral-s1",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner == ["vibe", "--auto-approve", "--prompt", "fix the bug"]

    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = MistralAdapter()
        with (
            patch(
                "bernstein.adapters.mistral.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match=r"vibe not found.*mistral\.ai"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="mistral-large", effort="high"),
                session_id="mistral-missing",
            )


class TestMistralAdapterName:
    def test_name(self) -> None:
        assert MistralAdapter().name() == "Mistral Vibe"


class TestMistralAdapterPluginInfo:
    def test_declares_no_sampling_capability(self) -> None:
        """vibe 2.26 has no sampling flag: ``--temperature`` is answered with
        "unrecognized arguments" and exit 1."""
        info = MistralAdapter().plugin_info()
        assert AdapterCapability.SUPPORTS_TEMPERATURE not in info.capabilities
        assert info.capabilities == ()

    def test_a_requested_temperature_is_refused_before_spawn(self) -> None:
        with pytest.raises(SamplingParamsRefusal, match="temperature"):
            ensure_sampling_params_supported(MistralAdapter(), {"temperature": 0.4})

    def test_does_not_declare_coarse_or_others(self) -> None:
        info = MistralAdapter().plugin_info()
        assert AdapterCapability.SUPPORTS_SAMPLING_PARAMS not in info.capabilities
        assert AdapterCapability.SUPPORTS_TOP_P not in info.capabilities
        assert AdapterCapability.SUPPORTS_TOP_K not in info.capabilities
        assert AdapterCapability.SUPPORTS_MAX_TOKENS not in info.capabilities


class TestMistralAdapterSamplingParams:
    """No sampling key ever reaches vibe's argv: vibe rejects the flags."""

    def test_temperature_never_reaches_argv(self, tmp_path: Path) -> None:
        adapter = MistralAdapter()
        proc_mock = make_popen_mock(pid=701)
        with patch("bernstein.adapters.mistral.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="mistral-large", effort="high"),
                session_id="mistral-sampling1",
                mcp_config={"temperature": 0.4},
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert "--temperature" not in inner
        assert inner == ["vibe", "--auto-approve", "--prompt", "fix the bug"]

    def test_no_mcp_config_omits_temperature_flag(self, tmp_path: Path) -> None:
        adapter = MistralAdapter()
        proc_mock = make_popen_mock(pid=702)
        with patch("bernstein.adapters.mistral.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="mistral-large", effort="high"),
                session_id="mistral-sampling2",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner == ["vibe", "--auto-approve", "--prompt", "fix the bug"]

    def test_top_p_top_k_max_tokens_never_reach_argv(self, tmp_path: Path) -> None:
        adapter = MistralAdapter()
        proc_mock = make_popen_mock(pid=703)
        with patch("bernstein.adapters.mistral.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="mistral-large", effort="high"),
                session_id="mistral-sampling3",
                mcp_config={"top_p": 0.9, "top_k": 40, "max_tokens": 4096},
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert "--top-p" not in inner
        assert "--top-k" not in inner
        assert "--max-tokens" not in inner
