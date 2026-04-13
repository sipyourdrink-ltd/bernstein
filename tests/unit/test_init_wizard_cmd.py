"""Tests for CLI-012: ``bernstein init`` interactive wizard."""

from __future__ import annotations

import tempfile
from pathlib import Path

from bernstein.cli.init_wizard_cmd import (
    detect_project_type,
    generate_yaml,
    init_wizard_cmd,
)
from click.testing import CliRunner

from bernstein.cli.main import cli


class TestInitWizardCmd:
    """Tests for the init-wizard command."""

    def test_init_wizard_command_exists(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init-wizard", "--help"])
        assert result.exit_code == 0
        assert "wizard" in result.output.lower() or "setup" in result.output.lower()

    def test_init_wizard_non_interactive(self) -> None:
        """Non-interactive mode should not prompt."""
        with tempfile.TemporaryDirectory() as td:
            runner = CliRunner()
            result = runner.invoke(init_wizard_cmd, ["--dir", td, "--non-interactive"])
            assert result.exit_code == 0
            assert "Done" in result.output or "done" in result.output.lower()
            # Check files were created
            assert (Path(td) / ".sdd").exists()
            assert (Path(td) / "bernstein.yaml").exists()

    def test_init_wizard_creates_sdd_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runner = CliRunner()
            result = runner.invoke(init_wizard_cmd, ["--dir", td, "--non-interactive"])
            assert result.exit_code == 0
            assert (Path(td) / ".sdd" / "backlog").exists()
            assert (Path(td) / ".sdd" / "runtime").exists()

    def test_init_wizard_yaml_content(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runner = CliRunner()
            result = runner.invoke(init_wizard_cmd, ["--dir", td, "--non-interactive"])
            assert result.exit_code == 0
            content = (Path(td) / "bernstein.yaml").read_text()
            assert "goal:" in content
            assert "orchestrator:" in content

    def testdetect_project_type_python(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "pyproject.toml").write_text("[project]\nname = 'test'\n")
            assert detect_project_type(Path(td)) == "python"

    def testdetect_project_type_node(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "package.json").write_text("{}\n")
            assert detect_project_type(Path(td)) == "node"

    def testdetect_project_type_generic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            assert detect_project_type(Path(td)) == "generic"

    def testgenerate_yaml_basic(self) -> None:
        yaml = generate_yaml(
            goal="Test goal",
            project_type="python",
            max_agents=3,
            budget=5.0,
            adapter="auto",
            approval="auto",
        )
        assert "Test goal" in yaml
        assert "max_agents: 3" in yaml
        assert "budget_usd: 5.00" in yaml

    def testgenerate_yaml_with_adapter(self) -> None:
        yaml = generate_yaml(
            goal="Test",
            project_type="python",
            max_agents=3,
            budget=5.0,
            adapter="claude",
            approval="auto",
        )
        assert "default_adapter: claude" in yaml

    def test_init_wizard_existing_yaml_non_interactive(self) -> None:
        """Non-interactive should overwrite existing yaml."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "bernstein.yaml").write_text("old: content\n")
            runner = CliRunner()
            result = runner.invoke(init_wizard_cmd, ["--dir", td, "--non-interactive"])
            assert result.exit_code == 0
            content = (Path(td) / "bernstein.yaml").read_text()
            assert "goal:" in content
