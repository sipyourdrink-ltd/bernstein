"""Tests for comparing the current project configuration with defaults."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.workspace_cmd import config_group
from bernstein.core.config.config_drift_cmd import diff_against_defaults


@pytest.mark.parametrize("filename", ["bernstein.yaml", "bernstein.yml"])
def test_config_diff_compares_an_empty_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / filename).write_text("{}\n", encoding="utf-8")

    result = CliRunner().invoke(config_group, ["diff"])

    assert result.exit_code == 0, result.output
    expected = diff_against_defaults({})
    summary = f"0 changed, 0 added, {expected.removed_count} removed out of {expected.total_keys} total settings."
    assert summary in result.output


def test_config_diff_reports_a_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(config_group, ["diff"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "No bernstein.yaml found in current directory."


def test_config_diff_skips_a_non_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bernstein.yml").write_text("[one, two]\n", encoding="utf-8")

    result = CliRunner().invoke(config_group, ["diff"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "bernstein.yml must contain a YAML mapping."
