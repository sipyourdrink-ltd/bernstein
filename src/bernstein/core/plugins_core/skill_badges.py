"""Agent skill badges — per-agent capability indicators for status display.

Provides skill proficiency badges (reasoning, tool support, code) that can be
rendered alongside worker badges in the status dashboard. Each adapter/model
combination maps to a pre-defined skill profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class SkillLevel(IntEnum):
    """Proficiency level for a skill badge.

    Integer-backed so levels are naturally orderable (NONE < BASIC < ... < EXPERT).
    """

    NONE = 0
    BASIC = 1
    PROFICIENT = 2
    EXPERT = 3


# Display mapping: filled vs hollow stars per level.
_STARS: dict[SkillLevel, str] = {
    SkillLevel.NONE: "\u2606\u2606\u2606",  # three hollow stars
    SkillLevel.BASIC: "\u2605\u2606\u2606",  # one filled, two hollow
    SkillLevel.PROFICIENT: "\u2605\u2605\u2606",  # two filled, one hollow
    SkillLevel.EXPERT: "\u2605\u2605\u2605",  # three filled
}


@dataclass(frozen=True)
class SkillBadge:
    """Single capability badge for an agent.

    Attributes:
        name: Human-readable skill name (e.g. ``reasoning``).
        level: Proficiency level.
        icon: Short icon/emoji used in compact display.
    """

    name: str
    level: SkillLevel
    icon: str


@dataclass(frozen=True)
class AgentSkillSet:
    """Complete set of skill badges for one agent.

    Attributes:
        agent_id: Unique agent identifier.
        adapter: Adapter name (``claude``, ``codex``, ``gemini``, etc.).
        model: Model identifier (``opus``, ``sonnet``, ``gpt-4``, etc.).
        badges: Ordered list of skill badges.
    """

    agent_id: str
    adapter: str
    model: str
    badges: list[SkillBadge]


def _badge(name: str, level: SkillLevel, icon: str) -> SkillBadge:
    """Shorthand constructor for profile definitions."""
    return SkillBadge(name=name, level=level, icon=icon)


# ---------------------------------------------------------------------------
# Default skill profiles keyed by ``adapter/model``.
#
# The key format is ``"<adapter>/<model>"``.  A fallback key ``"<adapter>/*"``
# is checked when the exact model is unknown.  The final fallback is
# ``"*/*"`` which yields BASIC across the board.
# ---------------------------------------------------------------------------

DEFAULT_SKILL_PROFILES: dict[str, list[SkillBadge]] = {
    # -- Claude family -------------------------------------------------------
    "claude/opus": [
        _badge("reasoning", SkillLevel.EXPERT, "\U0001f9e0"),
        _badge("tools", SkillLevel.EXPERT, "\U0001f527"),
        _badge("code", SkillLevel.EXPERT, "\U0001f4bb"),
    ],
    "claude/sonnet": [
        _badge("reasoning", SkillLevel.PROFICIENT, "\U0001f9e0"),
        _badge("tools", SkillLevel.EXPERT, "\U0001f527"),
        _badge("code", SkillLevel.EXPERT, "\U0001f4bb"),
    ],
    "claude/haiku": [
        _badge("reasoning", SkillLevel.BASIC, "\U0001f9e0"),
        _badge("tools", SkillLevel.PROFICIENT, "\U0001f527"),
        _badge("code", SkillLevel.PROFICIENT, "\U0001f4bb"),
    ],
    "claude/*": [
        _badge("reasoning", SkillLevel.PROFICIENT, "\U0001f9e0"),
        _badge("tools", SkillLevel.PROFICIENT, "\U0001f527"),
        _badge("code", SkillLevel.PROFICIENT, "\U0001f4bb"),
    ],
    # -- Codex family --------------------------------------------------------
    "codex/gpt-4": [
        _badge("reasoning", SkillLevel.PROFICIENT, "\U0001f9e0"),
        _badge("tools", SkillLevel.PROFICIENT, "\U0001f527"),
        _badge("code", SkillLevel.EXPERT, "\U0001f4bb"),
    ],
    "codex/o3": [
        _badge("reasoning", SkillLevel.EXPERT, "\U0001f9e0"),
        _badge("tools", SkillLevel.PROFICIENT, "\U0001f527"),
        _badge("code", SkillLevel.EXPERT, "\U0001f4bb"),
    ],
    "codex/*": [
        _badge("reasoning", SkillLevel.PROFICIENT, "\U0001f9e0"),
        _badge("tools", SkillLevel.PROFICIENT, "\U0001f527"),
        _badge("code", SkillLevel.PROFICIENT, "\U0001f4bb"),
    ],
    # -- Gemini family -------------------------------------------------------
    "gemini/pro": [
        _badge("reasoning", SkillLevel.PROFICIENT, "\U0001f9e0"),
        _badge("tools", SkillLevel.PROFICIENT, "\U0001f527"),
        _badge("code", SkillLevel.PROFICIENT, "\U0001f4bb"),
    ],
    "gemini/ultra": [
        _badge("reasoning", SkillLevel.EXPERT, "\U0001f9e0"),
        _badge("tools", SkillLevel.PROFICIENT, "\U0001f527"),
        _badge("code", SkillLevel.EXPERT, "\U0001f4bb"),
    ],
    "gemini/*": [
        _badge("reasoning", SkillLevel.BASIC, "\U0001f9e0"),
        _badge("tools", SkillLevel.BASIC, "\U0001f527"),
        _badge("code", SkillLevel.PROFICIENT, "\U0001f4bb"),
    ],
    # -- Catch-all fallback --------------------------------------------------
    "*/*": [
        _badge("reasoning", SkillLevel.BASIC, "\U0001f9e0"),
        _badge("tools", SkillLevel.BASIC, "\U0001f527"),
        _badge("code", SkillLevel.BASIC, "\U0001f4bb"),
    ],
}


def get_skill_set(adapter: str, model: str, agent_id: str) -> AgentSkillSet:
    """Look up the skill set for an adapter/model combination.

    Resolution order:
      1. ``"<adapter>/<model>"``   (exact match)
      2. ``"<adapter>/*"``         (adapter wildcard)
      3. ``"*/*"``                 (global fallback)

    Args:
        adapter: Adapter name (e.g. ``"claude"``).
        model: Model identifier (e.g. ``"opus"``).
        agent_id: Unique agent identifier.

    Returns:
        ``AgentSkillSet`` populated with the resolved badges.
    """
    key_exact = f"{adapter}/{model}"
    key_adapter = f"{adapter}/*"
    key_fallback = "*/*"

    badges = (
        DEFAULT_SKILL_PROFILES.get(key_exact)
        or DEFAULT_SKILL_PROFILES.get(key_adapter)
        or DEFAULT_SKILL_PROFILES[key_fallback]
    )

    return AgentSkillSet(
        agent_id=agent_id,
        adapter=adapter,
        model=model,
        badges=list(badges),
    )


def format_skill_badges(skill_set: AgentSkillSet) -> str:
    """Render skill badges as a Rich-formatted string.

    Produces output like ``reasoning[★★★] tools[★★☆] code[★★★]`` with
    Rich color markup.

    Args:
        skill_set: Agent skill set to format.

    Returns:
        Rich markup string suitable for console display.
    """
    parts: list[str] = []
    for badge in skill_set.badges:
        stars = _STARS[badge.level]
        parts.append(f"{badge.name}[bold]{stars}[/bold]")
    return " ".join(parts)
