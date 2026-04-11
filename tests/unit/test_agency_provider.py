"""Tests for AgencyProvider — parses msitarzewski/agency-agents format."""

from __future__ import annotations

import asyncio
import textwrap
from typing import TYPE_CHECKING

from bernstein.agents.agency_provider import AgencyProvider
from bernstein.agents.catalog import CatalogAgent

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

# ---------------------------------------------------------------------------
# Sample Agency markdown files
# ---------------------------------------------------------------------------

FULL_AGENT_MD = textwrap.dedent("""\
    ---
    name: Code Reviewer
    description: Expert code reviewer focused on correctness and security.
    color: purple
    emoji: "\U0001f441\ufe0f"
    vibe: Reviews code like a mentor, not a gatekeeper.
    capabilities: [code-review, security-analysis, static-analysis]
    tools: [ruff, mypy, pytest]
    ---

    # Code Reviewer Agent

    You are **Code Reviewer**, an expert who provides thorough code reviews.
""")

MINIMAL_AGENT_MD = textwrap.dedent("""\
    ---
    name: Minimal Agent
    ---

    Just the basics.
""")

NO_FRONTMATTER_MD = "# No frontmatter here\n\nJust a body."

EMPTY_NAME_MD = textwrap.dedent("""\
    ---
    name: ""
    description: No name given
    ---

    Some content.
""")


# ---------------------------------------------------------------------------
# _parse_file
# ---------------------------------------------------------------------------


class TestParseFile:
    def test_parses_full_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "engineering-code-reviewer.md"
        f.write_text(FULL_AGENT_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="engineering")
        assert len(agents) == 1
        agent = agents[0]
        assert agent.name == "Code Reviewer"
        assert agent.description == "Expert code reviewer focused on correctness and security."
        assert "Code Reviewer" in agent.system_prompt

    def test_id_is_slugified_name_with_prefix(self, tmp_path: Path) -> None:
        f = tmp_path / "engineering-code-reviewer.md"
        f.write_text(FULL_AGENT_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="engineering")
        assert agents[0].id == "agency:code-reviewer"

    def test_source_is_agency(self, tmp_path: Path) -> None:
        f = tmp_path / "engineering-code-reviewer.md"
        f.write_text(FULL_AGENT_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="engineering")
        assert agents[0].source == "agency"

    def test_code_reviewer_inferred_as_reviewer(self, tmp_path: Path) -> None:
        """Smart role inference overrides division-based 'backend' for Code Reviewer."""
        f = tmp_path / "engineering-code-reviewer.md"
        f.write_text(FULL_AGENT_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="engineering")
        assert agents[0].role == "reviewer"

    def test_design_ui_specialist_inferred_as_frontend(self, tmp_path: Path) -> None:
        """Smart inference overrides design division's 'architect' for UI agents."""
        f = tmp_path / "design-ui-specialist.md"
        f.write_text(MINIMAL_AGENT_MD.replace("Minimal Agent", "UI Specialist"), encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="design")
        assert agents[0].role == "frontend"

    def test_design_division_fallback_to_architect(self, tmp_path: Path) -> None:
        """Agents in design division with no strong signal keep 'architect' role."""
        f = tmp_path / "design-generic.md"
        f.write_text(MINIMAL_AGENT_MD.replace("Minimal Agent", "Design Lead"), encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="design")
        assert agents[0].role == "architect"

    def test_engineering_backend_stays_backend(self, tmp_path: Path) -> None:
        """Agent with 'backend' signals in engineering division keeps backend role."""
        backend_md = (
            FULL_AGENT_MD.replace("Code Reviewer", "Backend API Builder")
            .replace(
                "Expert code reviewer focused on correctness and security.",
                "Backend API developer specializing in REST and microservices.",
            )
            .replace(
                "capabilities: [code-review, security-analysis, static-analysis]",
                "capabilities: [api-design, rest, database]",
            )
        )
        f = tmp_path / "engineering-backend-builder.md"
        f.write_text(backend_md, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="engineering")
        assert agents[0].role == "backend"

    def test_unknown_division_kept_as_is(self, tmp_path: Path) -> None:
        f = tmp_path / "xr-spatial-agent.md"
        f.write_text(MINIMAL_AGENT_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="xr")
        assert agents[0].role == "xr"

    def test_tools_extracted_from_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "engineering-code-reviewer.md"
        f.write_text(FULL_AGENT_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="engineering")
        assert agents[0].tools == ["ruff", "mypy", "pytest"]

    def test_tools_empty_when_not_in_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "general-minimal.md"
        f.write_text(MINIMAL_AGENT_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="general")
        assert agents[0].tools == []

    def test_capabilities_extracted_from_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "engineering-code-reviewer.md"
        f.write_text(FULL_AGENT_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="engineering")
        assert agents[0].capabilities == ["code-review", "security-analysis", "static-analysis"]

    def test_capabilities_empty_when_not_in_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "general-minimal.md"
        f.write_text(MINIMAL_AGENT_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="general")
        assert agents[0].capabilities == []

    def test_parses_minimal_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "general-minimal.md"
        f.write_text(MINIMAL_AGENT_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="general")
        assert len(agents) == 1
        assert agents[0].name == "Minimal Agent"
        assert agents[0].description == ""

    def test_skips_file_without_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.md"
        f.write_text(NO_FRONTMATTER_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="engineering")
        assert agents == []

    def test_skips_file_with_empty_name(self, tmp_path: Path) -> None:
        f = tmp_path / "empty-name.md"
        f.write_text(EMPTY_NAME_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="engineering")
        assert agents == []

    def test_returns_catalog_agent_instances(self, tmp_path: Path) -> None:
        f = tmp_path / "engineering-code-reviewer.md"
        f.write_text(FULL_AGENT_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="engineering")
        assert all(isinstance(a, CatalogAgent) for a in agents)


