"""Unit tests for OpenHandsAdapter."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.openhands import OpenHandsAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


def test_spawn_builds_run_command(tmp_path: Path) -> None:
    adapter = OpenHandsAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.openhands.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="openhands-s1",
        )

    cmd = popen.call_args.args[0]
    inner = inner_cmd(cmd)
    assert inner[:4] == ["openhands", "--headless", "--override-with-envs", "-t"]
    assert inner[-1] == "fix the bug"


def test_spawn_translates_missing_cli(tmp_path: Path) -> None:
    adapter = OpenHandsAdapter()
    with (
        patch(
            "bernstein.adapters.openhands.subprocess.Popen",
            side_effect=FileNotFoundError("No such file"),
        ),
        pytest.raises(RuntimeError, match="openhands not found"),
    ):
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="openhands-missing",
        )


def test_name() -> None:
    assert OpenHandsAdapter().name() == "OpenHands"
