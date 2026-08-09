"""Unit tests for #3145: Moving bernstein listen to optional voice extra."""

from __future__ import annotations

import tomllib
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.main import cli


def test_voice_optional_dependency_extra_defined() -> None:
    pyproject_path = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    extras = data.get("project", {}).get("optional-dependencies", {})
    assert "voice" in extras, "voice optional dependency extra missing from pyproject.toml"
    voice_deps = extras["voice"]
    assert any("faster-whisper" in dep for dep in voice_deps)
    assert any("sounddevice" in dep for dep in voice_deps)
    assert any("numpy" in dep for dep in voice_deps)


def test_listen_without_extra_gives_informative_error() -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["listen"])
    assert res.exit_code != 0
    assert "pip install 'bernstein[voice]'" in res.output or "pip install 'bernstein[voice]'" in res.stderr
