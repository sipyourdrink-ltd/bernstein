"""Tests for CLI-009: config path display."""

from __future__ import annotations

import os
import tempfile

from bernstein.cli.config_path_cmd import (
    config_path_cmd,
    resolve_config_path,
    resolve_sdd_config_path,
)
from click.testing import CliRunner

from bernstein.cli.main import cli


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

    def test_config_path_json_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(config_path_cmd, ["--json"])
        assert result.exit_code == 0
        # Should output JSON
        assert "cwd" in result.output

    def test_resolve_config_path_no_file(self) -> None:
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as td:
                os.chdir(td)
                assert resolve_config_path() is None
        finally:
            os.chdir(old_cwd)

    def test_resolve_config_path_with_file(self) -> None:
        from pathlib import Path

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
