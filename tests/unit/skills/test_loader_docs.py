"""Documentation contracts for direct skill-loader failure reporting."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from bernstein.core.skills.loader import QuarantinedSkill


def _sources_section() -> str:
    docs = Path("docs/architecture/skills.md").read_text(encoding="utf-8")
    return docs.split("## Sources\n", 1)[1].split("\n## ", 1)[0]


def test_sources_document_in_memory_failure_records() -> None:
    """Every public record field must be discoverable by library callers."""
    sources = _sources_section()
    normalized = " ".join(sources.split())

    assert "``SkillLoader.quarantined``" in sources
    assert "tuple of immutable ``QuarantinedSkill`` records in failure order" in normalized
    for field in fields(QuarantinedSkill):
        assert f"| ``{field.name}`` |" in sources


def test_sources_document_enumeration_and_artifact_failure_boundaries() -> None:
    """Do not imply generator iteration or all-or-nothing duplicate handling."""
    normalized = " ".join(_sources_section().split())

    assert "returns an eager ``list[SkillArtifact]`` (possibly empty), not a generator" in normalized
    assert "if ``iter_skills()`` raises, the whole source is quarantined" in normalized
    assert "``origin`` set to the source label and ``skill_name`` set to ``None``" in normalized
    assert "only that artifact is quarantined; remaining artifacts from the same source" in normalized
    assert "the first successfully registered definition is retained in source/artifact order" in normalized
    assert "conflicting definition produces a ``DuplicateSkillError`` record" in normalized


def test_sources_document_callback_timing_and_exception_isolation() -> None:
    """Library reporters must not be mistaken for a prerequisite for loading."""
    normalized = " ".join(_sources_section().split())

    assert "``on_quarantine: Callable[[QuarantinedSkill], None]``" in normalized
    assert "called once per newly quarantined entry" in normalized
    assert "after the record has been added to ``quarantined``" in normalized
    assert "Callback exceptions are caught and logged" in normalized
    assert "the record remains available and other skills continue loading" in normalized


def test_sources_distinguish_bare_and_configured_default_loaders() -> None:
    """Persistent retry policy must not be attributed to a bare loader."""
    normalized = " ".join(_sources_section().split())

    assert "A bare ``SkillLoader`` without a ``quarantine_store``" in normalized
    assert "does not persist disabled markers or journal events on its own" in normalized
    assert "Constructing another bare loader attempts its supplied sources again" in normalized
    assert "``default_loader_from_templates`` supplies a persistent quarantine store" in normalized
    assert "persists disabled markers and journals failures" in normalized
    assert "[re-enable](../operations/declaration-quarantine.md)" in normalized
    assert "Previously persisted entries also appear in ``quarantined``" in normalized
    assert "do not trigger a new ``on_quarantine`` callback" in normalized
