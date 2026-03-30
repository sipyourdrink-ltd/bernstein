"""Tests for `bernstein agents` CLI command group."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_valid_definition(directory: Path, name: str = "test-agent") -> Path:
    """Write a minimal valid agent definition YAML."""
    p = directory / f"{name}.yaml"
    p.write_text(
        textwrap.dedent(f"""\
            name: {name}
            role: backend
            model: sonnet
            version: "1.0"
            description: Test agent
        """)
    )
    return p


def _write_invalid_definition(directory: Path, name: str = "bad-agent") -> Path:
    """Write an agent definition YAML with schema errors."""
    p = directory / f"{name}.yaml"
    p.write_text(
        textwrap.dedent(f"""\
            name: {name}
            role: backend
            model: not-a-real-model
            version: "1.0"
        """)
    )
    return p


def _write_agency_agent(directory: Path, name: str = "agency-dev") -> Path:
    """Write a minimal Agency-style persona YAML."""
    p = directory / f"{name}.yaml"
    p.write_text(
        textwrap.dedent(f"""\
            name: {name}
            description: Agency test agent
            division: Engineering
            system_prompt: You are an engineer.
        """)
    )
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _isolate_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent agent list tests from seeing the real Agency catalog."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "bernstein.agents.agency_provider.AgencyProvider.default_cache_path",
        staticmethod(lambda: tmp_path / "nonexistent_cache"),
    )
    # Ensure Rich tables have enough width to render all columns fully
    monkeypatch.setenv("COLUMNS", "200")


# ---------------------------------------------------------------------------
# agents sync
# ---------------------------------------------------------------------------


def test_agents_sync_no_directory(tmp_path: Path) -> None:
    """sync reports missing directory gracefully."""
    runner = CliRunner()
    result = runner.invoke(cli, ["agents", "sync", "--dir", str(tmp_path / "missing")])
    assert result.exit_code == 0
    assert "does not exist" in result.output


def test_agents_sync_empty_directory(tmp_path: Path) -> None:
    """sync on an empty definitions directory shows 0 definitions loaded."""
    defs = tmp_path / "definitions"
    defs.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["agents", "sync", "--dir", str(defs)])
    assert result.exit_code == 0
    assert "0 agent definition" in result.output


def test_agents_sync_with_valid_definition(tmp_path: Path) -> None:
    """sync loads and reports valid definitions."""
    defs = tmp_path / "definitions"
    defs.mkdir()
    _write_valid_definition(defs, "my-agent")

    runner = CliRunner()
    result = runner.invoke(cli, ["agents", "sync", "--dir", str(defs)])
    assert result.exit_code == 0
    assert "1 agent definition" in result.output
    assert "my-agent" in result.output


def test_agents_sync_complete_message(tmp_path: Path) -> None:
    """sync always prints a completion message."""
    defs = tmp_path / "definitions"
    defs.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["agents", "sync", "--dir", str(defs)])
    assert result.exit_code == 0
    assert "Sync complete" in result.output


# ---------------------------------------------------------------------------
# agents list
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_isolate_catalog")
def test_agents_list_no_agents(tmp_path: Path) -> None:
    """list with no definitions shows helpful message."""
    defs = tmp_path / "definitions"
    defs.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["agents", "list", "--dir", str(defs)])
    assert result.exit_code == 0
    assert "No agents found" in result.output


@pytest.mark.usefixtures("_isolate_catalog")
def test_agents_list_shows_local_agents(tmp_path: Path) -> None:
    """list shows agents loaded from the local definitions directory."""
    defs = tmp_path / "definitions"
    defs.mkdir()
    _write_valid_definition(defs, "alpha-agent")
    _write_valid_definition(defs, "beta-agent")

    runner = CliRunner()
    result = runner.invoke(cli, ["agents", "list", "--dir", str(defs)])
    assert result.exit_code == 0
    assert "alpha-agent" in result.output
    assert "beta-agent" in result.output
    assert "local" in result.output


@pytest.mark.usefixtures("_isolate_catalog")
def test_agents_list_filter_by_source_local(tmp_path: Path) -> None:
    """list --source local only shows local agents."""
    defs = tmp_path / "definitions"
    defs.mkdir()
    _write_valid_definition(defs, "local-only")

    runner = CliRunner()
    result = runner.invoke(cli, ["agents", "list", "--source", "local", "--dir", str(defs)])
    assert result.exit_code == 0
    assert "local-only" in result.output


@pytest.mark.usefixtures("_isolate_catalog")
def test_agents_list_filter_by_source_agency_no_dir(tmp_path: Path) -> None:
    """list --source agency with no agency dir shows no agents."""
    defs = tmp_path / "definitions"
    defs.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["agents", "list", "--source", "agency", "--dir", str(defs)])
    assert result.exit_code == 0
    assert "No agents found" in result.output


@pytest.mark.usefixtures("_isolate_catalog")
def test_agents_list_count_footer(tmp_path: Path) -> None:
    """list shows a total count in the footer."""
    defs = tmp_path / "definitions"
    defs.mkdir()
    _write_valid_definition(defs, "one")
    _write_valid_definition(defs, "two")

    runner = CliRunner()
    result = runner.invoke(cli, ["agents", "list", "--dir", str(defs)])
    assert result.exit_code == 0
    assert "2 agent(s) total" in result.output


# ---------------------------------------------------------------------------
# agents validate
# ---------------------------------------------------------------------------


def test_agents_validate_missing_directory(tmp_path: Path) -> None:
    """validate exits 1 when definitions directory is missing."""
    runner = CliRunner()
    result = runner.invoke(cli, ["agents", "validate", "--dir", str(tmp_path / "missing")])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_agents_validate_empty_directory(tmp_path: Path) -> None:
    """validate passes on an empty (but existing) definitions directory."""
    defs = tmp_path / "definitions"
    defs.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["agents", "validate", "--dir", str(defs)])
    assert result.exit_code == 0
    assert "valid" in result.output.lower()


def test_agents_validate_valid_definitions(tmp_path: Path) -> None:
    """validate passes and reports green checks for valid definitions."""
    defs = tmp_path / "definitions"
    defs.mkdir()
    _write_valid_definition(defs, "good-agent")

    runner = CliRunner()
    result = runner.invoke(cli, ["agents", "validate", "--dir", str(defs)])
    assert result.exit_code == 0
    assert "good-agent.yaml" in result.output
    assert "All catalogs valid" in result.output


def test_agents_validate_invalid_definition(tmp_path: Path) -> None:
    """validate exits 1 and reports issues for invalid definitions."""
    defs = tmp_path / "definitions"
    defs.mkdir()
    _write_invalid_definition(defs, "bad-agent")

    runner = CliRunner()
    result = runner.invoke(cli, ["agents", "validate", "--dir", str(defs)])
    assert result.exit_code == 1
    assert "bad-agent.yaml" in result.output
    assert "Validation failed" in result.output


def test_agents_validate_mixed_definitions(tmp_path: Path) -> None:
    """validate reports success for good agents and failures for bad ones."""
    defs = tmp_path / "definitions"
    defs.mkdir()
    _write_valid_definition(defs, "good-agent")
    _write_invalid_definition(defs, "bad-agent")

    runner = CliRunner()
    result = runner.invoke(cli, ["agents", "validate", "--dir", str(defs)])
    assert result.exit_code == 1
    assert "good-agent.yaml" in result.output
    assert "bad-agent.yaml" in result.output
    assert "1 issue" in result.output
