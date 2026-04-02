"""Tests for config provenance display in workspace CLI commands."""

from __future__ import annotations

from click.testing import CliRunner

from bernstein.cli.main import cli


def test_config_get_prints_resolution_chain(tmp_path, monkeypatch) -> None:
    """`bernstein config get` should render the provenance chain."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BERNSTEIN_CLI", "qwen")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    sdd_config = project_dir / ".sdd" / "config.yaml"
    sdd_config.parent.mkdir(parents=True)
    sdd_config.write_text("cli: gemini\n", encoding="utf-8")
    home_config = tmp_path / "home" / ".bernstein" / "config.yaml"
    home_config.parent.mkdir(parents=True)
    home_config.write_text("cli: codex\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["config", "get", "cli", "--project-dir", str(project_dir)])

    assert result.exit_code == 0
    assert "source: session" in result.output
    assert "resolution: session -> project -> global -> default" in result.output
