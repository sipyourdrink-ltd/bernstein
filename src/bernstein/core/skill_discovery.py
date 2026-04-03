"""Skill discovery priority order — 6-level cascade (T797).

Skills are loaded from multiple sources in priority order. Later sources
cannot override skills already loaded from earlier (higher-priority) sources.

Discovery priority (highest to lowest):
    1. **managed** — MDM/enterprise-managed skills that enforce org policy
    2. **user** — per-user skills in ``~/.bernstein/skills/``
    3. **project** — project-level skills in ``.bernstein/skills/``
    4. **additional** — extra skills declared via config/YAML
    5. **plugin** — skills bundled with installed plugins
    6. **mcp** — skills advertised by MCP servers

This mirrors Claude Code's ``loadSkillsDir.ts`` priority chain:
managed > user > project > additional > plugin > MCP.

Usage:
    >>> resolver = SkillResolver(workdir=Path.cwd())
    >>> loaded = resolver.resolve()
    >>> len(loaded)  # merged set, earlier sources win on conflict
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Skill source priority
# ---------------------------------------------------------------------------


class SkillSource(Enum):
    """Discovery priority level for a skill, highest to lowest."""

    MANAGED = "managed"
    USER = "user"
    PROJECT = "project"
    ADDITIONAL = "additional"
    PLUGIN = "plugin"
    MCP = "mcp"

    @property
    def sort_key(self) -> int:
        """Lower = higher priority (sort order for discovery)."""
        return {
            SkillSource.MANAGED: 0,
            SkillSource.USER: 1,
            SkillSource.PROJECT: 2,
            SkillSource.ADDITIONAL: 3,
            SkillSource.PLUGIN: 4,
            SkillSource.MCP: 5,
        }[self]


# ---------------------------------------------------------------------------
# Skill metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillDefinition:
    """A single skill description."""

    name: str
    description: str
    source: SkillSource
    origin: str  # Path or URL where the skill was loaded from
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def priority_key(self) -> str:
        """Dedup key: skill name + source priority."""
        return f"{self.source.sort_key}:{self.name}"


@dataclass
class SkillDiscoveryResult:
    """Full result of skill discovery across all sources."""

    skills: dict[str, SkillDefinition]
    conflicts: list[dict[str, Any]]
    warnings: list[str]


# ---------------------------------------------------------------------------
# Discovery resolver
# ---------------------------------------------------------------------------

#: Default directory names under which skill JSON/YAML files are discovered.
_SKILL_DIRS: list[str] = ["skills"]


class SkillResolver:
    """Resolve skills from multiple sources with priority ordering (T797).

    Args:
        workdir: Project working directory. Used to locate project-level
            skills and config.  Falls back to ``Path.cwd()`` if not given.
        home_dir: Home directory for user-level skills. Defaults to
            ``Path.home()``.
        managed_dir: Optional path to MDM/enterprise-managed skills.
        additional_dirs: Extra directories to scan for skills.
        plugin_skills: Skills discovered from installed plugins (name to SkillDefinition).
        mcp_skills: Skills advertised by connected MCP servers.
    """

    def __init__(
        self,
        workdir: Path | None = None,
        home_dir: Path | None = None,
        managed_dir: Path | None = None,
        additional_dirs: list[Path] | None = None,
        plugin_skills: dict[str, SkillDefinition] | None = None,
        mcp_skills: dict[str, SkillDefinition] | None = None,
    ) -> None:
        self.workdir = workdir or Path.cwd()
        self.home_dir = home_dir or Path.home()
        self.managed_dir = managed_dir
        self.additional_dirs = additional_dirs or []
        self.plugin_skills = plugin_skills or {}
        self.mcp_skills = mcp_skills or {}

    def resolve(self) -> SkillDiscoveryResult:
        """Discover skills from all sources, resolving conflicts by priority.

        Returns:
            SkillDiscoveryResult with merged skills, conflicts, and warnings.
        """
        merged: dict[str, SkillDefinition] = {}
        conflicts: list[dict[str, Any]] = []
        warnings: list[str] = []

        for source in SkillSource:
            skills = self._discover_source(source, warnings)
            for skill in skills:
                if skill.name in merged:
                    existing = merged[skill.name]
                    conflicts.append(
                        {
                            "skill": skill.name,
                            "winner": existing.source.value,
                            "loser": skill.source.value,
                            "winner_origin": existing.origin,
                            "loser_origin": skill.origin,
                        }
                    )
                    log.debug(
                        "Skill %r from %s ignored (already loaded from %s)",
                        skill.name,
                        skill.source.value,
                        existing.source.value,
                    )
                else:
                    merged[skill.name] = skill

        return SkillDiscoveryResult(
            skills=merged,
            conflicts=conflicts,
            warnings=warnings,
        )

    def _discover_source(
        self,
        source: SkillSource,
        warnings: list[str],
    ) -> list[SkillDefinition]:
        """Discover skills from a single source level."""
        if source == SkillSource.MANAGED:
            return self._discover_managed(warnings)
        if source == SkillSource.USER:
            return self._discover_user(warnings)
        if source == SkillSource.PROJECT:
            return self._discover_project(warnings)
        if source == SkillSource.ADDITIONAL:
            return self._discover_additional(warnings)
        if source == SkillSource.PLUGIN:
            return list(self.plugin_skills.values())
        if source == SkillSource.MCP:
            return list(self.mcp_skills.values())
        return []

    # ------------------------------------------------------------------
    # Discovery implementations
    # ------------------------------------------------------------------

    def _discover_managed(self, warnings: list[str]) -> list[SkillDefinition]:
        """Load skills from the managed/MDM skill directory."""
        if self.managed_dir is None:
            return []
        return self._scan_skill_dir(self.managed_dir, SkillSource.MANAGED, warnings)

    def _discover_user(self, warnings: list[str]) -> list[SkillDefinition]:
        """Load skills from ``~/.bernstein/skills/``."""
        user_dir = self.home_dir / ".bernstein" / "skills"
        return self._scan_skill_dir(user_dir, SkillSource.USER, warnings)

    def _discover_project(self, warnings: list[str]) -> list[SkillDefinition]:
        """Load skills from ``.bernstein/skills/`` in the workdir."""
        project_dir = self.workdir / ".bernstein" / "skills"
        return self._scan_skill_dir(project_dir, SkillSource.PROJECT, warnings)

    def _discover_additional(self, warnings: list[str]) -> list[SkillDefinition]:
        """Load skills from additional config directories."""
        skills: list[SkillDefinition] = []
        for d in self.additional_dirs:
            skills.extend(self._scan_skill_dir(d, SkillSource.ADDITIONAL, warnings))
        return skills

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _scan_skill_dir(
        self,
        directory: Path,
        source: SkillSource,
        warnings: list[str],
    ) -> list[SkillDefinition]:
        """Scan a directory for skill definition files.

        Skills are loaded from ``*.skill.json`` files.  Each file should
        contain a JSON object with at least ``name`` and ``description`` keys.

        Args:
            directory: Directory to scan.
            source: The SkillSource this directory belongs to.
            warnings: List to append load warnings to.

        Returns:
            List of successfully loaded SkillDefinition objects.
        """
        if not directory.is_dir():
            return []

        skills: list[SkillDefinition] = []
        for skill_file in sorted(directory.glob("*.skill.json")):
            try:
                data = json.loads(skill_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                warnings.append(f"Failed to read skill file {skill_file}: {exc}")
                continue

            name = data.get("name")
            description = data.get("description", "")
            if not name:
                warnings.append(f"Skill file {skill_file} missing 'name' field")
                continue

            meta = {k: v for k, v in data.items() if k not in {"name", "description"}}
            skills.append(
                SkillDefinition(
                    name=name,
                    description=description,
                    source=source,
                    origin=str(skill_file),
                    metadata=meta,
                )
            )

        return skills