# ---------------------------------------------------------------------------
# provider_id / is_available
# ---------------------------------------------------------------------------


class TestProviderMeta:
    def test_provider_id(self, tmp_path: Path) -> None:
        provider = AgencyProvider(local_path=tmp_path)
        assert provider.provider_id() == "agency"

    def test_is_available_when_dir_exists(self, tmp_path: Path) -> None:
        provider = AgencyProvider(local_path=tmp_path)
        assert provider.is_available() is True

    def test_is_not_available_when_dir_missing(self, tmp_path: Path) -> None:
        provider = AgencyProvider(local_path=tmp_path / "nonexistent")
        assert provider.is_available() is False


# ---------------------------------------------------------------------------
# fetch_agents
# ---------------------------------------------------------------------------


class TestFetchAgents:
    def test_returns_empty_list_for_empty_dir(self, tmp_path: Path) -> None:
        provider = AgencyProvider(local_path=tmp_path)
        agents = asyncio.run(provider.fetch_agents())
        assert agents == []

    def test_scans_subdirectories_for_md_files(self, tmp_path: Path) -> None:
        eng = tmp_path / "engineering"
        eng.mkdir()
        (eng / "engineering-code-reviewer.md").write_text(FULL_AGENT_MD, encoding="utf-8")

        provider = AgencyProvider(local_path=tmp_path)
        agents = asyncio.run(provider.fetch_agents())
        assert len(agents) == 1

    def test_skips_non_md_files(self, tmp_path: Path) -> None:
        eng = tmp_path / "engineering"
        eng.mkdir()
        (eng / "notes.txt").write_text("ignore me", encoding="utf-8")
        (eng / "engineering-code-reviewer.md").write_text(FULL_AGENT_MD, encoding="utf-8")

        provider = AgencyProvider(local_path=tmp_path)
        agents = asyncio.run(provider.fetch_agents())
        assert len(agents) == 1

    def test_loads_agents_from_multiple_divisions(self, tmp_path: Path) -> None:
        (tmp_path / "engineering").mkdir()
        (tmp_path / "design").mkdir()
        (tmp_path / "engineering" / "engineering-code-reviewer.md").write_text(FULL_AGENT_MD, encoding="utf-8")
        (tmp_path / "design" / "design-ui.md").write_text(MINIMAL_AGENT_MD, encoding="utf-8")

        provider = AgencyProvider(local_path=tmp_path)
        agents = asyncio.run(provider.fetch_agents())
        assert len(agents) == 2

    def test_agent_id_uses_agency_prefix(self, tmp_path: Path) -> None:
        eng = tmp_path / "engineering"
        eng.mkdir()
        (eng / "engineering-code-reviewer.md").write_text(FULL_AGENT_MD, encoding="utf-8")

        provider = AgencyProvider(local_path=tmp_path)
        agents = asyncio.run(provider.fetch_agents())
        assert agents[0].id == "agency:code-reviewer"

    def test_division_name_derived_from_directory(self, tmp_path: Path) -> None:
        qa = tmp_path / "qa_testing"
        qa.mkdir()
        (qa / "qa_testing-tester.md").write_text(MINIMAL_AGENT_MD, encoding="utf-8")

        provider = AgencyProvider(local_path=tmp_path)
        agents = asyncio.run(provider.fetch_agents())
        assert agents[0].role == "qa"


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


