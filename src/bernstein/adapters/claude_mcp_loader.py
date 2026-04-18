"""MCP config loading and merging for the Claude Code adapter.

Extracted from :mod:`bernstein.adapters.claude` in audit-142.  Keeps the
loader and env-var resolver in a focused module so the adapter shell stays
readable.  The public entry points :func:`load_mcp_config` and
:func:`_resolve_env_vars` are re-exported from ``claude`` for backwards
compatibility with callers that imported them from the original location.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

# Shared cast-type constant to avoid string duplication (Sonar S1192).
_CAST_DICT_STR_ANY = "dict[str, Any]"


def load_mcp_config(
    project_servers: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build merged MCP config from user global config and project-level overrides.

    Reads ~/.claude/mcp.json (user's global MCP servers), then merges in any
    project-level mcp_servers from bernstein.yaml. Project config wins on conflicts.

    Args:
        project_servers: MCP server definitions from bernstein.yaml mcp_servers field.

    Returns:
        Merged MCP config dict ready for --mcp-config, or None if no servers found.
    """
    merged: dict[str, Any] = {}

    # 1. Read user global config (~/.claude/mcp.json)
    global_path = Path.home() / ".claude" / "mcp.json"
    if global_path.exists():
        try:
            global_cfg = json.loads(global_path.read_text(encoding="utf-8"))
            if isinstance(global_cfg, dict):
                # mcp.json has {"mcpServers": {...}} structure
                cfg = cast(_CAST_DICT_STR_ANY, global_cfg)
                servers = cfg.get("mcpServers", cfg)
                if isinstance(servers, dict):
                    merged.update(cast(_CAST_DICT_STR_ANY, servers))
        except (OSError, json.JSONDecodeError):
            pass  # Global MCP config unreadable; skip

    # 2. Merge project-level config (overrides global)
    if project_servers:
        # Expand env vars in server config values
        for name, server_def in project_servers.items():
            resolved = _resolve_env_vars(server_def)
            merged[name] = resolved

    if not merged:
        return None

    return {"mcpServers": merged}


def _resolve_env_vars(obj: Any) -> Any:
    """Recursively resolve ${VAR} references in config values."""
    if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
        var_name = obj[2:-1]
        return os.environ.get(var_name, obj)
    if isinstance(obj, dict):
        d = cast(_CAST_DICT_STR_ANY, obj)
        return {k: _resolve_env_vars(v) for k, v in d.items()}
    if isinstance(obj, list):
        lst = cast("list[Any]", obj)
        return [_resolve_env_vars(item) for item in lst]
    return obj


__all__ = ["_resolve_env_vars", "load_mcp_config"]
