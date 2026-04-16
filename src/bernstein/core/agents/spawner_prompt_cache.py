"""Prompt caching utilities for system prompts and role templates.

Splits rendered prompts into cacheable (static) and dynamic blocks so
that adapters can apply provider-specific caching hints.  For example,
Anthropic's prompt caching API uses ``cache_control: {"type": "ephemeral"}``
on message blocks to cache the first marked prefix for 5 minutes.

The role template and coding standards are static across all tasks for a
given role, making them ideal cache candidates.  Task-specific sections
(assigned tasks, instructions, bulletin context) change per spawn and
must not be cached.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CacheableBlock:
    """A prompt segment annotated with cacheability.

    Attributes:
        content: The text content of this prompt block.
        cacheable: Whether this block is static and suitable for caching.
            Adapters use this hint to attach provider-specific cache
            directives (e.g. Anthropic's ``cache_control``).
    """

    content: str
    cacheable: bool


# Section headers that mark the boundary between static (cacheable) role
# context and dynamic per-task content.  Everything before the first
# occurrence of any of these markers is considered static.
_DYNAMIC_SECTION_MARKERS: frozenset[str] = frozenset(
    {
        "## Assigned tasks",
        "## Instructions",
        "## Team awareness",
        "## Persistent Memory",
        "## Relevant Code Context",
        "## File-scope context",
        "## Parent context",
        "## Predecessor context",
        "## Heartbeat",
        "## Token budget",
        "## Operational nudges",
    }
)


def mark_cacheable_prefix(prompt_parts: list[str]) -> list[CacheableBlock]:
    """Split prompt parts into cacheable (static) and dynamic blocks.

    The role template and coding standards that appear at the start of
    the prompt are stable across spawns for the same role.  This function
    identifies them as cacheable so adapters can instruct the provider to
    cache that prefix.

    The heuristic: scan *prompt_parts* in order.  Every part that does
    **not** contain a dynamic section marker is considered static
    (cacheable).  Once the first dynamic marker is encountered, all
    remaining parts are marked dynamic.

    Args:
        prompt_parts: Ordered list of prompt sections as returned by the
            prompt assembly logic (role prompt, specialist block,
            task block, lessons, etc.).

    Returns:
        List of :class:`CacheableBlock` instances preserving the original
        order, each annotated with ``cacheable=True`` (static prefix) or
        ``cacheable=False`` (dynamic suffix).
    """
    blocks: list[CacheableBlock] = []
    seen_dynamic = False

    for part in prompt_parts:
        if not part:
            continue
        if not seen_dynamic:
            # Check whether this part contains any dynamic marker.
            is_dynamic = any(marker in part for marker in _DYNAMIC_SECTION_MARKERS)
            if is_dynamic:
                seen_dynamic = True
                blocks.append(CacheableBlock(content=part, cacheable=False))
            else:
                blocks.append(CacheableBlock(content=part, cacheable=True))
        else:
            blocks.append(CacheableBlock(content=part, cacheable=False))

    return blocks
