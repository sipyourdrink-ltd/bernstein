"""Unit tests for CopilotAdapter."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.copilot import CopilotAdapter
from bernstein.adapters.session_id import derive_session_id
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


def test_spawn_builds_print_mode_command(tmp_path: Path) -> None:
    adapter = CopilotAdapter()
    proc_mock = make_popen_mock(800)

    with patch("bernstein.adapters.copilot.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-5.4", effort="high"),
            session_id="copilot-s1",
        )

    inner = inner_cmd(popen.call_args.args[0])
    assert inner[0] == "copilot"
    # Non-interactive print mode, not the legacy interactive ``-i`` surface.
    assert "-p" in inner
    assert inner[inner.index("-p") + 1] == "fix the bug"
    assert "-s" in inner
    assert "--allow-all-tools" in inner
    assert "--no-ask-user" in inner
    assert "-i" not in inner


def test_model_flag_passthrough(tmp_path: Path) -> None:
    adapter = CopilotAdapter()
    proc_mock = make_popen_mock(801)

    with patch("bernstein.adapters.copilot.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="claude-sonnet-4.5", effort="high"),
            session_id="copilot-s2",
        )

    inner = inner_cmd(popen.call_args.args[0])
    assert "--model" in inner
    assert inner[inner.index("--model") + 1] == "claude-sonnet-4.5"


def test_claude_tier_model_mapped_to_auto(tmp_path: Path) -> None:
    """A Claude tier name reaching the adapter must not become ``--model sonnet``."""
    adapter = CopilotAdapter()
    proc_mock = make_popen_mock(802)

    with patch("bernstein.adapters.copilot.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="max"),
            session_id="copilot-s3",
        )

    inner = inner_cmd(popen.call_args.args[0])
    assert inner[inner.index("--model") + 1] == "auto"
    assert "sonnet" not in inner


def test_spawn_pins_deterministic_session_id(tmp_path: Path) -> None:
    adapter = CopilotAdapter()
    proc_mock = make_popen_mock(803)

    with patch("bernstein.adapters.copilot.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-5.4", effort="high"),
            session_id="copilot-s4",
        )

    inner = inner_cmd(popen.call_args.args[0])
    assert "--session-id" in inner
    assert inner[inner.index("--session-id") + 1] == str(derive_session_id("copilot-s4", "copilot"))


def test_session_id_args_emits_flag_and_derived_id() -> None:
    adapter = CopilotAdapter()
    args = adapter.session_id_args("conv-1")
    assert args[0] == "--session-id"
    assert args[1] == str(derive_session_id("conv-1", "copilot"))


def test_spawn_translates_missing_cli(tmp_path: Path) -> None:
    adapter = CopilotAdapter()
    with (
        patch(
            "bernstein.adapters.copilot.subprocess.Popen",
            side_effect=FileNotFoundError("No such file"),
        ),
        pytest.raises(RuntimeError, match="copilot not found"),
    ):
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-5.4", effort="high"),
            session_id="copilot-missing",
        )


def test_spawn_translates_permission_error(tmp_path: Path) -> None:
    adapter = CopilotAdapter()
    with (
        patch(
            "bernstein.adapters.copilot.subprocess.Popen",
            side_effect=PermissionError("Permission denied"),
        ),
        pytest.raises(RuntimeError, match="[Pp]ermission"),
    ):
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-5.4", effort="high"),
            session_id="copilot-perm",
        )


def test_env_isolation_passes_only_copilot_keys(tmp_path: Path) -> None:
    adapter = CopilotAdapter()
    proc_mock = make_popen_mock(804)

    with (
        patch("bernstein.adapters.copilot.subprocess.Popen", return_value=proc_mock) as popen,
        patch.dict(
            "os.environ",
            {
                "COPILOT_GITHUB_TOKEN": "ghp-test",
                "ANTHROPIC_API_KEY": "ant-secret",
                "DATABASE_URL": "postgres://x",
                "PATH": "/usr/bin",
            },
            clear=True,
        ),
    ):
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-5.4", effort="high"),
            session_id="copilot-env1",
        )

    env = popen.call_args.kwargs.get("env", {})
    assert env.get("COPILOT_GITHUB_TOKEN") == "ghp-test"
    assert "ANTHROPIC_API_KEY" not in env
    assert "DATABASE_URL" not in env
    assert "PATH" in env


def test_name() -> None:
    assert CopilotAdapter().name() == "GitHub Copilot"
