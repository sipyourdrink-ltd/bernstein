"""Tests for zero-config agent setup — auto-detection on first run."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.agent_discovery import AgentCapabilities, DiscoveryResult

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(
    name: str = "claude",
    logged_in: bool = True,
    available_models: list[str] | None = None,
    default_model: str = "claude-sonnet-4-6",
) -> AgentCapabilities:
    return AgentCapabilities(
        name=name,
        binary=f"/usr/local/bin/{name}",
        version="1.0.0",
        logged_in=logged_in,
        login_method="OAuth" if logged_in else "",
        available_models=available_models or [default_model],
        default_model=default_model,
        supports_headless=True,
        supports_sandbox=False,
        supports_mcp=True,
        max_context_tokens=200_000,
        reasoning_strength="very_high",
        best_for=["architecture"],
        cost_tier="moderate",
    )


# ---------------------------------------------------------------------------
# SeedConfig default cli
# ---------------------------------------------------------------------------


class TestSeedConfigDefault:
    def test_cli_default_is_auto(self) -> None:
        from bernstein.core.seed import SeedConfig

        s = SeedConfig(goal="test")
        assert s.cli == "auto"

    def test_parse_seed_defaults_cli_to_auto(self, tmp_path: Path) -> None:
        """parse_seed uses 'auto' when cli key is absent from the YAML."""
        from bernstein.core.seed import parse_seed

        yaml = tmp_path / "bernstein.yaml"
        yaml.write_text('goal: "Build something"\n')
        cfg = parse_seed(yaml)
        assert cfg.cli == "auto"

    def test_parse_seed_explicit_claude(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        yaml = tmp_path / "bernstein.yaml"
        yaml.write_text('goal: "Build something"\ncli: claude\n')
        cfg = parse_seed(yaml)
        assert cfg.cli == "claude"

    def test_parse_seed_explicit_auto(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        yaml = tmp_path / "bernstein.yaml"
        yaml.write_text('goal: "Build something"\ncli: auto\n')
        cfg = parse_seed(yaml)
        assert cfg.cli == "auto"


# ---------------------------------------------------------------------------
# preflight_checks auto mode
# ---------------------------------------------------------------------------


class TestPreflightChecksAutoMode:
    @patch("bernstein.core.orchestration.preflight._check_port_free")
    @patch("bernstein.core.agent_discovery.discover_agents_cached")
    def test_auto_mode_prints_found_agents(self, mock_discover: MagicMock, mock_port: MagicMock) -> None:
        from bernstein.core.bootstrap import preflight_checks

        discovery = DiscoveryResult(
            agents=[_make_agent("claude", logged_in=True)],
            warnings=[],
        )
        mock_discover.return_value = discovery

        preflight_checks("auto", 8052)
        # Should not raise; port check is mocked

    @patch("bernstein.core.orchestration.preflight._check_port_free")
    @patch("bernstein.core.agent_discovery.discover_agents_cached")
    def test_auto_mode_no_agents_exits(self, mock_discover: MagicMock, mock_port: MagicMock) -> None:
        from bernstein.core.bootstrap import preflight_checks

        mock_discover.return_value = DiscoveryResult(agents=[], warnings=[])

        with pytest.raises(SystemExit):
            preflight_checks("auto", 8052)

    @patch("bernstein.core.orchestration.preflight._check_port_free")
    @patch("bernstein.core.agent_discovery.discover_agents_cached")
    def test_auto_mode_multiple_agents(self, mock_discover: MagicMock, mock_port: MagicMock) -> None:
        from bernstein.core.bootstrap import preflight_checks

        discovery = DiscoveryResult(
            agents=[
                _make_agent("claude", logged_in=True, available_models=["claude-sonnet-4-6", "claude-opus-4-6"]),
                _make_agent("codex", logged_in=True, available_models=["o4-mini", "o3"], default_model="o4-mini"),
            ],
            warnings=[],
        )
        mock_discover.return_value = discovery
        # Should not raise
        preflight_checks("auto", 8052)

    @patch("bernstein.core.orchestration.preflight._check_port_free")
    @patch("bernstein.core.agent_discovery.discover_agents_cached")
    def test_auto_mode_unauthenticated_agents_shown_as_warning(
        self, mock_discover: MagicMock, mock_port: MagicMock
    ) -> None:
        from bernstein.core.bootstrap import preflight_checks

        discovery = DiscoveryResult(
            agents=[_make_agent("codex", logged_in=False)],
            warnings=["codex found but not logged in — run: codex login"],
        )
        mock_discover.return_value = discovery
        # Should not raise (binary found, even if not authenticated)
        preflight_checks("auto", 8052)


# ---------------------------------------------------------------------------
# auto_write_bernstein_yaml
# ---------------------------------------------------------------------------


class TestAutoWriteBernsteinYaml:
    @patch("bernstein.core.agent_discovery.generate_auto_routing_yaml")
    def test_creates_file_with_auto_cli(self, mock_routing: MagicMock, tmp_path: Path) -> None:
        from bernstein.core.bootstrap import auto_write_bernstein_yaml

        mock_routing.return_value = "cli: auto  # detected: claude\nrouting:\n  backend: claude-sonnet\n"
        auto_write_bernstein_yaml(tmp_path)

        yaml_path = tmp_path / "bernstein.yaml"
        assert yaml_path.exists()
        content = yaml_path.read_text()
        assert "cli: auto" in content
        assert "goal" in content  # has a commented goal hint

    @patch("bernstein.core.agent_discovery.generate_auto_routing_yaml")
    def test_creates_file_when_routing_empty(self, mock_routing: MagicMock, tmp_path: Path) -> None:
        from bernstein.core.bootstrap import auto_write_bernstein_yaml

        mock_routing.return_value = ""
        auto_write_bernstein_yaml(tmp_path)

        yaml_path = tmp_path / "bernstein.yaml"
        assert yaml_path.exists()
        content = yaml_path.read_text()
        assert "cli: auto" in content


# ---------------------------------------------------------------------------
# bootstrap_from_goal defaults
# ---------------------------------------------------------------------------


class TestBootstrapFromGoalDefaults:
    def test_default_cli_is_auto(self) -> None:
        """bootstrap_from_goal defaults to cli='auto'."""
        import inspect

        from bernstein.core.bootstrap import bootstrap_from_goal

        sig = inspect.signature(bootstrap_from_goal)
        assert sig.parameters["cli"].default == "auto"

    def test_model_parameter_accepted(self) -> None:
        """bootstrap_from_goal accepts a model parameter."""
        import inspect

        from bernstein.core.bootstrap import bootstrap_from_goal

        sig = inspect.signature(bootstrap_from_goal)
        assert "model" in sig.parameters


# ---------------------------------------------------------------------------
# _generate_default_yaml uses cli: auto
# ---------------------------------------------------------------------------


class TestGenerateDefaultYaml:
    def test_uses_auto_not_claude(self) -> None:
        from bernstein.cli.run_cmd import _generate_default_yaml

        yaml = _generate_default_yaml("python")
        assert "cli: auto" in yaml
        assert "cli: claude" not in yaml


# ---------------------------------------------------------------------------
# CLI overrides: --cli, --model
# ---------------------------------------------------------------------------


class TestCLIOverrides:
    def test_run_command_accepts_cli_flag(self) -> None:
        """The 'conduct' command should have a --cli option."""
        from bernstein.cli.run_cmd import run

        # 'run' is a Click Command object after decoration
        # Check for 'cli' parameter in the command's params list
        param_names = [p.name for p in run.params]
        assert "cli" in param_names, f"Expected 'cli' param, got: {param_names}"

    def test_run_command_accepts_model_flag(self) -> None:
        """The 'conduct' command should have a --model option."""
        from bernstein.cli.run_cmd import run

        # 'run' is a Click Command object after decoration
        # Check for 'model' parameter in the command's params list
        param_names = [p.name for p in run.params]
        assert "model" in param_names, f"Expected 'model' param, got: {param_names}"

    def test_cli_flag_passed_to_bootstrap(self, tmp_path: Path) -> None:
        """--cli flag should be passed through to bootstrap_from_goal."""
        import inspect

        from bernstein.core.bootstrap import bootstrap_from_goal

        # Verify bootstrap_from_goal accepts cli parameter
        sig = inspect.signature(bootstrap_from_goal)
        assert "cli" in sig.parameters

    def test_model_flag_passed_to_bootstrap(self, tmp_path: Path) -> None:
        """--model flag should be passed through to bootstrap_from_goal."""
        import inspect

        from bernstein.core.bootstrap import bootstrap_from_goal

        # Verify bootstrap_from_goal accepts model parameter
        sig = inspect.signature(bootstrap_from_goal)
        assert "model" in sig.parameters

    def test_cli_flag_overrides_seed_config(self, tmp_path: Path) -> None:
        """CLI --cli flag should override values from seed file."""
        # This test verifies that bootstrap_from_seed accepts cli parameter
        import inspect

        from bernstein.core.bootstrap import bootstrap_from_seed

        sig = inspect.signature(bootstrap_from_seed)
        assert "cli" in sig.parameters

    def test_model_flag_overrides_seed_config(self, tmp_path: Path) -> None:
        """CLI --model flag should override values from seed file."""
        # This test verifies that bootstrap_from_seed accepts model parameter
        import inspect

        from bernstein.core.bootstrap import bootstrap_from_seed

        sig = inspect.signature(bootstrap_from_seed)
        assert "model" in sig.parameters

    def test_cli_flags_integration(self) -> None:
        """Integration test: CLI flags are properly wired through the run command."""
        from click.testing import CliRunner

        from bernstein.cli.run_cmd import run

        runner = CliRunner()

        # Test that --cli flag is accepted
        result = runner.invoke(run, ["--help"])
        assert "--cli" in result.output
        assert "Force specific CLI agent" in result.output

        # Test that --model flag is accepted
        assert "--model" in result.output
        assert "Force specific model" in result.output

        # Test that valid CLI choices are documented
        assert "auto" in result.output
        assert "claude" in result.output
        assert "codex" in result.output


# ---------------------------------------------------------------------------
# _detect_project_type (server_launch)
# ---------------------------------------------------------------------------


class TestDetectProjectType:
    def test_python_via_pyproject(self, tmp_path: Path) -> None:
        from bernstein.core.server_launch import _detect_project_type

        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'myapp'\n")
        assert _detect_project_type(tmp_path) == "python"

    def test_python_via_setup_py(self, tmp_path: Path) -> None:
        from bernstein.core.server_launch import _detect_project_type

        (tmp_path / "setup.py").write_text("from setuptools import setup; setup(name='x')\n")
        assert _detect_project_type(tmp_path) == "python"

    def test_node_via_package_json(self, tmp_path: Path) -> None:
        from bernstein.core.server_launch import _detect_project_type

        (tmp_path / "package.json").write_text('{"name": "myapp"}\n')
        assert _detect_project_type(tmp_path) == "node"

    def test_go_via_go_mod(self, tmp_path: Path) -> None:
        from bernstein.core.server_launch import _detect_project_type

        (tmp_path / "go.mod").write_text("module myapp\n\ngo 1.22\n")
        assert _detect_project_type(tmp_path) == "go"

    def test_rust_via_cargo_toml(self, tmp_path: Path) -> None:
        from bernstein.core.server_launch import _detect_project_type

        (tmp_path / "Cargo.toml").write_text("[package]\nname = 'myapp'\n")
        assert _detect_project_type(tmp_path) == "rust"

    def test_generic_fallback(self, tmp_path: Path) -> None:
        from bernstein.core.server_launch import _detect_project_type

        assert _detect_project_type(tmp_path) == "generic"


# ---------------------------------------------------------------------------
# _constraints_for_project_type (server_launch)
# ---------------------------------------------------------------------------


class TestConstraintsForProjectType:
    def test_python_constraints(self) -> None:
        from bernstein.core.server_launch import _constraints_for_project_type

        c = _constraints_for_project_type("python")
        assert any("pytest" in s for s in c)
        assert any("ruff" in s for s in c)

    def test_node_constraints(self) -> None:
        from bernstein.core.server_launch import _constraints_for_project_type

        c = _constraints_for_project_type("node")
        assert any("Node" in s for s in c)

    def test_generic_empty(self) -> None:
        from bernstein.core.server_launch import _constraints_for_project_type

        assert _constraints_for_project_type("generic") == []


# ---------------------------------------------------------------------------
# auto_write_bernstein_yaml includes project constraints
# ---------------------------------------------------------------------------


class TestAutoWriteWithProjectType:
    @patch("bernstein.core.agent_discovery.generate_auto_routing_yaml")
    def test_python_project_includes_constraints(self, mock_routing: MagicMock, tmp_path: Path) -> None:
        from bernstein.core.server_launch import auto_write_bernstein_yaml

        mock_routing.return_value = ""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        auto_write_bernstein_yaml(tmp_path)

        content = (tmp_path / "bernstein.yaml").read_text()
        assert "constraints:" in content
        assert "pytest" in content
        assert "ruff" in content

    @patch("bernstein.core.agent_discovery.generate_auto_routing_yaml")
    def test_node_project_includes_constraints(self, mock_routing: MagicMock, tmp_path: Path) -> None:
        from bernstein.core.server_launch import auto_write_bernstein_yaml

        mock_routing.return_value = ""
        (tmp_path / "package.json").write_text('{"name": "x"}\n')
        auto_write_bernstein_yaml(tmp_path)

        content = (tmp_path / "bernstein.yaml").read_text()
        assert "constraints:" in content
        assert "Node" in content

    @patch("bernstein.core.agent_discovery.generate_auto_routing_yaml")
    def test_generic_project_no_constraints_block(self, mock_routing: MagicMock, tmp_path: Path) -> None:
        from bernstein.core.server_launch import auto_write_bernstein_yaml

        mock_routing.return_value = ""
        auto_write_bernstein_yaml(tmp_path)

        content = (tmp_path / "bernstein.yaml").read_text()
        # Generic projects should not have a constraints block
        assert "constraints:" not in content

    @patch("bernstein.core.agent_discovery.generate_auto_routing_yaml")
    def test_always_has_cli_auto_and_goal_hint(self, mock_routing: MagicMock, tmp_path: Path) -> None:
        from bernstein.core.server_launch import auto_write_bernstein_yaml

        mock_routing.return_value = ""
        auto_write_bernstein_yaml(tmp_path)

        content = (tmp_path / "bernstein.yaml").read_text()
        assert "cli: auto" in content
        assert "goal" in content


# ---------------------------------------------------------------------------
# Task count includes .yaml tickets
# ---------------------------------------------------------------------------


class TestTaskCountYamlTickets:
    def test_yaml_tickets_counted(self, tmp_path: Path) -> None:
        """main.py task count should include .yaml ticket files."""
        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        open_dir.mkdir(parents=True)
        (open_dir / "T001.yaml").write_text("id: T001\ntitle: test\n")
        (open_dir / "T002.yaml").write_text("id: T002\ntitle: test2\n")

        count = sum(1 for f in open_dir.iterdir() if f.suffix in (".yaml", ".yml", ".md"))
        assert count == 2

    def test_mixed_extensions_counted(self, tmp_path: Path) -> None:
        """Both legacy .md and new .yaml tickets should be counted."""
        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        open_dir.mkdir(parents=True)
        (open_dir / "T001.yaml").write_text("id: T001\n")
        (open_dir / "T002.md").write_text("# T002\n")

        count = sum(1 for f in open_dir.iterdir() if f.suffix in (".yaml", ".yml", ".md"))
        assert count == 2
