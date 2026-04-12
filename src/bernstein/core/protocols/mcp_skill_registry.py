"""Write-once MCP skill registry — bridges MCP server tools to skill discovery.

MCP servers register skills by calling :func:`register_mcp_skills` once.
The registry is intentionally write-once (a second registration for the same
server name is silently ignored) to prevent import-cycle side-effects from
double-loading modules.

Usage (from an MCP server module)::

    from bernstein.core.mcp_skill_registry import (
        build_mcp_skills_from_tools,
        register_mcp_skills,
    )

    tools = [{"name": "search-issues", "description": "Search GitHub issues."}]
    skills = build_mcp_skills_from_tools("github-server", tools)
    register_mcp_skills("github-server", skills)

Usage (from SkillResolver)::

    from bernstein.core.mcp_skill_registry import get_mcp_skills

    mcp_skills = get_mcp_skills()  # dict[str, SkillDefinition]
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.skill_discovery import SkillDefinition

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Write-once registry: server_name → list of SkillDefinition
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, list[SkillDefinition]] = {}


def register_mcp_skills(server_name: str, skills: list[SkillDefinition]) -> None:
    """Register skills from an MCP server (write-once per server name).

    Write-once: if *server_name* is already registered, the call is silently
    ignored.  This prevents double-registration when the same MCP server
    module is imported via different code paths (circular import protection).

    Args:
        server_name: Unique identifier for the MCP server.
        skills: List of SkillDefinition objects to register.
    """
    if server_name in _REGISTRY:
        log.debug(
            "mcp_skill_registry: server %r already registered — ignoring",
            server_name,
        )
        return
    _REGISTRY[server_name] = list(skills)
    log.debug(
        "mcp_skill_registry: registered %d skill(s) for server %r",
        len(skills),
        server_name,
    )


def get_mcp_skills() -> dict[str, SkillDefinition]:
    """Return a flat dict of all MCP-registered skills, keyed by skill name.

    When two servers provide a skill with the same name, the first-registered
    server wins (matches the discovery priority model in :mod:`skill_discovery`).

    Returns:
        Mapping of skill name → :class:`~bernstein.core.skill_discovery.SkillDefinition`
        for all registered MCP skills.
    """
    merged: dict[str, SkillDefinition] = {}
    for server_name, skills in _REGISTRY.items():
        for skill in skills:
            if skill.name not in merged:
                merged[skill.name] = skill
            else:
                log.debug(
                    "mcp_skill_registry: skill %r from %r dropped (already registered)",
                    skill.name,
                    server_name,
                )
    return merged


def clear_registry() -> None:
    """Clear all registered MCP skills.

    Intended for use in tests only — do not call from production code.
    """
    _REGISTRY.clear()


# ---------------------------------------------------------------------------
# Skill builder
# ---------------------------------------------------------------------------


def build_mcp_skills_from_tools(
    server_name: str,
    tools: list[dict[str, Any]],
) -> list[SkillDefinition]:
    """Build :class:`~bernstein.core.skill_discovery.SkillDefinition` objects from MCP tool descriptors.

    Converts MCP tool metadata (``{"name": ..., "description": ...}``) into
    SkillDefinition objects with ``source=SkillSource.MCP``.  Extra fields are
    preserved in :attr:`~bernstein.core.skill_discovery.SkillDefinition.metadata`.

    The import of :mod:`skill_discovery` is deferred to runtime to avoid
    circular imports at module load time.

    Args:
        server_name: Name of the MCP server providing the tools.
        tools: List of tool descriptor dicts with at least a ``name`` key and
            an optional ``description`` key.

    Returns:
        List of SkillDefinition objects ready to pass to
        :func:`register_mcp_skills`.
    """
    # Deferred import: avoids circular dependency when mcp_skill_registry is
    # imported by code that skill_discovery.py also imports.
    from bernstein.core.skill_discovery import SkillDefinition, SkillSource

    skills: list[SkillDefinition] = []
    origin = f"mcp://{server_name}"
    for tool in tools:
        name = str(tool.get("name", "")).strip()
        if not name:
            continue
        description = str(tool.get("description", "")).strip()
        metadata: dict[str, Any] = {k: v for k, v in tool.items() if k not in {"name", "description"}}
        skills.append(
            SkillDefinition(
                name=name,
                description=description,
                source=SkillSource.MCP,
                origin=origin,
                metadata=metadata,
            )
        )
    return skills
