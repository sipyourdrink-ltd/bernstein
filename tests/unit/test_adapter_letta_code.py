"""Unit tests for LettaCodeAdapter."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.letta_code import LettaCodeAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


def test_spawn_builds_run_command(tmp_path: Path) -> None:
    adapter = LettaCodeAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.letta_code.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="letta-s1",
        )

    cmd = popen.call_args.args[0]
    inner = inner_cmd(cmd)
    assert inner[:3] == ["letta", "--yolo", "-p"]
    assert inner[-1] == "fix the bug"


def test_spawn_translates_missing_cli(tmp_path: Path) -> None:
    adapter = LettaCodeAdapter()
    with (
        patch(
            "bernstein.adapters.letta_code.subprocess.Popen",
            side_effect=FileNotFoundError("No such file"),
        ),
        pytest.raises(RuntimeError, match="letta not found"),
    ):
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="letta-missing",
        )


def test_name() -> None:
    assert LettaCodeAdapter().name() == "Letta Code"
