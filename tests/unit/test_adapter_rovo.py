"""Unit tests for RovoAdapter spawn/name."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.rovo import RovoAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


class TestRovoAdapterSpawn:
    """RovoAdapter.spawn() builds the expected command."""

    def test_spawn_builds_run_command(self, tmp_path: Path) -> None:
        adapter = RovoAdapter()
        proc_mock = make_popen_mock(pid=700)
        with patch("bernstein.adapters.rovo.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="rovo-s1",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner[:4] == ["acli", "rovodev", "run", "--yolo"]
        assert inner[-1] == "fix the bug"


class TestRovoSpawnMissingBinary:
    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = RovoAdapter()
        with (
            patch(
                "bernstein.adapters.rovo.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError) as excinfo,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="rovo-missing",
            )
        message = str(excinfo.value)
        assert "acli not found" in message
        assert "rovodev" in message
        assert "acli rovodev auth login" in message


class TestRovoAdapterName:
    def test_name(self) -> None:
        assert RovoAdapter().name() == "Rovo Dev"
