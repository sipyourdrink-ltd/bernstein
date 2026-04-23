"""Unit tests for MistralAdapter spawn/name."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.mistral import MistralAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


class TestMistralAdapterSpawn:
    """MistralAdapter.spawn() builds the expected command."""

    def test_spawn_builds_run_command(self, tmp_path: Path) -> None:
        adapter = MistralAdapter()
        proc_mock = make_popen_mock(pid=700)
        with patch("bernstein.adapters.mistral.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="mistral-large", effort="high"),
                session_id="mistral-s1",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner == ["vibe", "--auto-approve", "--prompt", "fix the bug"]

    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = MistralAdapter()
        with (
            patch(
                "bernstein.adapters.mistral.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match=r"vibe not found.*mistral\.ai"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="mistral-large", effort="high"),
                session_id="mistral-missing",
            )


class TestMistralAdapterName:
    def test_name(self) -> None:
        assert MistralAdapter().name() == "Mistral Vibe"
