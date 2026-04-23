"""Unit tests for KimiAdapter spawn command construction."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.kimi import KimiAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


class TestKimiAdapterSpawn:
    """KimiAdapter.spawn() builds correct command."""

    def test_spawn_builds_run_command(self, tmp_path: Path) -> None:
        adapter = KimiAdapter()
        proc_mock = make_popen_mock(pid=800)
        with patch("bernstein.adapters.kimi.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="kimi-s1",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner[:3] == ["kimi", "--yolo", "-c"]
        assert inner[-1] == "fix the bug"

    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = KimiAdapter()
        with (
            patch(
                "bernstein.adapters.kimi.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="kimi-missing",
            )
        msg = str(exc_info.value)
        assert "kimi not found" in msg
        assert "uv tool install kimi-cli" in msg


class TestKimiAdapterName:
    def test_name(self) -> None:
        assert KimiAdapter().name() == "Kimi"
