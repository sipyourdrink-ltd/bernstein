"""Issue #5108: one broken skill source must not take every other skill with it.

``SkillLoader._reload`` had no exception handling, and ``__init__`` calls it
directly. A source whose ``iter_skills()`` threw therefore raised out of the
constructor: the caller got no ``SkillLoader`` at all, and every skill from
every OTHER source failed to load because of one bad entry.

A failure that vanishes into a log line is the same as no failure at all to the
operator reading ``status``, so the loader records what it refused to index and
why, and the rest of the set loads.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from bernstein.core.skills.loader import (
    DuplicateSkillError,
    QuarantinedSkill,
    SkillLoader,
)
from bernstein.core.skills.manifest import SkillManifest
from bernstein.core.skills.source import SkillArtifact, SkillSource

if TYPE_CHECKING:
    from pathlib import Path


class _InMemorySource(SkillSource):
    """A source that yields a fixed artifact list. Idiom from ``test_loader.py``."""

    def __init__(self, label: str, artifacts: list[SkillArtifact]) -> None:
        self._label = label
        self._artifacts = artifacts

    @property
    def name(self) -> str:
        return self._label

    def iter_skills(self) -> list[SkillArtifact]:
        return self._artifacts.copy()


class _ThrowingSource(SkillSource):
    """A source that cannot enumerate at all -- an unreadable dir, a bad plugin."""

    def __init__(self, label: str, error: Exception) -> None:
        self._label = label
        self._error = error

    @property
    def name(self) -> str:
        return self._label

    def iter_skills(self) -> list[SkillArtifact]:
        raise self._error


def _artifact(name: str, origin: str) -> SkillArtifact:
    return SkillArtifact(
        manifest=SkillManifest(
            name=name,
            description="A stub description exceeding twenty characters easily.",
        ),
        body=f"# body for {name}",
        origin=origin,
    )


def test_one_throwing_source_quarantines_and_others_load() -> None:
    """The load-bearing case. On main the constructor raises and there is no loader."""
    broken = _ThrowingSource("broken-plugin", RuntimeError("entry point blew up"))
    healthy = _InMemorySource("healthy", [_artifact("alpha", "from-healthy")])

    loader = SkillLoader([broken, healthy])

    assert [s.name for s in loader.list_all()] == ["alpha"]

    (quarantined,) = loader.quarantined
    assert quarantined.source_name == "broken-plugin"
    assert quarantined.error_type == "RuntimeError"
    assert quarantined.reason == "entry point blew up"
    # A source that threw while enumerating never said WHICH skill it was
    # building, and claiming one would be an invention.
    assert quarantined.skill_name is None


def test_a_source_after_a_broken_one_still_loads() -> None:
    """Ordering: the isolation is per source, not "stop at the first failure"."""
    loader = SkillLoader(
        [
            _InMemorySource("first", [_artifact("alpha", "a")]),
            _ThrowingSource("broken", OSError("permission denied")),
            _InMemorySource("last", [_artifact("beta", "b")]),
        ]
    )

    assert [s.name for s in loader.list_all()] == ["alpha", "beta"]
    assert len(loader.quarantined) == 1


def test_duplicate_skill_name_quarantines_the_second_one() -> None:
    """A conflict is still never silent -- it is recorded rather than fatal."""
    first = _InMemorySource("first", [_artifact("conflict", "from-first")])
    second = _InMemorySource("second", [_artifact("conflict", "from-second")])

    loader = SkillLoader([first, second])

    assert loader.get("conflict").origin == "from-first"
    (quarantined,) = loader.quarantined
    assert quarantined.error_type == DuplicateSkillError.__name__
    assert quarantined.skill_name == "conflict"


def test_the_record_carries_a_reason_and_a_timestamp() -> None:
    """What the operator needs to act: which, why, and when."""
    before = time.time()
    loader = SkillLoader([_ThrowingSource("broken", ValueError("bad manifest"))])
    after = time.time()

    (quarantined,) = loader.quarantined
    assert isinstance(quarantined, QuarantinedSkill)
    assert quarantined.reason == "bad manifest"
    assert quarantined.error_type == "ValueError"
    assert before <= quarantined.at <= after


def test_the_hook_is_called_once_per_quarantine_as_it_happens() -> None:
    """The seam a caller journals a governance event through.

    The loader is deliberately stateless and holds no audit key, so it reports
    rather than writes -- and it reports at the moment of failure rather than
    handing back a list to diff afterwards.
    """
    seen: list[QuarantinedSkill] = []
    SkillLoader(
        [
            _ThrowingSource("broken-a", RuntimeError("a")),
            _ThrowingSource("broken-b", RuntimeError("b")),
            _InMemorySource("healthy", [_artifact("alpha", "x")]),
        ],
        on_quarantine=seen.append,
    )

    assert [record.reason for record in seen] == ["a", "b"]


def test_a_throwing_hook_does_not_break_the_load() -> None:
    """The reporter is not the subject.

    An audit sink that is down must not do what the broken skill could not.
    """

    def _explode(_record: QuarantinedSkill) -> None:
        raise RuntimeError("audit sink unavailable")

    loader = SkillLoader(
        [
            _ThrowingSource("broken", RuntimeError("boom")),
            _InMemorySource("healthy", [_artifact("alpha", "x")]),
        ],
        on_quarantine=_explode,
    )

    assert [s.name for s in loader.list_all()] == ["alpha"]
    assert len(loader.quarantined) == 1


def test_a_healthy_load_quarantines_nothing() -> None:
    """The control: the record is empty when nothing failed."""
    loader = SkillLoader([_InMemorySource("healthy", [_artifact("alpha", "x")])])
    assert loader.quarantined == ()


def test_a_reload_clears_the_previous_quarantine(sample_skills_root: Path) -> None:
    """Quarantine describes THIS load, not every load the object has ever done."""
    from bernstein.core.skills.sources import LocalDirSkillSource

    loader = SkillLoader([_ThrowingSource("broken", RuntimeError("boom"))])
    assert len(loader.quarantined) == 1

    loader._sources = [LocalDirSkillSource(sample_skills_root)]
    loader._reload()

    assert loader.quarantined == ()
    assert [s.name for s in loader.list_all()] == ["alpha", "beta", "gamma"]
