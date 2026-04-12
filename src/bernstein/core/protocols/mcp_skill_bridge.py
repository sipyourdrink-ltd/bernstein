"""Bridge MCP servers to skill-like prompt objects (SkillDefinition).

Provides a write-once builder registry so that any part of the Bernstein
codebase can register a "builder" function for a named MCP server.  When
``collect_mcp_skills()`` is called it runs all registered builders and wraps
their output as ``SkillDefinition`` objects with
``source == SkillSource.MCP``.

Typical usage::

    from bernstein.core.mcp_skill_bridge import (
        register_skill_builder,
        collect_mcp_skills,
        build_skills_from_mcp_server,
    )

    # Register once at startup (e.g., from the MCP server module).
    register_skill_builder("bernstein", lambda: build_skills_from_mcp_server(mcp))

    # Later, collect all skills from all registered MCP servers.
    skills = collect_mcp_skills()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.skill_discovery import SkillDefinition, SkillSource

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MCPToolInfo — lightweight descriptor for a single MCP tool
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MCPToolInfo:
    """Lightweight descriptor for a single tool exported by an MCP server.

    Args:
        name: Tool name as advertised by the MCP server.
        description: Human-readable description of what the tool does.
        server_name: Name of the MCP server that owns this tool.
    """

    name: str
    description: str
    server_name: str


# ---------------------------------------------------------------------------
# Write-once builder registry
# ---------------------------------------------------------------------------

#: Maps ``server_name`` → callable that returns a list of ``MCPToolInfo``.
_BUILDERS: dict[str, Callable[[], list[MCPToolInfo]]] = {}


def register_skill_builder(server_name: str, builder: Callable[[], list[MCPToolInfo]]) -> None:
    """Register a builder function for *server_name* (write-once).

    If *server_name* is already registered the call is silently ignored so
    that module-level registration at import time is safe to repeat.

    Args:
        server_name: Unique identifier for the MCP server.
        builder: Zero-argument callable that returns a list of
            :class:`MCPToolInfo` objects when invoked.
    """
    if server_name in _BUILDERS:
        log.debug("Skill builder for %r already registered — skipping", server_name)
        return
    _BUILDERS[server_name] = builder
    log.debug("Registered skill builder for MCP server %r", server_name)


def collect_mcp_skills() -> dict[str, SkillDefinition]:
    """Run all registered builders and return a mapping of skills.

    Each :class:`MCPToolInfo` returned by the builders is converted into a
    :class:`~bernstein.core.skill_discovery.SkillDefinition` with
    ``source == SkillSource.MCP``.  The mapping key is the tool name.

    If a builder raises, the exception is caught, logged, and the server is
    skipped so that one faulty server does not block the others.

    Returns:
        Dict mapping tool name to :class:`SkillDefinition`.
    """
    result: dict[str, SkillDefinition] = {}
    for server_name, builder in _BUILDERS.items():
        try:
            tools = builder()
        except Exception:
            log.exception("Skill builder for MCP server %r raised an error — skipping", server_name)
            continue
        for tool in tools:
            skill = SkillDefinition(
                name=tool.name,
                description=tool.description,
                source=SkillSource.MCP,
                origin=f"mcp://{tool.server_name}/{tool.name}",
            )
            result[tool.name] = skill
    return result


# ---------------------------------------------------------------------------
# Convenience helper — extract tool info from a FastMCP instance
# ---------------------------------------------------------------------------


def build_skills_from_mcp_server(mcp_server: FastMCP) -> list[MCPToolInfo]:  # type: ignore[type-arg]
    """Extract :class:`MCPToolInfo` objects from a live FastMCP instance.

    Uses ``mcp_server._tool_manager.list_tools()`` to enumerate the tools
    registered on the server and converts each to a :class:`MCPToolInfo`.

    Args:
        mcp_server: A configured :class:`mcp.server.fastmcp.FastMCP` instance.

    Returns:
        List of :class:`MCPToolInfo` objects, one per registered tool.
    """
    server_name: str = getattr(mcp_server, "name", "unknown")
    tools = mcp_server._tool_manager.list_tools()  # pyright: ignore[reportPrivateUsage]
    infos: list[MCPToolInfo] = []
    for tool in tools:
        name: str = tool.name or ""
        description: str = tool.description or ""
        infos.append(MCPToolInfo(name=name, description=description, server_name=server_name))
    return infos
