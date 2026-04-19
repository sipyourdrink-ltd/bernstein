"""Tests for :class:`LocalDirSkillSource`."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.skills.manifest import SkillManifestError
from bernstein.core.skills.sources import LocalDirSkillSource


def test_iter_skills_returns_empty_when_root_missing(tmp_path: Path) -> None:
    source = LocalDirSkillSource(tmp_path / "missing")
    assert source.iter_skills() == []


def test_iter_skills_lists_every_skill_in_root(sample_skills_root: Path) -> None:
    source = LocalDirSkillSource(sample_skills_root)

    artifacts = source.iter_skills()

    names = [a.manifest.name for a in artifacts]
    assert names == ["alpha", "beta", "gamma"]
    assert all(a.body for a in artifacts)


def test_iter_skills_rejects_mismatched_directory_name(tmp_path: Path, write_skill) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    # Create a skill where manifest name disagrees with the directory name.
    skill_dir = root / "dir-name"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: different
description: Description long enough to pass the 20-char minimum length check.
---
body""",
        encoding="utf-8",
    )

    source = LocalDirSkillSource(root)

    with pytest.raises(SkillManifestError):
        source.iter_skills()


def test_read_reference_returns_content(sample_skills_root: Path) -> None:
    source = LocalDirSkillSource(sample_skills_root)
    content = source.read_reference("alpha", "deep-dive.md")
    assert "# Deep dive for alpha" in content


def test_read_reference_raises_when_missing(sample_skills_root: Path) -> None:
    source = LocalDirSkillSource(sample_skills_root)
    with pytest.raises(FileNotFoundError):
        source.read_reference("alpha", "missing.md")


def test_read_reference_rejects_path_traversal(sample_skills_root: Path) -> None:
    source = LocalDirSkillSource(sample_skills_root)
    with pytest.raises(ValueError):
        source.read_reference("alpha", "../../secrets.txt")


def test_list_references_returns_sorted_filenames(sample_skills_root: Path) -> None:
    source = LocalDirSkillSource(sample_skills_root)
    refs = source.list_references("alpha")
    assert refs == ["deep-dive.md"]


def test_list_scripts_returns_sorted_filenames(sample_skills_root: Path) -> None:
    source = LocalDirSkillSource(sample_skills_root)
    scripts = source.list_scripts("alpha")
    assert scripts == ["hello.sh"]


def test_manifest_for_returns_none_for_missing_skill(sample_skills_root: Path) -> None:
    source = LocalDirSkillSource(sample_skills_root)
    assert source.manifest_for("does-not-exist") is None


def test_manifest_for_returns_manifest_for_existing_skill(sample_skills_root: Path) -> None:
    source = LocalDirSkillSource(sample_skills_root)
    manifest = source.manifest_for("alpha")
    assert manifest is not None
    assert manifest.name == "alpha"
