"""Unit tests for ClineAdapter spawn/name."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.cline import ClineAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


class TestClineAdapterSpawn:
    """ClineAdapter.spawn() builds the expected command."""

    def test_spawn_builds_run_command(self, tmp_path: Path) -> None:
        adapter = ClineAdapter()
        proc_mock = make_popen_mock(pid=800)
        with patch("bernstein.adapters.cline.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="refactor module",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="cline-s1",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner[:2] == ["cline", "--yolo"]
        assert inner[-1] == "refactor module"


class TestClineSpawnMissingBinary:
    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = ClineAdapter()
        with (
            patch(
                "bernstein.adapters.cline.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="not found in PATH"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="cline-missing",
            )


class TestClineAdapterName:
    def test_name(self) -> None:
        assert ClineAdapter().name() == "Cline"