class TestRefresh:
    def test_refresh_returns_agents_like_fetch(self, tmp_path: Path) -> None:
        eng = tmp_path / "engineering"
        eng.mkdir()
        (eng / "engineering-code-reviewer.md").write_text(FULL_AGENT_MD, encoding="utf-8")

        provider = AgencyProvider(local_path=tmp_path)
        agents = asyncio.run(provider.refresh())
        assert len(agents) == 1

    def test_refresh_picks_up_new_files(self, tmp_path: Path) -> None:
        eng = tmp_path / "engineering"
        eng.mkdir()

        provider = AgencyProvider(local_path=tmp_path)
        assert asyncio.run(provider.fetch_agents()) == []

        (eng / "engineering-code-reviewer.md").write_text(FULL_AGENT_MD, encoding="utf-8")
        agents = asyncio.run(provider.refresh())
        assert len(agents) == 1


# ---------------------------------------------------------------------------
# default_cache_path / sync_catalog
# ---------------------------------------------------------------------------


class TestSyncCatalog:
    def test_default_cache_path_under_home(self) -> None:
        path = AgencyProvider.default_cache_path()
        assert path.name == "agency"
        assert ".bernstein" in path.parts
        assert "catalogs" in path.parts

    def test_sync_catalog_respects_ttl(self, tmp_path: Path) -> None:
        """If a fresh marker file exists, sync_catalog returns (True, ...) without git."""
        target = tmp_path / "agency"
        target.mkdir()
        # Create a git repo stub so the "pull" path would be taken
        (target / ".git").mkdir()
        # Create a fresh marker file
        marker = tmp_path / ".agency.synced"
        marker.touch()
        ok, msg = AgencyProvider.sync_catalog(target=target, force=False)
        assert ok is True
        assert "synced" in msg

    def test_sync_catalog_force_ignores_ttl(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With force=True the TTL is bypassed; subprocess is called."""
        import subprocess as _sp

        target = tmp_path / "agency"
        target.mkdir()
        (target / ".git").mkdir()

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            calls.append(cmd)

            class _Result:
                returncode = 0
                stderr = ""

            return _Result()

        monkeypatch.setattr(_sp, "run", fake_run)

        # Plant a fresh marker
        marker = tmp_path / ".agency.synced"
        marker.touch()

        ok, _msg = AgencyProvider.sync_catalog(target=target, force=True)
        assert ok is True
        assert any("pull" in c for c in calls)


# ---------------------------------------------------------------------------
# Integration: loaded agents feed CatalogRegistry.match()
# ---------------------------------------------------------------------------


class TestCatalogRegistryIntegration:
    """Verify Agency agents flow from provider → CatalogRegistry.match()."""

    def test_fetched_agents_matchable_by_role(self, tmp_path: Path) -> None:
        """Agents fetched from AgencyProvider can be matched via CatalogRegistry."""
        from bernstein.agents.catalog import CatalogRegistry

        eng = tmp_path / "engineering"
        eng.mkdir()
        (eng / "code-reviewer.md").write_text(FULL_AGENT_MD, encoding="utf-8")

        provider = AgencyProvider(local_path=tmp_path)
        agents = asyncio.run(provider.fetch_agents())

        registry = CatalogRegistry()
        for a in agents:
            registry.register_agent(a)

        # Code Reviewer is now inferred as "reviewer" role, not "backend"
        match = registry.match("reviewer", "review code quality")
        assert match is not None
        assert match.name == "Code Reviewer"

    def test_fetched_agents_system_prompt_populated(self, tmp_path: Path) -> None:
        """Matched agent carries the full markdown body as system_prompt."""
        from bernstein.agents.catalog import CatalogRegistry

        eng = tmp_path / "engineering"
        eng.mkdir()
        (eng / "code-reviewer.md").write_text(FULL_AGENT_MD, encoding="utf-8")

        provider = AgencyProvider(local_path=tmp_path)
        agents = asyncio.run(provider.fetch_agents())

        registry = CatalogRegistry()
        for a in agents:
            registry.register_agent(a)

        # Code Reviewer is now inferred as "reviewer" role
        match = registry.match("reviewer", "review code")
        assert match is not None
        assert "Code Reviewer" in match.system_prompt

    def test_fetched_agents_capabilities_drive_matching(self, tmp_path: Path) -> None:
        """Capability keywords in the task description break ties between agents."""
        from bernstein.agents.catalog import CatalogRegistry

        eng = tmp_path / "engineering"
        eng.mkdir()

        # Two backend agents with different capabilities
        api_md = (
            FULL_AGENT_MD.replace(
                "capabilities: [code-review, security-analysis, static-analysis]",
                "capabilities: [api-design, rest, microservice]",
            )
            .replace("name: Code Reviewer", "name: API Builder")
            .replace(
                "Expert code reviewer focused on correctness and security.",
                "Backend API developer for REST microservices.",
            )
        )
        (eng / "api-builder.md").write_text(api_md, encoding="utf-8")

        db_md = (
            FULL_AGENT_MD.replace(
                "capabilities: [code-review, security-analysis, static-analysis]",
                "capabilities: [database, sql, postgres]",
            )
            .replace("name: Code Reviewer", "name: DB Specialist")
            .replace(
                "Expert code reviewer focused on correctness and security.",
                "Backend database developer and optimizer.",
            )
        )
        (eng / "db-specialist.md").write_text(db_md, encoding="utf-8")

        provider = AgencyProvider(local_path=tmp_path)
        agents = asyncio.run(provider.fetch_agents())

        registry = CatalogRegistry()
        for a in agents:
            registry.register_agent(a)

        match = registry.match("backend", "design REST API for microservice architecture")
        assert match is not None
        assert match.name == "API Builder"

    def test_fetched_agents_tools_in_catalog_agent(self, tmp_path: Path) -> None:
        """Tools parsed from frontmatter are accessible on the CatalogAgent."""
        from bernstein.agents.catalog import CatalogRegistry

        eng = tmp_path / "engineering"
        eng.mkdir()
        (eng / "code-reviewer.md").write_text(FULL_AGENT_MD, encoding="utf-8")

        provider = AgencyProvider(local_path=tmp_path)
        agents = asyncio.run(provider.fetch_agents())

        registry = CatalogRegistry()
        for a in agents:
            registry.register_agent(a)

        match = registry.match("reviewer", "review code")
        assert match is not None
        assert match.tools == ["ruff", "mypy", "pytest"]


# ---------------------------------------------------------------------------
# model_preferences field handling
# ---------------------------------------------------------------------------


MODEL_PREFS_AGENT_MD = textwrap.dedent("""\
    ---
    name: Smart Planner
    description: Plans tasks with model preferences.
    capabilities: [planning, decomposition]
    tools: [git]
    model_preferences:
      preferred: claude-3-opus
      fallback: claude-3-sonnet
    ---

    # Smart Planner

    You are Smart Planner, an expert task decomposer.
""")


class TestModelPreferencesField:
    """model_preferences frontmatter field is gracefully ignored — not required by Bernstein."""

    def test_model_preferences_field_does_not_cause_error(self, tmp_path: Path) -> None:
        """Agents with model_preferences in frontmatter are parsed without error."""
        f = tmp_path / "smart-planner.md"
        f.write_text(MODEL_PREFS_AGENT_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="engineering")
        assert len(agents) == 1
        assert agents[0].name == "Smart Planner"

    def test_capabilities_and_tools_extracted_alongside_model_preferences(self, tmp_path: Path) -> None:
        """Other fields are still parsed correctly when model_preferences is present."""
        f = tmp_path / "smart-planner.md"
        f.write_text(MODEL_PREFS_AGENT_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="engineering")
        assert agents[0].capabilities == ["planning", "decomposition"]
        assert agents[0].tools == ["git"]

    def test_system_prompt_populated_when_model_preferences_present(self, tmp_path: Path) -> None:
        """System prompt body is extracted correctly even with model_preferences in frontmatter."""
        f = tmp_path / "smart-planner.md"
        f.write_text(MODEL_PREFS_AGENT_MD, encoding="utf-8")
        agents = AgencyProvider._parse_file(f, division="engineering")
        assert "Smart Planner" in agents[0].system_prompt
        assert "expert task decomposer" in agents[0].system_prompt
