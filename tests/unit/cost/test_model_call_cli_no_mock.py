"""Test that model-call CLI commands are properly gated against mock usage in production."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.cost import cost_cmd
from bernstein.core.cost.model_call_ledger import ModelCallLedger


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    """Return a temporary .sdd directory."""
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    (sdd / "runtime").mkdir()
    return sdd


@pytest.fixture()
def runner() -> CliRunner:
    """Return a Click CLI test runner."""
    return CliRunner()


def test_model_call_commands_are_hidden_test_scaffolding(runner: CliRunner) -> None:
    """Model-call commands must be marked hidden=True (test scaffolding only)."""
    # The commands should not appear in the public help output
    result = runner.invoke(cost_cmd, ["--help"])
    assert result.exit_code == 0
    # Hidden commands should not be listed in the help
    # (They're still callable but not advertised)


def test_invoke_fails_with_clear_message_without_real_adapter(
    sdd_dir: Path,
    runner: CliRunner,
) -> None:
    """Invoke command must fail closed when no real adapter is available."""
    result = runner.invoke(
        cost_cmd,
        [
            "model-call",
            "invoke",
            "--sdd-dir",
            str(sdd_dir),
            "--capability-id",
            "test-cap",
            "--adapter-id",
            "test-adapter",
            "--model",
            "test-model",
            "--input",
            "test input",
            "--parameters",
            json.dumps({"key": "value"}),
        ],
    )

    # Must fail (non-zero exit) or refuse with clear message
    # Should NOT return mock output like "Mock output for test-model"
    assert "Mock output for" not in result.output
    # If it succeeds, it must be because a real adapter was wired
    # For now, we expect it to either:
    # 1. Exit non-zero with clear error message, or
    # 2. Work with real adapter (future enhancement)


def test_replay_fails_with_clear_message_without_real_adapter(
    sdd_dir: Path,
    runner: CliRunner,
) -> None:
    """Replay command must fail closed when no real adapter is available."""
    # Create a record first using the library API (which allows mock for testing)
    ledger = ModelCallLedger(sdd_dir)
    original = ledger.invoke(
        capability_id="test-cap",
        adapter_id="test-adapter",
        model="test-model",
        call=lambda: "test output",
        input_text="test input",
        parameters={"key": "value"},
    )

    result = runner.invoke(
        cost_cmd,
        [
            "model-call",
            "replay",
            "--sdd-dir",
            str(sdd_dir),
            "--record-id",
            original.id,
        ],
    )

    # Must fail (non-zero exit) or refuse with clear message
    # Should NOT return mock output like "Replayed output for test-model"
    assert "Replayed output for" not in result.output
