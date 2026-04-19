"""Tests for ``bernstein.core.skills.manifest``."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bernstein.core.skills.manifest import (
    SkillManifest,
    SkillManifestError,
    parse_skill_md,
)


def _write(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    return path


def test_parse_skill_md_round_trips_minimal_manifest(tmp_path: Path) -> None:
    skill_md = _write(
        tmp_path / "SKILL.md",
        """
        ---
        name: backend
        description: Backend skill for server-side Python work covering APIs and data.
        ---
        # Body

        Prose goes here.
        """,
    )

    manifest, body = parse_skill_md(skill_md)

    assert manifest.name == "backend"
    assert manifest.description.startswith("Backend skill")
    assert manifest.references == []
    assert body.startswith("# Body")


def test_parse_skill_md_accepts_optional_fields(tmp_path: Path) -> None:
    skill_md = _write(
        tmp_path / "SKILL.md",
        """
        ---
        name: qa
        description: Quality assurance skill covering pytest, edge cases, and regressions.
        trigger_keywords:
          - pytest
          - regression
        references:
          - checklist.md
        scripts:
          - run.sh
        version: "2.1.0"
        author: Team QA
        ---
        body
        """,
    )

    manifest, _ = parse_skill_md(skill_md)

    assert manifest.trigger_keywords == ["pytest", "regression"]
    assert manifest.references == ["checklist.md"]
    assert manifest.scripts == ["run.sh"]
    assert manifest.version == "2.1.0"
    assert manifest.author == "Team QA"


def test_parse_skill_md_rejects_missing_frontmatter(tmp_path: Path) -> None:
    skill_md = _write(tmp_path / "SKILL.md", "# no frontmatter here")

    with pytest.raises(SkillManifestError) as excinfo:
        parse_skill_md(skill_md)
    assert "frontmatter" in str(excinfo.value)
    assert excinfo.value.path == skill_md


def test_parse_skill_md_rejects_invalid_name(tmp_path: Path) -> None:
    skill_md = _write(
        tmp_path / "SKILL.md",
        """
        ---
        name: Bad Name
        description: This name contains spaces which violates the slug regex rule.
        ---
        body
        """,
    )

    with pytest.raises(SkillManifestError) as excinfo:
        parse_skill_md(skill_md)
    assert "name" in str(excinfo.value).lower()


def test_parse_skill_md_rejects_short_description(tmp_path: Path) -> None:
    skill_md = _write(
        tmp_path / "SKILL.md",
        """
        ---
        name: ok
        description: too short
        ---
        body
        """,
    )

    with pytest.raises(SkillManifestError):
        parse_skill_md(skill_md)


def test_parse_skill_md_rejects_unknown_keys(tmp_path: Path) -> None:
    skill_md = _write(
        tmp_path / "SKILL.md",
        """
        ---
        name: ok
        description: A valid description that exceeds twenty characters in length.
        keywords: [typo]
        ---
        body
        """,
    )

    with pytest.raises(SkillManifestError):
        parse_skill_md(skill_md)


def test_parse_skill_md_rejects_non_mapping(tmp_path: Path) -> None:
    skill_md = _write(
        tmp_path / "SKILL.md",
        """
        ---
        - name: not-a-dict
        - description: But YAML list instead of mapping, so should fail.
        ---
        body
        """,
    )

    with pytest.raises(SkillManifestError):
        parse_skill_md(skill_md)


def test_parse_skill_md_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SkillManifestError):
        parse_skill_md(tmp_path / "does-not-exist.md")


def test_skill_manifest_is_immutable() -> None:
    manifest = SkillManifest(
        name="x",
        description="A valid description for the immutability test harness.",
    )
    with pytest.raises((TypeError, ValueError)):
        manifest.name = "y"  # type: ignore[misc]
