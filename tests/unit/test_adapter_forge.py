"""Unit tests for ForgeAdapter spawn and metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.forge import ForgeAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


class TestForgeAdapterSpawn:
    """ForgeAdapter.spawn() builds the correct command."""

    def test_spawn_builds_run_command(self, tmp_path: Path) -> None:
        adapter = ForgeAdapter()
        proc_mock = make_popen_mock(pid=700)
        with patch("bernstein.adapters.forge.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="forge-s1",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner == ["forge", "-p", "fix the bug"]

    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = ForgeAdapter()
        with (
            patch(
                "bernstein.adapters.forge.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match=r"forge not found.*forgecode\.dev"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="forge-missing",
            )


class TestForgeAdapterName:
    """ForgeAdapter.name() returns the human-readable label."""

    def test_name(self) -> None:
        assert ForgeAdapter().name() == "Forge"
