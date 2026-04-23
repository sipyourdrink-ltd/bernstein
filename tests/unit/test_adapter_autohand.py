"""Unit tests for AutohandAdapter spawn/name."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.autohand import AutohandAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


class TestAutohandAdapterSpawn:
    """AutohandAdapter.spawn() builds the expected command."""

    def test_spawn_builds_run_command(self, tmp_path: Path) -> None:
        adapter = AutohandAdapter()
        proc_mock = make_popen_mock(pid=800)
        with patch("bernstein.adapters.autohand.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="autohand-s1",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner == ["autohand", "--unrestricted", "-p", "fix the bug"]

    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = AutohandAdapter()
        with (
            patch(
                "bernstein.adapters.autohand.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="not found in PATH"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="autohand-missing",
            )


class TestAutohandAdapterName:
    def test_name(self) -> None:
        assert AutohandAdapter().name() == "Autohand"
