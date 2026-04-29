"""Unit tests for PlandexAdapter."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.plandex import PlandexAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


def test_spawn_builds_run_command(tmp_path: Path) -> None:
    adapter = PlandexAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.plandex.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="refactor module",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="plandex-s1",
        )

    cmd = popen.call_args.args[0]
    inner = inner_cmd(cmd)
    assert inner[:3] == ["plandex", "tell", "refactor module"]
    assert "--apply" in inner
    assert "--auto-exec" in inner
    assert "--skip-menu" in inner
    assert "--stop" in inner


def test_spawn_translates_missing_cli(tmp_path: Path) -> None:
    adapter = PlandexAdapter()
    with (
        patch(
            "bernstein.adapters.plandex.subprocess.Popen",
            side_effect=FileNotFoundError("No such file"),
        ),
        pytest.raises(RuntimeError, match="plandex not found"),
    ):
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="plandex-missing",
        )


def test_name() -> None:
    assert PlandexAdapter().name() == "Plandex"
