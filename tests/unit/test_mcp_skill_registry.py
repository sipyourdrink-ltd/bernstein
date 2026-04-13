"""Tests for MCP skill registry — T1: bridge MCP servers to skill-like prompts."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from bernstein.core.mcp_skill_registry import (
    build_mcp_skills_from_tools,
    clear_registry,
    get_mcp_skills,
    register_mcp_skills,
)
from bernstein.core.skill_discovery import SkillDefinition, SkillResolver, SkillSource


@pytest.fixture(autouse=True)
def clean_registry() -> Generator[None, None, None]:
    """Clear the MCP skill registry before and after each test."""
    clear_registry()
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# register_mcp_skills / get_mcp_skills
# ---------------------------------------------------------------------------


class TestRegisterMcpSkills:
    def test_register_and_retrieve(self) -> None:
        skill = SkillDefinition(
            name="search-issues",
            description="Search GitHub issues.",
            source=SkillSource.MCP,
            origin="mcp://github-server",
        )
        register_mcp_skills("github-server", [skill])

        skills = get_mcp_skills()
        assert "search-issues" in skills
        assert skills["search-issues"].source == SkillSource.MCP
        assert skills["search-issues"].origin == "mcp://github-server"

    def test_write_once_ignores_second_registration(self) -> None:
        """Second registration for the same server is silently ignored."""
        skill_v1 = SkillDefinition(
            name="deploy",
            description="Deploy v1",
            source=SkillSource.MCP,
            origin="mcp://server",
        )
        skill_v2 = SkillDefinition(
            name="deploy",
            description="Deploy v2",
            source=SkillSource.MCP,
            origin="mcp://server",
        )
        register_mcp_skills("deploy-server", [skill_v1])
        register_mcp_skills("deploy-server", [skill_v2])  # ignored

        skills = get_mcp_skills()
        assert skills["deploy"].description == "Deploy v1"

    def test_multiple_servers_merged(self) -> None:
        skill_a = SkillDefinition(
            name="skill-a",
            description="A",
            source=SkillSource.MCP,
            origin="mcp://server-a",
        )
        skill_b = SkillDefinition(
            name="skill-b",
            description="B",
            source=SkillSource.MCP,
            origin="mcp://server-b",
        )
        register_mcp_skills("server-a", [skill_a])
        register_mcp_skills("server-b", [skill_b])

        skills = get_mcp_skills()
        assert "skill-a" in skills
        assert "skill-b" in skills

    def test_first_server_wins_on_name_conflict(self) -> None:
        """When two different servers provide the same skill name, first wins."""
        s1 = SkillDefinition(
            name="lint",
            description="From server 1",
            source=SkillSource.MCP,
            origin="mcp://server-1",
        )
        s2 = SkillDefinition(
            name="lint",
            description="From server 2",
            source=SkillSource.MCP,
            origin="mcp://server-2",
        )
        register_mcp_skills("server-1", [s1])
        register_mcp_skills("server-2", [s2])

        skills = get_mcp_skills()
        assert skills["lint"].description == "From server 1"

    def test_empty_registry_returns_empty(self) -> None:
        assert get_mcp_skills() == {}

    def test_clear_registry_removes_all(self) -> None:
        skill = SkillDefinition(
            name="x",
            description="d",
            source=SkillSource.MCP,
            origin="mcp://s",
        )
        register_mcp_skills("s", [skill])
        assert get_mcp_skills() != {}

        clear_registry()
        assert get_mcp_skills() == {}


# ---------------------------------------------------------------------------
# build_mcp_skills_from_tools
# ---------------------------------------------------------------------------


class TestBuildMcpSkillsFromTools:
    def test_basic_tool_converted(self) -> None:
        tools = [{"name": "get-repo", "description": "Get repository info."}]
        skills = build_mcp_skills_from_tools("gh-server", tools)

        assert len(skills) == 1
        assert skills[0].name == "get-repo"
        assert skills[0].description == "Get repository info."
        assert skills[0].source == SkillSource.MCP
        assert skills[0].origin == "mcp://gh-server"

    def test_tool_without_description_defaults_empty(self) -> None:
        tools = [{"name": "ping"}]
        skills = build_mcp_skills_from_tools("server", tools)
        assert skills[0].description == ""

    def test_tool_without_name_skipped(self) -> None:
        tools = [{"description": "no name here"}]
        skills = build_mcp_skills_from_tools("server", tools)
        assert skills == []

    def test_empty_name_skipped(self) -> None:
        tools = [{"name": "  ", "description": "blank name"}]
        skills = build_mcp_skills_from_tools("server", tools)
        assert skills == []

    def test_extra_fields_preserved_in_metadata(self) -> None:
        tools = [{"name": "query", "description": "Query DB.", "inputSchema": {"type": "object"}}]
        skills = build_mcp_skills_from_tools("db-server", tools)
        assert "inputSchema" in skills[0].metadata

    def test_name_and_description_not_in_metadata(self) -> None:
        tools = [{"name": "foo", "description": "bar", "extra": 1}]
        skills = build_mcp_skills_from_tools("s", tools)
        assert "name" not in skills[0].metadata
        assert "description" not in skills[0].metadata
        assert skills[0].metadata == {"extra": 1}

    def test_multiple_tools_all_converted(self) -> None:
        tools = [
            {"name": "create-issue", "description": "Create issue"},
            {"name": "close-issue", "description": "Close issue"},
        ]
        skills = build_mcp_skills_from_tools("jira-server", tools)
        assert len(skills) == 2
        assert {s.name for s in skills} == {"create-issue", "close-issue"}

    def test_empty_tool_list_returns_empty(self) -> None:
        assert build_mcp_skills_from_tools("server", []) == []


# ---------------------------------------------------------------------------
# Integration: registry → SkillResolver
# ---------------------------------------------------------------------------


class TestMcpSkillsInResolver:
    """MCP skills from registry are discoverable via SkillResolver.mcp_skills."""

    def test_resolver_uses_registry_skills(self, tmp_path: Path) -> None:
        skill = SkillDefinition(
            name="mcp-from-registry",
            description="Registered via MCP registry",
            source=SkillSource.MCP,
            origin="mcp://test-server",
        )
        register_mcp_skills("test-server", [skill])

        resolver = SkillResolver(workdir=tmp_path, mcp_skills=get_mcp_skills())
        result = resolver.resolve()

        assert "mcp-from-registry" in result.skills
        assert result.skills["mcp-from-registry"].source == SkillSource.MCP

    def test_build_then_register_then_resolve(self, tmp_path: Path) -> None:
        tools = [
            {"name": "list-prs", "description": "List open pull requests."},
            {"name": "merge-pr", "description": "Merge a pull request."},
        ]
        skills = build_mcp_skills_from_tools("github-server", tools)
        register_mcp_skills("github-server", skills)

        resolver = SkillResolver(workdir=tmp_path, mcp_skills=get_mcp_skills())
        result = resolver.resolve()

        assert "list-prs" in result.skills
        assert "merge-pr" in result.skills
        assert result.skills["list-prs"].description == "List open pull requests."

    def test_mcp_skill_loses_to_project_skill(self, tmp_path: Path) -> None:
        """PROJECT source wins over MCP (lower priority number = higher priority)."""
        import json

        skills_dir = tmp_path / ".bernstein" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "deploy.skill.json").write_text(
            json.dumps({"name": "deploy", "description": "Project deploy"}),
            encoding="utf-8",
        )

        mcp_skill = SkillDefinition(
            name="deploy",
            description="MCP deploy",
            source=SkillSource.MCP,
            origin="mcp://ci-server",
        )
        register_mcp_skills("ci-server", [mcp_skill])

        resolver = SkillResolver(workdir=tmp_path, mcp_skills=get_mcp_skills())
        result = resolver.resolve()

        # Project skill (priority 2) wins over MCP (priority 5)
        assert result.skills["deploy"].source == SkillSource.PROJECT
        assert len(result.conflicts) == 1
        assert result.conflicts[0]["winner"] == "project"
        assert result.conflicts[0]["loser"] == "mcp"
