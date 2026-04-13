"""Tests for bernstein.core.agency_loader."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bernstein.core.agency_loader import (
    AgencyAgent,
    _map_division,
    load_agency_catalog,
    parse_agency_agent,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_AGENT_YAML = """\
name: code-optimizer
description: Optimizes Python code for performance
division: Engineering
system_prompt: |
  You are a code optimization specialist.
  Focus on performance improvements.
"""

MINIMAL_AGENT_YAML = """\
name: helper
"""

AGENT_WITH_PROMPT_KEY = """\
name: reviewer-bot
description: Reviews pull requests
division: Code Review
prompt: |
  You review pull requests carefully.
"""


@pytest.fixture()
def catalog_dir(tmp_path: Path) -> Path:
    """Return a temporary directory for agency YAML files."""
    d = tmp_path / "agency"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# _map_division
# ---------------------------------------------------------------------------


class TestMapDivision:
    """Tests for division-to-role mapping."""

    def test_engineering_maps_to_backend(self) -> None:
        assert _map_division("Engineering") == "backend"

    def test_software_engineering_maps_to_backend(self) -> None:
        assert _map_division("Software Engineering") == "backend"

    def test_frontend_engineering(self) -> None:
        assert _map_division("Frontend Engineering") == "frontend"

    def test_qa_testing(self) -> None:
        assert _map_division("QA Testing") == "qa"

    def test_security(self) -> None:
        assert _map_division("Security") == "security"

    def test_cybersecurity(self) -> None:
        assert _map_division("Cybersecurity") == "security"

    def test_devops(self) -> None:
        assert _map_division("DevOps") == "devops"

    def test_infrastructure(self) -> None:
        assert _map_division("Infrastructure") == "devops"

    def test_documentation(self) -> None:
        assert _map_division("Documentation") == "docs"

    def test_machine_learning(self) -> None:
        assert _map_division("Machine Learning") == "ml-engineer"

    def test_management(self) -> None:
        assert _map_division("Management") == "manager"

    def test_unknown_division_passes_through(self) -> None:
        assert _map_division("Exotic Division") == "exotic_division"

    def test_case_insensitive(self) -> None:
        assert _map_division("ENGINEERING") == "backend"

    def test_hyphenated_division(self) -> None:
        assert _map_division("qa-testing") == "qa"


# ---------------------------------------------------------------------------
# parse_agency_agent — valid inputs
# ---------------------------------------------------------------------------


class TestParseAgencyAgentValid:
    """Tests for valid Agency persona parsing."""

    def test_full_agent(self, tmp_path: Path) -> None:
        f = tmp_path / "agent.yaml"
        f.write_text(VALID_AGENT_YAML)
        agent = parse_agency_agent(f)
        assert agent.name == "code-optimizer"
        assert agent.description == "Optimizes Python code for performance"
        assert agent.division == "Engineering"
        assert agent.role == "backend"
        assert "code optimization specialist" in agent.prompt_body

    def test_minimal_agent(self, tmp_path: Path) -> None:
        f = tmp_path / "agent.yaml"
        f.write_text(MINIMAL_AGENT_YAML)
        agent = parse_agency_agent(f)
        assert agent.name == "helper"
        assert agent.description == ""
        assert agent.division == "general"
        assert agent.role == "general"
        assert agent.prompt_body == ""

    def test_prompt_key_alternative(self, tmp_path: Path) -> None:
        f = tmp_path / "agent.yaml"
        f.write_text(AGENT_WITH_PROMPT_KEY)
        agent = parse_agency_agent(f)
        assert agent.name == "reviewer-bot"
        assert "pull requests" in agent.prompt_body
        assert agent.role == "reviewer"

    def test_frozen_dataclass(self, tmp_path: Path) -> None:
        f = tmp_path / "agent.yaml"
        f.write_text(VALID_AGENT_YAML)
        agent = parse_agency_agent(f)
        with pytest.raises(AttributeError):
            agent.name = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# parse_agency_agent — invalid inputs
# ---------------------------------------------------------------------------


class TestParseAgencyAgentInvalid:
    """Tests for Agency persona parsing errors."""

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Cannot read"):
            parse_agency_agent(tmp_path / "nonexistent.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.yaml"
        f.write_text("name: [\ninvalid {{{\n")
        with pytest.raises(ValueError, match="Invalid YAML"):
            parse_agency_agent(f)

    def test_non_mapping_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "list.yaml"
        f.write_text("- item1\n- item2\n")
        with pytest.raises(ValueError, match="YAML mapping"):
            parse_agency_agent(f)

    def test_missing_name(self, tmp_path: Path) -> None:
        f = tmp_path / "noname.yaml"
        f.write_text("description: no name here\n")
        with pytest.raises(ValueError, match="missing 'name'"):
            parse_agency_agent(f)


# ---------------------------------------------------------------------------
# load_agency_catalog
# ---------------------------------------------------------------------------


class TestLoadAgencyCatalog:
    """Tests for catalog directory loading."""

    def test_empty_directory(self, catalog_dir: Path) -> None:
        catalog = load_agency_catalog(catalog_dir)
        assert catalog == {}

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        catalog = load_agency_catalog(tmp_path / "nope")
        assert catalog == {}

    def test_loads_valid_files(self, catalog_dir: Path) -> None:
        (catalog_dir / "alpha.yaml").write_text(VALID_AGENT_YAML)
        (catalog_dir / "beta.yaml").write_text(AGENT_WITH_PROMPT_KEY)
        catalog = load_agency_catalog(catalog_dir)
        assert len(catalog) == 2
        assert "code-optimizer" in catalog
        assert "reviewer-bot" in catalog

    def test_skips_invalid_files(self, catalog_dir: Path) -> None:
        (catalog_dir / "good.yaml").write_text(VALID_AGENT_YAML)
        (catalog_dir / "bad.yaml").write_text("- not a mapping\n")
        catalog = load_agency_catalog(catalog_dir)
        assert len(catalog) == 1
        assert "code-optimizer" in catalog

    def test_skips_non_yaml_files(self, catalog_dir: Path) -> None:
        (catalog_dir / "readme.md").write_text("# Readme\n")
        (catalog_dir / "agent.yaml").write_text(VALID_AGENT_YAML)
        catalog = load_agency_catalog(catalog_dir)
        assert len(catalog) == 1

    def test_yml_extension_supported(self, catalog_dir: Path) -> None:
        (catalog_dir / "agent.yml").write_text(VALID_AGENT_YAML)
        catalog = load_agency_catalog(catalog_dir)
        assert len(catalog) == 1

    def test_catalog_keyed_by_name(self, catalog_dir: Path) -> None:
        (catalog_dir / "file1.yaml").write_text(VALID_AGENT_YAML)
        catalog = load_agency_catalog(catalog_dir)
        agent = catalog["code-optimizer"]
        assert isinstance(agent, AgencyAgent)
        assert agent.name == "code-optimizer"


# ---------------------------------------------------------------------------
# Integration: spawner fallback uses agency catalog
# ---------------------------------------------------------------------------


class TestSpawnerAgencyCatalogIntegration:
    """Tests that the spawner _render_fallback uses agency catalog."""

    def test_fallback_uses_agency_agent_prompt(self, tmp_path: Path) -> None:
        from bernstein.core.spawner import _render_fallback

        catalog = {
            "data-analyst": AgencyAgent(
                name="data-analyst",
                description="Analyzes data",
                division="Data Science",
                role="ml-engineer",
                prompt_body="You are a data analysis expert.",
            ),
        }
        # No template exists for "data-analyst" role
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        result = _render_fallback("data-analyst", templates_dir, catalog)
        assert result == "You are a data analysis expert."

    def test_fallback_matches_by_role(self, tmp_path: Path) -> None:
        from bernstein.core.spawner import _render_fallback

        catalog = {
            "perf-optimizer": AgencyAgent(
                name="perf-optimizer",
                description="Optimizes performance",
                division="Engineering",
                role="backend",
                prompt_body="You optimize backend performance.",
            ),
        }
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        # Look up by role "backend" — should find "perf-optimizer" by role match
        result = _render_fallback("backend", templates_dir, catalog)
        assert result == "You optimize backend performance."

    def test_fallback_prefers_template_over_catalog(self, tmp_path: Path) -> None:
        from bernstein.core.spawner import _render_fallback

        catalog = {
            "backend": AgencyAgent(
                name="backend",
                description="Backend agent",
                division="Engineering",
                role="backend",
                prompt_body="Agency backend prompt.",
            ),
        }
        templates_dir = tmp_path / "templates" / "roles"
        role_dir = templates_dir / "backend"
        role_dir.mkdir(parents=True)
        (role_dir / "system_prompt.md").write_text("Template backend prompt.")

        result = _render_fallback("backend", templates_dir, catalog)
        assert result == "Template backend prompt."

    def test_fallback_without_catalog_returns_generic(self, tmp_path: Path) -> None:
        from bernstein.core.spawner import _render_fallback

        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        result = _render_fallback("unknown-role", templates_dir)
        assert result == "You are a unknown-role specialist."


# ---------------------------------------------------------------------------
# SeedConfig agent_catalog field
# ---------------------------------------------------------------------------


class TestSeedConfigAgentCatalog:
    """Tests for the agent_catalog field in SeedConfig."""

    def test_default_is_none(self) -> None:
        from bernstein.core.seed import SeedConfig

        cfg = SeedConfig(goal="Test")
        assert cfg.agent_catalog is None

    def test_parses_agent_catalog_path(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text('goal: "Test"\nagent_catalog: "./agency_agents"\n')
        cfg = parse_seed(seed_file)
        assert cfg.agent_catalog == "./agency_agents"

    def test_invalid_agent_catalog_type(self, tmp_path: Path) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text('goal: "Test"\nagent_catalog: [1, 2]\n')
        with pytest.raises(SeedError, match="agent_catalog must be a string"):
            parse_seed(seed_file)
