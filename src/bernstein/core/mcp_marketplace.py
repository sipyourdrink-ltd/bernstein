"""Bundled MCP marketplace entries for one-command installation."""

from __future__ import annotations

from bernstein.core.mcp_registry import MCPServerEntry

MARKETPLACE_ENTRIES: tuple[MCPServerEntry, ...] = (
    MCPServerEntry(
        name="filesystem",
        package="@modelcontextprotocol/server-filesystem",
        capabilities=("filesystem", "files", "read-write"),
        keywords=(".py", ".ts", ".md", ".yaml"),
        args=("-y", "@modelcontextprotocol/server-filesystem", "."),
    ),
    MCPServerEntry(
        name="github",
        package="@modelcontextprotocol/server-github",
        capabilities=("github", "issues", "pull-requests"),
        keywords=("github", "issue", "pull request", "repository"),
        env_required=("GITHUB_PERSONAL_ACCESS_TOKEN",),
    ),
    MCPServerEntry(
        name="fetch",
        package="mcp-server-fetch",
        capabilities=("web", "http", "fetch"),
        keywords=("url", "web", "http", "fetch"),
    ),
    MCPServerEntry(
        name="memory",
        package="@modelcontextprotocol/server-memory",
        capabilities=("memory", "knowledge-base"),
        keywords=("memory", "notes", "knowledge"),
    ),
)


def marketplace_entries() -> list[MCPServerEntry]:
    """Return the bundled marketplace catalog."""
    return list(MARKETPLACE_ENTRIES)


def marketplace_entry(name: str) -> MCPServerEntry | None:
    """Return a marketplace entry by name."""
    normalized = name.strip().lower()
    for entry in MARKETPLACE_ENTRIES:
        if entry.name.lower() == normalized:
            return entry
    return None
