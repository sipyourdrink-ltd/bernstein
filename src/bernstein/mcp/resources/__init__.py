"""MCP resource registrars for Bernstein.

Each module registers a focused set of resources/tools on a ``MCPServer`` and
returns a bool indicating whether registration happened (so callers can
short-circuit when the feature is disabled).
"""
