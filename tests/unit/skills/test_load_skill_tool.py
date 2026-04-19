"""Tests for :func:`bernstein.core.skills.load_skill`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bernstein.core.skills.load_skill_tool import load_skill, result_as_dict
from bernstein.core.skills.loader import SkillLoader
from bernstein.core.skills.sources import LocalDirSkillSource


def _loader(root: Path) -> SkillLoader:
    return SkillLoader([LocalDirSkillSource(root)])


def test_load_skill_returns_body_and_available_paths(sample_skills_root: Path) -> None:
    result = load_skill(name="alpha", loader=_loader(sample_skills_root))

    assert result.error is None
    assert "Alpha skill" in result.body
    assert result.available_references == ["deep-dive.md"]
    assert result.available_scripts == ["hello.sh"]
    assert result.reference_content is None
    assert result.script_content is None


def test_load_skill_returns_reference_content(sample_skills_root: Path) -> None:
    result = load_skill(
        name="alpha",
        reference="deep-dive.md",
        loader=_loader(sample_skills_root),
    )

    assert result.error is None
    assert result.reference_content is not None
    assert "Deep dive" in result.reference_content


def test_load_skill_returns_script_content(sample_skills_root: Path) -> None:
    result = load_skill(
        name="alpha",
        script="hello.sh",
        loader=_loader(sample_skills_root),
    )

    assert result.error is None
    assert result.script_content is not None
    assert "#!/usr/bin/env bash" in result.script_content


def test_load_skill_reports_missing_reference(sample_skills_root: Path) -> None:
    result = load_skill(
        name="alpha",
        reference="missing.md",
        loader=_loader(sample_skills_root),
    )
    assert result.error is not None
    assert "missing.md" in result.error


def test_load_skill_reports_unknown_skill(sample_skills_root: Path) -> None:
    result = load_skill(name="nope", loader=_loader(sample_skills_root))
    assert result.error is not None
    assert "nope" in result.error


def test_load_skill_emits_wal_event(sample_skills_root: Path) -> None:
    events: list[dict[str, Any]] = []

    def sink(event: dict[str, Any]) -> None:
        events.append(event)

    load_skill(name="alpha", loader=_loader(sample_skills_root), wal_sink=sink)

    assert len(events) == 1
    event = events[0]
    assert event["event"] == "skill_loaded"
    assert event["name"] == "alpha"
    assert event["reference"] is None
    assert event["source"].startswith("local")


def test_load_skill_requires_loader_or_templates_dir() -> None:
    with pytest.raises(ValueError):
        load_skill(name="alpha")


def test_result_as_dict_is_json_serializable(sample_skills_root: Path) -> None:
    import json

    result = load_skill(name="alpha", loader=_loader(sample_skills_root))
    data = result_as_dict(result)
    # Should not raise.
    json.dumps(data)
    assert data["name"] == "alpha"
