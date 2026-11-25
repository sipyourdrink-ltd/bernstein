"""CLI tests for the plan template output path."""

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein import _BUNDLED_TEMPLATES_DIR
from bernstein.cli.main import cli


def test_templates_use_requires_a_file_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    target = Path("nested") / "rest-api.yaml"
    target.parent.mkdir()
    target.write_text("existing plan", encoding="utf-8")
    runner = CliRunner()

    rejected = runner.invoke(cli, ["templates", "use", "rest-api", str(target.parent)], input="y\n")

    assert rejected.exit_code == 2, rejected.output
    assert "nested" in rejected.output
    assert "is a directory" in rejected.output
    assert "Overwrite?" not in rejected.output
    assert target.read_text(encoding="utf-8") == "existing plan"

    written = runner.invoke(cli, ["templates", "use", "rest-api", str(target)], input="y\n")

    assert written.exit_code == 0, written.output
    assert f"File already exists: {target}" in written.output
    assert f"Created {target}" in written.output
    assert f"bernstein run {target}" in written.output
    assert target.read_bytes() == (_BUNDLED_TEMPLATES_DIR / "plans" / "rest-api.yaml").read_bytes()


def test_templates_use_displays_literal_output_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    target = "[bold] release.yaml"
    Path(target).write_text("previous", encoding="utf-8")

    result = CliRunner().invoke(cli, ["templates", "use", "rest-api", target], input="y\n", env={"COLUMNS": "32"})

    assert result.exit_code == 0, result.output
    assert f"File already exists: {target}" in result.output
    assert f"Created {target}" in result.output
    quoted = f'"{target}"' if os.name == "nt" else f"'{target}'"
    assert f"bernstein run {quoted}" in result.output


def test_templates_use_rejects_directory_at_default_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory at the computed default plans/<name>.yaml is rejected before the prompt."""
    monkeypatch.chdir(tmp_path)
    dest_dir = Path("plans") / "rest-api.yaml"
    dest_dir.mkdir(parents=True)
    child = dest_dir / "rest-api.yaml"
    child.write_text("existing plan", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(cli, ["templates", "use", "rest-api"], input="y\n")

    assert result.exit_code == 2, result.output
    assert str(dest_dir) in result.output
    assert "is a directory" in result.output
    assert "File already exists:" not in result.output
    assert "Overwrite?" not in result.output
    assert child.read_text(encoding="utf-8") == "existing plan"
