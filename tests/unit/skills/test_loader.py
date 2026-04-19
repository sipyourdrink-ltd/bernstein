"""Tests for :class:`bernstein.core.skills.SkillLoader`."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.skills.loader import (
    DuplicateSkillError,
    SkillLoader,
    SkillNotFoundError,
)
from bernstein.core.skills.manifest import SkillManifest
from bernstein.core.skills.source import SkillArtifact, SkillSource
from bernstein.core.skills.sources import LocalDirSkillSource


class _InMemorySource(SkillSource):
    """Deterministic in-memory source used when testing conflict paths."""

    def __init__(self, label: str, artifacts: list[SkillArtifact]) -> None:
        self._label = label
        self._artifacts = artifacts

    @property
    def name(self) -> str:
        return self._label

    def iter_skills(self) -> list[SkillArtifact]:
        return list(self._artifacts)


def _artifact(name: str, origin: str) -> SkillArtifact:
    return SkillArtifact(
        manifest=SkillManifest(
            name=name,
            description="A stub description exceeding twenty characters easily.",
        ),
        body=f"# body for {name}",
        origin=origin,
    )


def test_loader_indexes_sources_in_order(sample_skills_root: Path) -> None:
    local = LocalDirSkillSource(sample_skills_root)
    loader = SkillLoader([local])

    names = [s.name for s in loader.list_all()]
    assert names == ["alpha", "beta", "gamma"]
    assert loader.has("alpha") is True
    assert loader.has("does-not-exist") is False


def test_loader_raises_on_duplicate_name() -> None:
    first = _InMemorySource("first", [_artifact("conflict", "from-first")])
    second = _InMemorySource("second", [_artifact("conflict", "from-second")])

    with pytest.raises(DuplicateSkillError) as excinfo:
        SkillLoader([first, second])

    err = excinfo.value
    assert err.skill_name == "conflict"
    assert err.first_origin == "from-first"
    assert err.second_origin == "from-second"


def test_loader_get_raises_skill_not_found_error() -> None:
    loader = SkillLoader([_InMemorySource("only", [_artifact("alpha", "x")])])
    with pytest.raises(SkillNotFoundError):
        loader.get("missing")


def test_loader_read_reference_delegates_to_owning_source(
    sample_skills_root: Path,
) -> None:
    loader = SkillLoader([LocalDirSkillSource(sample_skills_root)])
    content = loader.read_reference("alpha", "deep-dive.md")
    assert "Deep dive" in content


def test_loader_read_reference_errors_when_source_lacks_support() -> None:
    loader = SkillLoader([_InMemorySource("only", [_artifact("alpha", "x")])])
    with pytest.raises(RuntimeError):
        loader.read_reference("alpha", "anything.md")


def test_loader_find_source_for_returns_owning_source(sample_skills_root: Path) -> None:
    local = LocalDirSkillSource(sample_skills_root, source_name="local-xyz")
    loader = SkillLoader([local])
    assert loader.find_source_for("alpha").name == "local-xyz"
