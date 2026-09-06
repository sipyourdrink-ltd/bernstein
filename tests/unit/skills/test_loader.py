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
        return self._artifacts.copy()


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


def test_loader_quarantines_a_duplicate_name() -> None:
    """A conflict is recorded, not fatal (#5108).

    This used to raise out of ``__init__``, which meant one duplicate name took
    every OTHER skill down with it. Detection is what mattered -- two plugins
    must never silently shadow each other -- and the quarantine record keeps
    that while leaving the rest of the set loaded. The first origin wins,
    deterministically, so precedence still follows source order.
    """
    first = _InMemorySource("first", [_artifact("conflict", "from-first")])
    second = _InMemorySource("second", [_artifact("conflict", "from-second"), _artifact("fine", "from-second")])

    loader = SkillLoader([first, second])

    assert loader.get("conflict").origin == "from-first"
    assert loader.has("fine") is True, "the duplicate must not cost the source's other skills"

    (quarantined,) = loader.quarantined
    assert quarantined.error_type == DuplicateSkillError.__name__
    assert quarantined.skill_name == "conflict"
    assert quarantined.origin == "from-second"
    assert "from-first" in quarantined.reason and "from-second" in quarantined.reason


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
