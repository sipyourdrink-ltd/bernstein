"""Shared fixtures for skill-pack tests.

Each test gets a fresh ``tmp_path/skills`` root populated with one or more
synthetic skill packs. We keep fixtures tiny and deterministic so the
token-reduction regression test has stable numbers.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str,
    body: str,
    references: dict[str, str] | None = None,
    scripts: dict[str, str] | None = None,
    trigger_keywords: list[str] | None = None,
) -> Path:
    """Materialise a skill directory under ``root`` for the test."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    front_lines = [
        "---",
        f"name: {name}",
        "description: >-",
        f"  {description}",
    ]
    if trigger_keywords:
        front_lines.append("trigger_keywords:")
        for kw in trigger_keywords:
            front_lines.append(f"  - {kw}")
    if references:
        front_lines.append("references:")
        for ref_name in references:
            front_lines.append(f"  - {ref_name}")
    if scripts:
        front_lines.append("scripts:")
        for script_name in scripts:
            front_lines.append(f"  - {script_name}")
    front_lines.append("---")
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "\n".join(front_lines) + "\n\n" + body.strip() + "\n",
        encoding="utf-8",
    )

    if references:
        (skill_dir / "references").mkdir(exist_ok=True)
        for ref_name, content in references.items():
            (skill_dir / "references" / ref_name).write_text(content, encoding="utf-8")

    if scripts:
        (skill_dir / "scripts").mkdir(exist_ok=True)
        for script_name, content in scripts.items():
            (skill_dir / "scripts" / script_name).write_text(content, encoding="utf-8")

    return skill_dir


@pytest.fixture
def write_skill() -> WriteSkillCallable:
    """Return the helper for tests to create synthetic skills."""
    return _write_skill


@pytest.fixture
def sample_skills_root(tmp_path: Path) -> Path:
    """Populate ``tmp_path/skills`` with three tiny skills.

    Tests that need a ready-to-load tree use this instead of wiring up
    fixtures by hand.
    """
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(
        root,
        "alpha",
        description="Alpha skill — simple test skill with references and scripts.",
        body=textwrap.dedent(
            """
            # Alpha skill
            You are the alpha skill. Use references for deeper guidance.
            """
        ),
        references={
            "deep-dive.md": "# Deep dive for alpha\nDetailed content.",
        },
        scripts={
            "hello.sh": "#!/usr/bin/env bash\necho hello from alpha\n",
        },
        trigger_keywords=["alpha", "test"],
    )
    _write_skill(
        root,
        "beta",
        description="Beta skill — second skill without references.",
        body="# Beta skill body",
    )
    _write_skill(
        root,
        "gamma",
        description="Gamma skill — third skill used for index testing only.",
        body="# Gamma skill body",
    )
    return root


class WriteSkillCallable:
    """Type alias for the ``write_skill`` fixture."""

    def __call__(  # pragma: no cover — typing helper only
        self,
        root: Path,
        name: str,
        *,
        description: str,
        body: str,
        references: dict[str, str] | None = None,
        scripts: dict[str, str] | None = None,
        trigger_keywords: list[str] | None = None,
    ) -> Path: ...
