"""Tests for :mod:`bernstein.core.planning.role_resolver`."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.planning.role_resolver import (
    invalidate_cache,
    resolve_role_prompt,
)


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    invalidate_cache()


def _build_templates_dir(tmp_path: Path, *, with_skill: bool, with_legacy: bool) -> Path:
    """Create a ``templates/roles/`` + ``templates/skills/`` layout.

    Returns the ``roles`` path (resolver input).
    """
    templates = tmp_path / "templates"
    roles = templates / "roles"
    skills = templates / "skills"
    roles.mkdir(parents=True)
    skills.mkdir()

    if with_legacy:
        legacy_role = roles / "backend"
        legacy_role.mkdir()
        (legacy_role / "system_prompt.md").write_text(
            "# Legacy backend role\nOld long role prompt body.",
            encoding="utf-8",
        )

    if with_skill:
        skill_dir = skills / "backend"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            """---
name: backend
description: Backend skill description that clears twenty characters.
---
# Backend skill body""",
            encoding="utf-8",
        )

    return roles


def test_resolve_prefers_skill_when_available(tmp_path: Path) -> None:
    roles_dir = _build_templates_dir(tmp_path, with_skill=True, with_legacy=True)

    resolved = resolve_role_prompt(
        "backend",
        templates_dir=roles_dir,
        include_plugins=False,
    )

    assert resolved.source == "skill"
    assert resolved.skill_name == "backend"
    # Role hint names the skill and tells the agent to load_skill; the
    # full skill body is NOT inlined — agents fetch it on demand.
    assert "load_skill" in resolved.body
    assert "Role: backend" in resolved.body
    assert "Backend skill body" not in resolved.body


def test_resolve_falls_back_to_legacy_when_no_skill(tmp_path: Path) -> None:
    roles_dir = _build_templates_dir(tmp_path, with_skill=False, with_legacy=True)

    resolved = resolve_role_prompt(
        "backend",
        templates_dir=roles_dir,
        include_plugins=False,
    )

    assert resolved.source == "legacy"
    assert resolved.skill_name is None
    assert "Legacy backend role" in resolved.body


def test_resolve_falls_back_to_stub_when_nothing_matches(tmp_path: Path) -> None:
    roles_dir = _build_templates_dir(tmp_path, with_skill=False, with_legacy=False)

    resolved = resolve_role_prompt(
        "nonexistent-role",
        templates_dir=roles_dir,
        include_plugins=False,
    )

    assert resolved.source == "fallback"
    assert "nonexistent-role" in resolved.body


def test_resolve_supports_legacy_renderer_injection(tmp_path: Path) -> None:
    roles_dir = _build_templates_dir(tmp_path, with_skill=False, with_legacy=False)
    # Provide a custom renderer.

    def renderer(role: str, context: dict[str, str], templates_dir: Path) -> str:
        return f"rendered-{role}"

    resolved = resolve_role_prompt(
        "whatever",
        templates_dir=roles_dir,
        legacy_renderer=renderer,
        legacy_context={"x": "y"},
        include_plugins=False,
    )

    assert resolved.source == "legacy"
    assert resolved.body == "rendered-whatever"
