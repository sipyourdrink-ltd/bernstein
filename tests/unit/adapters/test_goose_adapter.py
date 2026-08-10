"""Unit tests for the Goose CLI adapter argv contract.

Goose ``run`` accepts the prompt either as ``-i/--instructions <FILE>`` or as
``-t/--text <TEXT>``.  It has never accepted a singular ``--instruction``
carrying prompt text; passing one makes clap abort with exit code 2 before the
agent starts, so a worker spawned that way dies without doing any work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from bernstein.core.models import ModelConfig

from bernstein.adapters.goose import GooseAdapter

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contract" / "contracts" / "goose.yaml"


def _inner_argv(cmd: list[str]) -> list[str]:
    """Return the adapter's own argv from a bernstein-worker wrapped command.

    ``build_worker_cmd`` prefixes the worker invocation, which carries its own
    ``--model`` flag, and separates it from the wrapped command with ``--``.
    """
    return cmd[cmd.index("--") + 1 :]


def _spawn_and_capture(tmp_path: Path, **overrides: Any) -> list[str]:
    """Spawn the adapter with Popen patched; return the inner goose argv."""
    kwargs: dict[str, Any] = {
        "prompt": "Refactor the auth module",
        "workdir": tmp_path,
        "model_config": ModelConfig(model="sonnet", effort="normal"),
        "session_id": "backend-goose-task-1",
        "mcp_config": None,
        "task_scope": "medium",
        "budget_multiplier": 1.0,
        "system_addendum": "",
        "timeout_seconds": 0,
    }
    kwargs.update(overrides)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc
        result = GooseAdapter().spawn(**kwargs)
    assert result.pid == 12345
    return _inner_argv(list(mock_popen.call_args[0][0]))


def test_spawn_does_not_pass_singular_instruction_flag(tmp_path: Path) -> None:
    """``--instruction`` is rejected by goose's parser (exit 2); never emit it."""
    argv = _spawn_and_capture(tmp_path)
    assert "--instruction" not in argv


def test_spawn_passes_prompt_via_text_flag(tmp_path: Path) -> None:
    """The prompt rides on ``--text``, immediately followed by the prompt."""
    argv = _spawn_and_capture(tmp_path, prompt="Refactor the auth module")
    assert "--text" in argv
    assert argv[argv.index("--text") + 1] == "Refactor the auth module"


def test_spawn_keeps_run_subcommand_and_model_flag(tmp_path: Path) -> None:
    """``goose run`` and ``--model <id>`` are still part of the invocation."""
    argv = _spawn_and_capture(tmp_path)
    assert argv[0] == "goose"
    assert argv[1] == "run"
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"


def test_contract_fixture_declares_only_flags_goose_accepts() -> None:
    """The drift contract must not require a flag the CLI rejects.

    A fixture that declares ``--instruction`` keeps the nightly probe green
    against a CLI that only offers ``--instructions``, because the required
    token is a prefix substring of the one actually advertised.
    """
    spec = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert "--instruction" not in spec["required_flags"]


@pytest.mark.parametrize("flag", ["--text", "--model"])
def test_contract_fixture_covers_the_flags_the_adapter_spawns(flag: str, tmp_path: Path) -> None:
    """Every flag the adapter always passes is guarded by the drift contract."""
    spec = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert flag in spec["required_flags"]
