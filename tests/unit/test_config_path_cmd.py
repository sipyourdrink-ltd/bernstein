"""Tests for CLI-009: config path display."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.cli.config_path_cmd import (
    config_path_cmd,
    resolve_config_path,
    resolve_sdd_config_path,
)
from click.testing import CliRunner

from bernstein.cli.main import cli

if TYPE_CHECKING:
    import pytest


class TestConfigPathCmd:
    """Tests for the config-path command."""

    def test_config_path_command_exists(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["config-path", "--help"])
        assert result.exit_code == 0
        assert "bernstein.yaml" in result.output.lower() or "config" in result.output.lower()

    def test_config_path_shows_output(self) -> None:
        runner = CliRunner()
        result = runner.invoke(config_path_cmd, [])
        assert result.exit_code == 0
        assert "Config" in result.output or "config" in result.output.lower()

    def test_config_path_json_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seed = tmp_path / "bernstein.yaml"
        seed.write_text("name: local plan\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("BERNSTEIN_SEED_PATH", raising=False)
        runner = CliRunner()
        result = runner.invoke(cli, ["config-path", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {
            "config_file": str(seed.resolve()),
            "sdd_config": None,
            "cwd": str(tmp_path.resolve()),
        }

    def test_resolve_config_path_no_file(self) -> None:
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as td:
                os.chdir(td)
                assert resolve_config_path() is None
        finally:
            os.chdir(old_cwd)

    def test_resolve_config_path_with_file(self) -> None:
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as td:
                os.chdir(td)
                (Path(td) / "bernstein.yaml").write_text("goal: test\n")
                result = resolve_config_path()
                assert result is not None
                assert result.name == "bernstein.yaml"
        finally:
            os.chdir(old_cwd)

    def test_resolve_sdd_config_path_no_file(self) -> None:
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as td:
                os.chdir(td)
                assert resolve_sdd_config_path() is None
        finally:
            os.chdir(old_cwd)

    def test_config_path_not_found_message(self) -> None:
        """When no config exists, show helpful message."""
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as td:
                os.chdir(td)
                runner = CliRunner()
                result = runner.invoke(config_path_cmd, [])
                assert result.exit_code == 0
                assert "not found" in result.output.lower() or "init" in result.output.lower()
        finally:
            os.chdir(old_cwd)
