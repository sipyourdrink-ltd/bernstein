"""Unit tests for KiloAdapter spawn command construction.

The adapter used to run ``kilo run --prompt <task> --model <m> --yes [--mcp <json>]``.
``kilo run`` (``@kilocode/cli`` 7.8) has none of ``--prompt``, ``--yes`` or
``--mcp``: given any of them it prints its usage and exits 1, so every spawned
Kilo agent failed before it ran.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.kilo import KiloAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


def _spawn(tmp_path: Path, prompt: str = "fix the bug", **kwargs: object) -> list[str]:
    adapter = KiloAdapter()
    with patch("bernstein.adapters.kilo.subprocess.Popen", return_value=make_popen_mock(pid=990)) as popen:
        adapter.spawn(
            prompt=prompt,
            workdir=tmp_path,
            model_config=ModelConfig(model="anthropic/claude-sonnet-4-5", effort="high"),
            session_id="kilo-s1",
            **kwargs,  # type: ignore[arg-type]
        )
    return inner_cmd(popen.call_args.args[0])


def test_spawn_uses_the_documented_run_surface(tmp_path: Path) -> None:
    assert _spawn(tmp_path) == [
        "kilo",
        "run",
        "--model",
        "anthropic/claude-sonnet-4-5",
        "--auto",
        "--",
        "fix the bug",
    ]


def test_spawn_never_passes_flags_kilo_run_rejects(tmp_path: Path) -> None:
    inner = _spawn(tmp_path, mcp_config={"mcpServers": {"bernstein": {"command": "bernstein-mcp"}}})
    for rejected in ("--prompt", "--yes", "--mcp"):
        assert rejected not in inner


def test_a_prompt_that_starts_with_a_dash_stays_the_message(tmp_path: Path) -> None:
    inner = _spawn(tmp_path, prompt="--help me refactor")
    assert inner[inner.index("--") + 1] == "--help me refactor"


def test_mcp_servers_that_cannot_be_passed_are_reported(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="bernstein.adapters.kilo"):
        _spawn(tmp_path, mcp_config={"mcpServers": {"a": {}, "b": {}}})
    assert "kilo mcp" in caplog.text
    assert "2 server(s)" in caplog.text


def test_missing_cli_names_the_real_package(tmp_path: Path) -> None:
    adapter = KiloAdapter()
    with (
        patch("bernstein.adapters.kilo.subprocess.Popen", side_effect=FileNotFoundError("no kilo")),
        pytest.raises(RuntimeError, match=r"@kilocode/cli"),
    ):
        adapter.spawn(
            prompt="hi",
            workdir=tmp_path,
            model_config=ModelConfig(model="anthropic/claude-sonnet-4-5", effort="high"),
            session_id="kilo-missing",
        )
