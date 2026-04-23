"""Unit tests for DroidAdapter."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.droid import DroidAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


def test_spawn_builds_run_command(tmp_path: Path) -> None:
    adapter = DroidAdapter()
    proc_mock = make_popen_mock(700)

    with patch("bernstein.adapters.droid.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="droid-s1",
        )

    cmd = popen.call_args.args[0]
    inner = inner_cmd(cmd)
    assert inner == ["droid", "fix the bug"]


def test_spawn_translates_missing_cli(tmp_path: Path) -> None:
    adapter = DroidAdapter()
    with (
        patch(
            "bernstein.adapters.droid.subprocess.Popen",
            side_effect=FileNotFoundError("No such file"),
        ),
        pytest.raises(RuntimeError) as excinfo,
    ):
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="droid-missing",
        )

    message = str(excinfo.value)
    assert "droid not found" in message
    assert "https://app.factory.ai/cli" in message


def test_name() -> None:
    assert DroidAdapter().name() == "Droid"
