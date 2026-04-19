"""Progressive-disclosure skill packs (oai-004).

A ``skill`` is a directory with a ``SKILL.md`` file (YAML frontmatter +
markdown body) and optional ``references/``, ``scripts/``, ``assets/``
siblings. Agents receive only a compact index (``name + description``) in
their system prompt and call ``load_skill(name=...)`` via MCP when they
decide a skill is relevant.

This replaces the old eager-loaded ``templates/roles/<role>.md`` model
while staying backwards compatible: when no skill pack exists for a role,
``SkillLoader.get_role_body`` returns ``None`` and the legacy role template
is used instead.

The package is organised as:

- :mod:`manifest`    — Pydantic model for ``SKILL.md`` frontmatter.
- :mod:`source`      — ``SkillSource`` / ``LazySkillSource`` ABCs.
- :mod:`sources.local_dir` — default loader (reads ``templates/skills/``).
- :mod:`sources.plugin`    — pluggy entry-point loader.
- :mod:`loader`      — ``SkillLoader`` orchestrator with conflict detection.
- :mod:`index_builder` — builds the compact index injected into prompts.
- :mod:`load_skill_tool` — ``load_skill`` MCP tool implementation.
"""

from __future__ import annotations

from bernstein.core.skills.index_builder import build_skill_index
from bernstein.core.skills.load_skill_tool import load_skill
from bernstein.core.skills.loader import (
    DuplicateSkillError,
    LoadedSkill,
    SkillLoader,
    SkillNotFoundError,
)
from bernstein.core.skills.manifest import SkillManifest, SkillManifestError
from bernstein.core.skills.source import LazySkillSource, SkillSource

__all__ = [
    "DuplicateSkillError",
    "LazySkillSource",
    "LoadedSkill",
    "SkillLoader",
    "SkillManifest",
    "SkillManifestError",
    "SkillNotFoundError",
    "SkillSource",
    "build_skill_index",
    "load_skill",
]
