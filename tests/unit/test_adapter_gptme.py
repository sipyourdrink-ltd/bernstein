"""Unit tests for GptmeAdapter."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.gptme import GptmeAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


def test_spawn_builds_run_command(tmp_path: Path) -> None:
    adapter = GptmeAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.gptme.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="gptme-s1",
        )

    cmd = popen.call_args.args[0]
    inner = inner_cmd(cmd)
    assert inner[:4] == ["gptme", "-n", "-m", "anthropic/claude-sonnet-4-6"]
    assert inner[-1] == "fix the bug"


def test_spawn_translates_missing_cli(tmp_path: Path) -> None:
    adapter = GptmeAdapter()
    with (
        patch(
            "bernstein.adapters.gptme.subprocess.Popen",
            side_effect=FileNotFoundError("No such file"),
        ),
        pytest.raises(RuntimeError, match="gptme not found"),
    ):
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="gptme-missing",
        )


def test_spawn_forwards_max_tokens_env(tmp_path: Path) -> None:
    adapter = GptmeAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.gptme.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="gptme-s1",
            mcp_config={"max_tokens": 4096},
        )

    env = popen.call_args.kwargs["env"]
    assert env["GPTME_MAX_TOKENS"] == "4096"


def test_spawn_omits_max_tokens_env_when_unset(tmp_path: Path) -> None:
    adapter = GptmeAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.gptme.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="gptme-s1",
        )

    env = popen.call_args.kwargs["env"]
    assert "GPTME_MAX_TOKENS" not in env


def test_name() -> None:
    assert GptmeAdapter().name() == "gptme"


def test_sampling_gate_admits_max_tokens_but_refuses_unwired_keys() -> None:
    from bernstein.adapters.plugin_sdk import (
        SamplingParamsRefusal,
        ensure_sampling_params_supported,
    )

    adapter = GptmeAdapter()

    ensure_sampling_params_supported(adapter, {"max_tokens": 4096})

    with pytest.raises(SamplingParamsRefusal):
        ensure_sampling_params_supported(adapter, {"temperature": 0.5})
