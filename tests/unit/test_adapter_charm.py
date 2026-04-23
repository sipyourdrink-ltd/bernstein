"""Unit tests for CharmAdapter spawn/name."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.charm import CharmAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


class TestCharmAdapterSpawn:
    def test_spawn_builds_run_command(self, tmp_path: Path) -> None:
        adapter = CharmAdapter()
        proc_mock = make_popen_mock(pid=950)
        with patch("bernstein.adapters.charm.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="refactor the module",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="charm-s1",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner == ["crush", "--yolo", "refactor the module"]

    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = CharmAdapter()
        with (
            patch(
                "bernstein.adapters.charm.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match=r"crush not found.*@charmland/crush"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="charm-missing",
            )


class TestCharmAdapterName:
    def test_name(self) -> None:
        assert CharmAdapter().name() == "Charm"
