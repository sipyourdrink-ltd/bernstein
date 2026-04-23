"""Unit tests for HermesAdapter spawn/name."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.hermes import HermesAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


class TestHermesAdapterSpawn:
    def test_spawn_builds_run_command(self, tmp_path: Path) -> None:
        adapter = HermesAdapter()
        proc_mock = make_popen_mock(pid=900)
        with patch("bernstein.adapters.hermes.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="ship the feature",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="hermes-s1",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner == ["hermes", "ship the feature"]

    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = HermesAdapter()
        with (
            patch(
                "bernstein.adapters.hermes.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match=r"hermes not found.*NousResearch/hermes-agent"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="hermes-missing",
            )


class TestHermesAdapterName:
    def test_name(self) -> None:
        assert HermesAdapter().name() == "Hermes Agent"
