"""Tests for ModelCallLedger CLI commands - verify fail-closed behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.cost import cost_cmd
from bernstein.core.cost.model_call_ledger import ModelCallLedger

# --- Fixtures ---


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    """Return a temporary .sdd directory."""
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    (sdd / "runtime").mkdir()
    return sdd


@pytest.fixture()
def ledger(sdd_dir: Path) -> ModelCallLedger:
    """Return a fresh ledger instance."""
    return ModelCallLedger(sdd_dir)


@pytest.fixture()
def runner() -> CliRunner:
    """Return a Click CLI test runner."""
    return CliRunner()


# --- Tests ---


def test_cli_reuse_identical_flag_short_circuits(
    ledger: ModelCallLedger,
    sdd_dir: Path,
    runner: CliRunner,
) -> None:
    """The CLI commands fail closed when no real adapter is available."""
    # Arrange: write one succeeded record to the ledger using library API
    call_count = 0

    def mock_call() -> str:
        nonlocal call_count
        call_count += 1
        return "output text"

    first = ledger.invoke(
        capability_id="test-cap",
        adapter_id="test-adapter",
        model="test-model",
        call=mock_call,
        input_text="input text",
        parameters={"key": "value"},
    )
    assert call_count == 1
    assert first.status == "succeeded"

    # Act: invoke via CLI - should fail closed
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
            "input text",
            "--parameters",
            json.dumps({"key": "value"}),
            "--reuse-identical",
        ],
    )

    # Assert: CLI fails closed with clear error message
    assert result.exit_code == 1
    assert "Real adapter invocation requires a running agent session" in result.output
    assert "test scaffolding" in result.output.lower()
    assert "ModelCallLedger library API" in result.output


def test_cli_replay_record_id(
    ledger: ModelCallLedger,
    sdd_dir: Path,
    runner: CliRunner,
) -> None:
    """The replay subcommand fails closed when no real adapter is available."""
    # Arrange: write one record using library API
    original = ledger.invoke(
        capability_id="test-cap",
        adapter_id="test-adapter",
        model="test-model",
        call=lambda: "first output",
        input_text="input text",
        parameters={"key": "value"},
    )
    assert original.output_text == "first output"

    # Act: replay via CLI - should fail closed
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

    # Assert: CLI fails closed with clear error message
    assert result.exit_code == 1
    assert "Real adapter invocation requires a running agent session" in result.output
    assert "test scaffolding" in result.output.lower()
    assert "ModelCallLedger library API" in result.output

    # Verify the original record is untouched (no replay happened)
    fresh_ledger = ModelCallLedger(sdd_dir)
    records = fresh_ledger.list_records(limit=10)
    assert len(records) == 1  # Only the original, no replay
