"""Unit tests for OpenInterpreterAdapter."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.open_interpreter import OpenInterpreterAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


def test_spawn_builds_run_command(tmp_path: Path) -> None:
    adapter = OpenInterpreterAdapter()
    proc_mock = make_popen_mock(801)

    with patch(
        "bernstein.adapters.open_interpreter.subprocess.Popen",
        return_value=proc_mock,
    ) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-4o", effort="high"),
            session_id="oi-s1",
        )

    cmd = popen.call_args.args[0]
    inner = inner_cmd(cmd)
    assert inner[0] == "interpreter"
    # The -y / --auto_run flag is mandatory; without it the process hangs
    # on every code-execution confirmation. This assertion is the
    # make-or-break check for this adapter.
    assert "-y" in inner
    assert inner[inner.index("--model") + 1] == "gpt-4o"
    assert inner[-1] == "fix the bug"


def test_spawn_translates_missing_cli(tmp_path: Path) -> None:
    adapter = OpenInterpreterAdapter()
    with (
        patch(
            "bernstein.adapters.open_interpreter.subprocess.Popen",
            side_effect=FileNotFoundError("No such file"),
        ),
        pytest.raises(RuntimeError, match="interpreter not found"),
    ):
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-4o", effort="high"),
            session_id="oi-missing",
        )


def test_name() -> None:
    assert OpenInterpreterAdapter().name() == "Open Interpreter"
