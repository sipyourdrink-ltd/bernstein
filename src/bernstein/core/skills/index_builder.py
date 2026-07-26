"""Render the compact skill index injected into agent system prompts.

The index is a flat markdown list of ``name: description`` entries with a
tiny header explaining how to load a skill. It deliberately stays small -
that's the whole point of progressive disclosure.

Callers (``spawn_prompt._render_prompt``) insert the returned string into
the ``role`` section in place of the full role body when a skill exists
for the role.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.skills.loader import LoadedSkill, SkillLoader

SKILL_INDEX_RESOURCE_URI = "bernstein://skills/index"


def build_skill_index(
    loader: SkillLoader,
    *,
    highlight: str | None = None,
    header: str = "Skills:",
) -> str:
    """Return a compact index string.

    Args:
        loader: Loaded skills registry.
        highlight: Optional skill name to pin to the top with a
            ``(primary)`` marker - the role-matched skill selected by
            ``role_resolver``.
        header: Override the leading header (lets us re-use the
            renderer for different channels).

    Returns:
        Multi-line string. The intentionally-terse format keeps per-spawn
        overhead minimal; every byte here multiplies by the number of
        spawned agents.

    Raises:
        SkillNotFoundError: When ``highlight`` names a skill the loader
            does not know about.
    """
    skills = loader.list_all()
    if not skills:
        return ""

    lines: list[str] = [header]

    highlighted: LoadedSkill | None = None
    if highlight is not None:
        # Access via .get so a missing highlight surfaces the loader's error.
        highlighted = loader.get(highlight)
        lines.append(f"* {highlighted.name}: {_fmt_entry(highlighted)}")

    for skill in skills:
        if highlighted is not None and skill.name == highlighted.name:
            continue
        lines.append(f"- {skill.name}: {_fmt_entry(skill)}")

    return "\n".join(lines) + "\n"


def _fmt_entry(skill: LoadedSkill) -> str:
    """Format a single index line.

    Intentionally bare - just the description. Reference / script counts
    are discoverable via ``load_skill`` when the agent decides to pull the
    body.
    """
    return skill.description.strip().replace("\n", " ")


def serialize_skill_discovery_index(loader: SkillLoader) -> str:
    """Return the deterministic JSON index exposed to MCP clients.

    Skill bodies stay behind ``load_skill(name=...)``. Their hashes let a
    client invalidate a cached body without putting that body in discovery
    output.
    """
    skills = [
        {
            "content_hash": hashlib.sha256(skill.body.encode("utf-8")).hexdigest(),
            "description": _fmt_entry(skill),
            "name": skill.name,
        }
        for skill in loader.list_all()
    ]
    return json.dumps({"skills": skills}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
