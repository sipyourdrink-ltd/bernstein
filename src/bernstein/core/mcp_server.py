"""Bernstein MCP server — thin shim that re-exports from bernstein.mcp.server.

Kept for backwards compatibility with any direct imports.
"""

from __future__ import annotations

from bernstein.mcp.server import (
    _DEFAULT_SERVER_URL,
    create_mcp_server,
    run_sse,
    run_stdio,
)

__all__ = ["_DEFAULT_SERVER_URL", "create_mcp_server", "run_sse", "run_stdio"]
