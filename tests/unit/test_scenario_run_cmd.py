"""Tests for the scenario run CLI command."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli


def test_scenario_run_command_works_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that bernstein scenario run works from a fresh init workspace."""
    # Change to temp directory
    monkeypatch.chdir(tmp_path)

    # Initialize a workspace
    runner = CliRunner()
    result = runner.invoke(cli, ["init"])
    assert result.exit_code == 0, f"init failed: {result.output}"

    # Create a simple scenario in the workspace scenarios directory
    scenarios_dir = tmp_path / ".bernstein" / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    (scenarios_dir / "test-scenario.yaml").write_text(
        """\
id: test-scenario
name: Test Scenario
description: A simple test scenario
tasks:
  - title: Create a test file
    description: Create a simple text file to verify the scenario ran
    role: backend
""",
        encoding="utf-8",
    )

    # Mock the agent spawning to avoid actually calling external agents
    with patch("bernstein.cli.helpers.server_post") as mock_post:
        # Mock successful post that returns a task ID
        def mock_post_impl(*args, **kwargs):
            return {"id": "task-123"}

        mock_post.side_effect = mock_post_impl

        # Run the scenario
        result = runner.invoke(cli, ["scenario", "run", "test-scenario"])
        assert result.exit_code == 0, f"scenario run failed: {result.output}"

        # Verify that the scenario ran by checking for expected output
        assert "Successfully spawned 1 tasks for scenario 'test-scenario'" in result.output
        assert "task-123" in result.output


def test_scenario_run_command_handles_missing_scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that bernstein scenario run gives proper error for missing scenario."""
    # Change to temp directory
    monkeypatch.chdir(tmp_path)

    # Initialize a workspace
    runner = CliRunner()
    result = runner.invoke(cli, ["init"])
    assert result.exit_code == 0, f"init failed: {result.output}"

    # Try to run a non-existent scenario
    result = runner.invoke(cli, ["scenario", "run", "non-existent-scenario"])
    assert result.exit_code != 0
    assert "unknown scenario" in result.output.lower()


def test_scenario_run_command_works_with_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that bernstein scenario run works with context injection."""
    # Change to temp directory
    monkeypatch.chdir(tmp_path)

    # Initialize a workspace
    runner = CliRunner()
    result = runner.invoke(cli, ["init"])
    assert result.exit_code == 0, f"init failed: {result.output}"

    # Create a scenario with multiple tasks
    scenarios_dir = tmp_path / ".bernstein" / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    (scenarios_dir / "multi-task-scenario.yaml").write_text(
        """\
id: multi-task-scenario
name: Multi Task Scenario
description: A scenario with multiple tasks
tasks:
  - title: First task
    description: First task in scenario
    role: backend
  - title: Second task
    description: Second task in scenario
    role: frontend
""",
        encoding="utf-8",
    )

    # Mock the agent spawning
    with patch("bernstein.cli.helpers.server_post") as mock_post:
        # Mock successful post that returns a task ID
        def mock_post_impl(*args, **kwargs):
            return {"id": "task-123"}

        mock_post.side_effect = mock_post_impl

        # Run the scenario with context
        result = runner.invoke(cli, [
            "scenario", "run", "multi-task-scenario",
            "--context", "Test context from trigger",
            "--pr-number", "42",
            "--branch", "feature/test"
        ])
        assert result.exit_code == 0, f"scenario run with context failed: {result.output}"

        # Verify that tasks were spawned
        assert "Successfully spawned 2 tasks for scenario 'multi-task-scenario'" in result.output
        assert "task-123" in result.output