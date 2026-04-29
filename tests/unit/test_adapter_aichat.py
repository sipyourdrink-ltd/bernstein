"""Unit tests for AIChatAdapter."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.aichat import AIChatAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


def test_spawn_builds_run_command(tmp_path: Path) -> None:
    adapter = AIChatAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.aichat.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="summarize this",
            workdir=tmp_path,
            model_config=ModelConfig(model="openai:gpt-4o", effort="high"),
            session_id="aichat-s1",
        )

    cmd = popen.call_args.args[0]
    inner = inner_cmd(cmd)
    assert inner[:3] == ["aichat", "-m", "openai:gpt-4o"]
    assert inner[-2] == "--"
    assert inner[-1] == "summarize this"


def test_spawn_translates_missing_cli(tmp_path: Path) -> None:
    adapter = AIChatAdapter()
    with (
        patch(
            "bernstein.adapters.aichat.subprocess.Popen",
            side_effect=FileNotFoundError("No such file"),
        ),
        pytest.raises(RuntimeError, match="aichat not found"),
    ):
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="openai:gpt-4o", effort="high"),
            session_id="aichat-missing",
        )


def test_name() -> None:
    assert AIChatAdapter().name() == "AIChat"
