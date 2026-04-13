"""Tests for T676 — SKILL.md frontmatter parsing and loading.

Covers:
- ``parse_frontmatter``: frontmatter extraction, fallback, edge cases
- ``normalise_skill``: field extraction, validation, defaults
- ``load_skill_md``: file loading, with/without frontmatter
- ``_load_skill_md_files`` in catalog.py: directory scanning
- ``SkillMD.to_catalog_agent_fields``: catalog integration
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bernstein.core.skill_md import SkillMD, load_skill_md, normalise_skill, parse_frontmatter

from bernstein.agents.catalog import _load_skill_md_files  # pyright: ignore[reportPrivateUsage]

# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_empty_string_returns_empty_fm_and_body(self) -> None:
        fm, body = parse_frontmatter("")
        assert fm == {}
        assert body == ""

    def test_no_frontmatter_returns_full_body(self) -> None:
        content = "# Just markdown\n\nNo frontmatter here."
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_full_frontmatter_parsed(self) -> None:
        content = "---\nname: test-skill\ndescription: A test skill\neffort: high\n---\n# Heading\n\nBody content."
        fm, body = parse_frontmatter(content)
        assert fm["name"] == "test-skill"
        assert fm["description"] == "A test skill"
        assert fm["effort"] == "high"
        assert body == "# Heading\n\nBody content."

    def test_frontmatter_with_list_fields(self) -> None:
        content = (
            "---\nname: full-skill\nhooks:\n  - pytest\n  - ruff\npaths:\n  - src/bernstein/\n---\n# Full SKILL.md"
        )
        fm, _body = parse_frontmatter(content)
        assert fm["hooks"] == ["pytest", "ruff"]
        assert fm["paths"] == ["src/bernstein/"]

    def test_no_closing_fence_returns_empty_fm(self) -> None:
        """Missing closing --- means no frontmatter detected."""
        content = "---\nname: broken\nbody text"
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert "name: broken" in body

    def test_empty_frontmatter_block(self) -> None:
        content = "---\n---\n# Body only"
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == "# Body only"

    def test_invalid_yaml_returns_empty_fm(self) -> None:
        content = "---\nname: [unclosed\n---\nBody"
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == "Body"

    def test_frontmatter_not_at_start_returns_as_body(self) -> None:
        content = "Some text\n---\nname: foo\n---\nMore"
        fm, body = parse_frontmatter(content)
        assert fm == {}
        # Full content returned as body.
        assert "Some text" in body

    def test_yaml_not_a_dict_returns_empty(self) -> None:
        """A YAML block that parses as a scalar (not a dict) yields empty fm."""
        content = "---\n- item1\n- item2\n---\nBody"
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == "Body"

    def test_leading_whitespace_before_fence(self) -> None:
        """Strips leading/trailing whitespace before checking for fence."""
        content = "  ---\nname: ws-skill\n---\nContent"
        fm, body = parse_frontmatter(content)
        assert fm == {"name": "ws-skill"}
        assert body == "Content"


# ---------------------------------------------------------------------------
# normalise_skill
# ---------------------------------------------------------------------------


class TestNormaliseSkill:
    def test_empty_data_returns_defaults(self) -> None:
        result = normalise_skill({})
        assert result["name"] == ""
        assert result["description"] == ""
        assert result["hooks"] == []
        assert result["paths"] == []
        assert result["context"] == ""
        assert result["effort"] == "normal"

    def test_valid_fields_passed_through(self) -> None:
        data: dict[str, Any] = {
            "name": "backend-dev",
            "description": "Builds APIs",
            "hooks": ["pytest", "mypy"],
            "paths": ["src/"],
            "context": "FastAPI server",
            "effort": "high",
        }
        result = normalise_skill(data)
        assert result == data

    def test_invalid_effort_normalised(self) -> None:
        result = normalise_skill({"effort": "mega-ultra"})
        assert result["effort"] == "normal"

    def test_hooks_not_string_list_uses_default(self) -> None:
        result = normalise_skill({"hooks": "not-a-list"})
        assert result["hooks"] == []

    def test_paths_not_string_list_uses_default(self) -> None:
        result = normalise_skill({"paths": {"src": "value"}})
        assert result["paths"] == []

    def test_name_and_description_stripped(self) -> None:
        result = normalise_skill({"name": "  spaced  ", "description": "  desc  "})
        assert result["name"] == "spaced"
        assert result["description"] == "desc"

    def test_defaults_override(self) -> None:
        defaults = {"effort": "max", "context": "default-ctx"}
        result = normalise_skill({}, defaults=defaults)
        assert result["effort"] == "max"
        assert result["context"] == "default-ctx"

    def test_unknown_keys_ignored(self) -> None:
        result = normalise_skill({"unknown_field": 42, "name": "ok"})
        assert "unknown_field" not in result
        assert result["name"] == "ok"

    def test_effort_case_insensitive(self) -> None:
        result = normalise_skill({"effort": "HIGH"})
        assert result["effort"] == "high"


# ---------------------------------------------------------------------------
# SkillMD dataclass
# ---------------------------------------------------------------------------


class TestSkillMDClass:
    def test_to_catalog_agent_fields(self) -> None:
        skill = SkillMD(
            name="test",
            description="A test",
            hooks=[],
            paths=[],
            context="",
            effort="high",
            body="# Test",
            source="/fake/path.md",
        )
        fields = skill.to_catalog_agent_fields()
        assert fields == {"description": "A test", "model": "sonnet", "effort": "high"}


# ---------------------------------------------------------------------------
# load_skill_md
# ---------------------------------------------------------------------------


class TestLoadSkillMd:
    def test_load_file_with_full_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "SKILL.md"
        f.write_text(
            "---\n"
            "name: my-skill\n"
            "description: Does things\n"
            "hooks: [pytest]\n"
            "paths: [src/]\n"
            "context: backend\n"
            "effort: max\n"
            "---\n"
            "# My Skill\n\nBody text.",
            encoding="utf-8",
        )
        skill = load_skill_md(f)
        assert skill is not None
        assert skill.name == "my-skill"
        assert skill.description == "Does things"
        assert skill.hooks == ["pytest"]
        assert skill.paths == ["src/"]
        assert skill.context == "backend"
        assert skill.effort == "max"
        assert skill.body == "# My Skill\n\nBody text."

    def test_load_file_no_frontmatter_with_fallback(self, tmp_path: Path) -> None:
        f = tmp_path / "SKILL.md"
        f.write_text("# Just a markdown file\n\nNo frontmatter.", encoding="utf-8")
        skill = load_skill_md(f, role_fallback="fallback-role")
        assert skill is not None
        assert skill.name == "fallback-role"
        assert "# Just a markdown file" in skill.body
        assert skill.hooks == []
        assert skill.effort == "normal"

    def test_load_file_no_frontmatter_no_fallback_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "SKILL.md"
        f.write_text("# No frontmatter.", encoding="utf-8")
        assert load_skill_md(f) is None

    def test_empty_name_with_no_fallback_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "SKILL.md"
        f.write_text("---\nname: ''\ndescription: empty\n---\nBody", encoding="utf-8")
        assert load_skill_md(f) is None

    def test_file_not_found_returns_none(self, tmp_path: Path) -> None:
        assert load_skill_md(tmp_path / "nonexistent.md") is None

    def test_partial_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "SKILL.md"
        f.write_text(
            "---\nname: partial\neffort: low\n---\n# Partial",
            encoding="utf-8",
        )
        skill = load_skill_md(f)
        assert skill is not None
        assert skill.name == "partial"
        assert skill.effort == "low"
        assert skill.description == ""

    def test_source_field_set(self, tmp_path: Path) -> None:
        f = tmp_path / "SKILL.md"
        f.write_text("---\nname: src-test\n---\nBody", encoding="utf-8")
        skill = load_skill_md(f)
        assert skill is not None
        assert str(f) in skill.source


# ---------------------------------------------------------------------------
# _load_skill_md_files (catalog integration)
# ---------------------------------------------------------------------------


class TestLoadSkillMdFiles:
    def test_discover_subdirectory_skill(self, tmp_path: Path) -> None:
        role_dir = tmp_path / "backend"
        role_dir.mkdir()
        (role_dir / "SKILL.md").write_text(
            "---\nname: backend-dev\neffort: high\n---\n# Backend",
            encoding="utf-8",
        )
        result = _load_skill_md_files(tmp_path)
        assert "backend-dev" in result
        assert result["backend-dev"]["effort"] == "high"

    def test_discover_root_skill(self, tmp_path: Path) -> None:
        (tmp_path / "SKILL.md").write_text(
            "---\nname: root-skill\ndescription: At root\n---\n# Root",
            encoding="utf-8",
        )
        result = _load_skill_md_files(tmp_path)
        assert "root-skill" in result

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        assert _load_skill_md_files(tmp_path) == {}

    def test_non_skill_md_files_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# Readme", encoding="utf-8")
        (tmp_path / "other.txt").write_text("other", encoding="utf-8")
        assert _load_skill_md_files(tmp_path) == {}

    def test_skill_without_name_uses_stem_fallback(self, tmp_path: Path) -> None:
        role_dir = tmp_path / "qa"
        role_dir.mkdir()
        (role_dir / "SKILL.md").write_text(
            "---\ndescription: Test engineer\n---\n# QA",
            encoding="utf-8",
        )
        result = _load_skill_md_files(tmp_path)
        # No name in fm → fallback to directory stem "qa".
        assert "qa" in result

    def test_multiple_skills_in_subdirs(self, tmp_path: Path) -> None:
        for role in ("backend", "frontend", "devops"):
            d = tmp_path / role
            d.mkdir()
            (d / "SKILL.md").write_text(
                f"---\nname: {role}-skill\neffort: normal\n---\n# {role}",
                encoding="utf-8",
            )
        result = _load_skill_md_files(tmp_path)
        assert len(result) == 3
        assert "backend-skill" in result
        assert "frontend-skill" in result
        assert "devops-skill" in result
