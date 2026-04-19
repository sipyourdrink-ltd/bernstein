"""Tests for :func:`bernstein.core.skills.build_skill_index`."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.skills.index_builder import build_skill_index
from bernstein.core.skills.loader import SkillLoader, SkillNotFoundError
from bernstein.core.skills.sources import LocalDirSkillSource


def _make_loader(root: Path) -> SkillLoader:
    return SkillLoader([LocalDirSkillSource(root)])


def test_index_contains_every_skill_name_and_description(sample_skills_root: Path) -> None:
    loader = _make_loader(sample_skills_root)
    index = build_skill_index(loader)

    assert "- alpha:" in index
    assert "- beta:" in index
    assert "- gamma:" in index
    # Descriptions appear verbatim.
    assert "simple test skill" in index


def test_index_highlights_primary_skill(sample_skills_root: Path) -> None:
    loader = _make_loader(sample_skills_root)
    index = build_skill_index(loader, highlight="beta")

    # Primary skill uses a ``*`` marker; others use ``-``.
    assert "* beta:" in index
    # Primary skill appears before the others in the output.
    beta_position = index.index("* beta:")
    alpha_position = index.index("- alpha:")
    assert beta_position < alpha_position


def test_index_raises_when_highlight_is_unknown(sample_skills_root: Path) -> None:
    loader = _make_loader(sample_skills_root)
    with pytest.raises(SkillNotFoundError):
        build_skill_index(loader, highlight="unknown")


def test_index_empty_when_no_skills(tmp_path: Path) -> None:
    loader = _make_loader(tmp_path / "empty")
    assert build_skill_index(loader) == ""


def test_index_header_mentions_skills_keyword(sample_skills_root: Path) -> None:
    loader = _make_loader(sample_skills_root)
    index = build_skill_index(loader)
    # Terse header; agents learn the load_skill syntax from the role hint.
    assert "Skills" in index
