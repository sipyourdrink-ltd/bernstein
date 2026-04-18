"""Anthropic API cache-control block builder for the Claude Code adapter.

Extracted from :mod:`bernstein.adapters.claude` in audit-142.  The builder
is a pure function and has no side effects, so moving it out keeps the
adapter shell focused on spawn orchestration.  The public entry point
:func:`build_cacheable_system_blocks` is re-exported from ``claude`` for
backwards compatibility.
"""

from __future__ import annotations

from typing import Any


def build_cacheable_system_blocks(
    system_addendum: str,
) -> list[dict[str, Any]]:
    """Build Anthropic API system message blocks with cache control hints.

    Wraps the static system addendum (role template + coding standards) in
    a content block with ``cache_control: {"type": "ephemeral"}``.  When
    used with the Anthropic Messages API, this instructs the provider to
    cache the block for up to 5 minutes, reducing input token costs for
    repeated spawns with the same role.

    The Claude Code CLI handles caching transparently when content is
    passed via ``--append-system-prompt``.  This function is provided for
    adapters that call the API directly or for future Claude Code CLI
    versions that support explicit cache control.

    Args:
        system_addendum: Static system prompt content to mark as cacheable.

    Returns:
        List of Anthropic API content blocks.  If *system_addendum* is
        non-empty, the block includes ``cache_control``.  Returns an
        empty list if the addendum is empty.
    """
    if not system_addendum:
        return []
    return [
        {
            "type": "text",
            "text": system_addendum,
            "cache_control": {"type": "ephemeral"},
        }
    ]


__all__ = ["build_cacheable_system_blocks"]
