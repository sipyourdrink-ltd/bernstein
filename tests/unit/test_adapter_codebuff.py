"""Unit tests for CodebuffAdapter."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.codebuff import CodebuffAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


class TestCodebuffAdapterSpawn:
    """CodebuffAdapter.spawn() builds correct command."""

    def test_spawn_builds_run_command(self, tmp_path: Path) -> None:
        adapter = CodebuffAdapter()
        proc_mock = make_popen_mock(pid=700)
        with patch("bernstein.adapters.codebuff.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="codebuff-s1",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner == ["codebuff", "fix the bug"]

    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = CodebuffAdapter()
        with (
            patch(
                "bernstein.adapters.codebuff.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="codebuff not found") as excinfo,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="codebuff-missing",
            )
        assert "npm install -g codebuff" in str(excinfo.value)


class TestCodebuffAdapterName:
    def test_name(self) -> None:
        assert CodebuffAdapter().name() == "Codebuff"
