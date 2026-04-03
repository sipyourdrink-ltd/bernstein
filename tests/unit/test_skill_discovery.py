"""Tests for T797 — skill discovery priority order."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.skill_discovery import (
    SkillDefinition,
    SkillResolver,
    SkillSource,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_skill(
    directory: Path,
    name: str,
    description: str = "",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write a skill JSON file and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"name": name, "description": description}
    if extra:
        data.update(extra)
    path = directory / f"{name}.skill.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture()
def resolver(tmp_path: Path) -> SkillResolver:
    """Empty resolver scoped to tmp_path."""
    return SkillResolver(workdir=tmp_path, home_dir=tmp_path / "home")


# ---------------------------------------------------------------------------
# SkillSource
# ---------------------------------------------------------------------------


class TestSkillSource:
    def test_priority_order(self) -> None:
        sorted_sources = sorted(SkillSource, key=lambda s: s.sort_key)
        expected = [
            SkillSource.MANAGED,
            SkillSource.USER,
            SkillSource.PROJECT,
            SkillSource.ADDITIONAL,
            SkillSource.PLUGIN,
            SkillSource.MCP,
        ]
        assert sorted_sources == expected

    def test_sort_key_values(self) -> None:
        assert SkillSource.MANAGED.sort_key == 0
        assert SkillSource.USER.sort_key == 1
        assert SkillSource.MCP.sort_key == 5


# ---------------------------------------------------------------------------
# SkillDefinition
# ---------------------------------------------------------------------------


class TestSkillDefinition:
    def test_priority_key(self) -> None:
        skill = SkillDefinition(
            name="build",
            description="Build skill",
            source=SkillSource.PROJECT,
            origin="/proj",
        )
        assert skill.priority_key == "2:build"

    def test_metadata_defaults_empty(self) -> None:
        skill = SkillDefinition(name="x", description="d", source=SkillSource.USER, origin="o")
        assert skill.metadata == {}


# ---------------------------------------------------------------------------
# SkillResolver - basic discovery
# ---------------------------------------------------------------------------


class TestSkillResolver:
    def test_empty_resolver(self, resolver: SkillResolver) -> None:
        result = resolver.resolve()
        assert result.skills == {}
        assert result.conflicts == []
        assert result.warnings == []

    def test_project_skills(self, resolver: SkillResolver) -> None:
        project_skills = resolver.workdir / ".bernstein" / "skills"
        _write_skill(project_skills, "build", "Build project")
        _write_skill(project_skills, "deploy", "Deploy to prod")

        result = resolver.resolve()
        assert "build" in result.skills
        assert "deploy" in result.skills
        assert result.skills["build"].source == SkillSource.PROJECT
        assert result.skills["deploy"].source == SkillSource.PROJECT

    def test_user_skills(self, resolver: SkillResolver) -> None:
        user_skills = resolver.home_dir / ".bernstein" / "skills"
        _write_skill(user_skills, "custom", "User custom skill")

        result = resolver.resolve()
        assert "custom" in result.skills
        assert result.skills["custom"].source == SkillSource.USER

    def test_managed_skills(self, resolver: SkillResolver, tmp_path: Path) -> None:
        managed = tmp_path / "managed"
        resolver.managed_dir = managed
        _write_skill(managed, "policy-enforce", "Managed policy")

        result = resolver.resolve()
        assert "policy-enforce" in result.skills
        assert result.skills["policy-enforce"].source == SkillSource.MANAGED

    def test_additional_dirs(self, resolver: SkillResolver, tmp_path: Path) -> None:
        extra = tmp_path / "extra_skills"
        resolver.additional_dirs = [extra]
        _write_skill(extra, "extra-tool", "Extra dir tool")

        result = resolver.resolve()
        assert "extra-tool" in result.skills
        assert result.skills["extra-tool"].source == SkillSource.ADDITIONAL

    def test_plugin_skills(self, resolver: SkillResolver) -> None:
        resolver.plugin_skills = {
            "plugin-skill": SkillDefinition(
                name="plugin-skill",
                description="From plugin",
                source=SkillSource.PLUGIN,
                origin="pkg://some-plugin",
            ),
        }
        result = resolver.resolve()
        assert "plugin-skill" in result.skills
        assert result.skills["plugin-skill"].source == SkillSource.PLUGIN

    def test_mcp_skills(self, resolver: SkillResolver) -> None:
        resolver.mcp_skills = {
            "mcp-skill": SkillDefinition(
                name="mcp-skill",
                description="From MCP",
                source=SkillSource.MCP,
                origin="mcp://server",
            ),
        }
        result = resolver.resolve()
        assert "mcp-skill" in result.skills
        assert result.skills["mcp-skill"].source == SkillSource.MCP


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------


class TestConflictResolution:
    """Earlier (higher-priority) sources win over later ones."""

    def test_managed_wins_over_user(self, resolver: SkillResolver) -> None:
        managed = resolver.home_dir.parent / "managed"
        resolver.managed_dir = managed
        _write_skill(managed, "build", "Managed build")
        _write_skill(resolver.home_dir / ".bernstein" / "skills", "build", "User build")

        result = resolver.resolve()
        skill = result.skills["build"]
        assert skill.source == SkillSource.MANAGED
        assert "Managed build" in skill.description

        assert len(result.conflicts) == 1
        assert result.conflicts[0]["skill"] == "build"
        assert result.conflicts[0]["winner"] == "managed"
        assert result.conflicts[0]["loser"] == "user"

    def test_managed_wins_over_project(self, resolver: SkillResolver) -> None:
        managed = resolver.home_dir.parent / "managed"
        resolver.managed_dir = managed
        _write_skill(managed, "lint", "Managed lint")
        _write_skill(resolver.workdir / ".bernstein" / "skills", "lint", "Project lint")

        result = resolver.resolve()
        assert result.skills["lint"].source == SkillSource.MANAGED
        assert any(c["skill"] == "lint" for c in result.conflicts)

    def test_user_wins_over_project(self, resolver: SkillResolver) -> None:
        _write_skill(resolver.home_dir / ".bernstein" / "skills", "debug", "User debug")
        _write_skill(resolver.workdir / ".bernstein" / "skills", "debug", "Project debug")

        result = resolver.resolve()
        assert result.skills["debug"].source == SkillSource.USER

    def test_three_way_conflict(self, resolver: SkillResolver) -> None:
        managed = resolver.home_dir.parent / "managed"
        resolver.managed_dir = managed
        _write_skill(managed, "test", "Managed")
        _write_skill(resolver.home_dir / ".bernstein" / "skills", "test", "User")
        _write_skill(resolver.workdir / ".bernstein" / "skills", "test", "Project")

        result = resolver.resolve()
        assert result.skills["test"].source == SkillSource.MANAGED
        assert len(result.conflicts) == 2

    def test_no_conflict_different_names(self, resolver: SkillResolver) -> None:
        _write_skill(resolver.workdir / ".bernstein" / "skills", "build", "Build")
        _write_skill(resolver.home_dir / ".bernstein" / "skills", "debug", "Debug")

        result = resolver.resolve()
        assert result.conflicts == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_missing_name_warned(self, resolver: SkillResolver) -> None:
        skills_dir = resolver.workdir / ".bernstein" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "noname.skill.json").write_text('{"description": "no name"}')

        result = resolver.resolve()
        assert any("missing" in w for w in result.warnings)

    def test_malformed_json_warned(self, resolver: SkillResolver) -> None:
        skills_dir = resolver.workdir / ".bernstein" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "bad.skill.json").write_text("{not json}")

        result = resolver.resolve()
        assert any("Failed" in w for w in result.warnings)

    def test_unknown_directory_returns_empty(self, resolver: SkillResolver) -> None:
        result = resolver.resolve()
        assert result.skills == {}

    def test_skill_file_sorted(self, resolver: SkillResolver) -> None:
        skills_dir = resolver.workdir / ".bernstein" / "skills"
        _write_skill(skills_dir, "zebra", "Last alpha")
        _write_skill(skills_dir, "alpha", "First alpha")

        result = resolver.resolve()
        assert "zebra" in result.skills
        assert "alpha" in result.skills

    def test_skill_metadata_preserved(self, resolver: SkillResolver) -> None:
        skills_dir = resolver.workdir / ".bernstein" / "skills"
        _write_skill(
            skills_dir,
            "rich",
            "Rich metadata",
            {"category": "tooling", "version": 2},
        )

        result = resolver.resolve()
        assert result.skills["rich"].metadata == {"category": "tooling", "version": 2}
