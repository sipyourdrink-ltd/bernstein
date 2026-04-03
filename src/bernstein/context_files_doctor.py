"""Context warnings for bernstein doctor -- detect stale configs, bad files, MCP issues.

Checks CLAUDE.md/agents for parse errors, MCP server reachability,
and permission rules for unreachable conditions.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)


@dataclass
class DoctorWarning:
    """A single doctor check result."""

    name: str
    ok: bool
    detail: str
    fix: str = ""


# ---------------------------------------------------------------------------
# Context file parsing
# ---------------------------------------------------------------------------

# Known context files and their expected formats
_CONTEXT_FILES = [
    ("CLAUDE.md", "markdown"),
    ("CLAUDE.local.md", "markdown"),
    ("AGENTS.md", "markdown"),
    (".claude/settings.json", "json"),
    (".claude/settings.local.json", "json"),
]


def check_context_files(workdir: Path) -> list[DoctorWarning]:
    """Check context files for existence, size, and parse errors.

    Args:
        workdir: Project root directory.

    Returns:
        List of DoctorWarning results.
    """
    results: list[DoctorWarning] = []

    for rel_path, fmt in _CONTEXT_FILES:
        fpath = workdir / rel_path
        if not fpath.exists():
            continue

        try:
            content = fpath.read_text(encoding="utf-8")
        except Exception as exc:
            results.append(
                DoctorWarning(
                    name=f"Context file: {rel_path}",
                    ok=False,
                    detail=f"unreadable: {exc}",
                    fix=f"Fix permissions or recreate {rel_path}",
                )
            )
            continue

        if not content.strip():
            results.append(
                DoctorWarning(
                    name=f"Context file: {rel_path}",
                    ok=False,
                    detail="file is empty",
                    fix=f"Add content to {rel_path} or remove it",
                )
            )
            continue

        if fmt == "json":
            try:
                json.loads(content)
            except json.JSONDecodeError as exc:
                results.append(
                    DoctorWarning(
                        name=f"Context file: {rel_path}",
                        ok=False,
                        detail=f"invalid JSON: {exc}",
                        fix=f"Fix JSON syntax in {rel_path}",
                    )
                )

    # Check for role template references that don't exist
    templates_dir = workdir / "templates" / "roles"
    if templates_dir.exists():
        template_files = list(templates_dir.glob("*.md"))
        if not template_files:
            results.append(
                DoctorWarning(
                    name="Role templates",
                    ok=False,
                    detail="templates/roles/ exists but contains no .md files",
                    fix="Add role template files to templates/roles/",
                )
            )

    # Warn about very large context files that may cause token issues
    for rel_path, _fmt in _CONTEXT_FILES:
        fpath = workdir / rel_path
        if fpath.exists():
            size_bytes = fpath.stat().st_size
            if size_bytes > 100_000:  # 100 KB
                size_kb = size_bytes / 1024
                results.append(
                    DoctorWarning(
                        name=f"Context file: {rel_path}",
                        ok=False,
                        detail=f"large file ({size_kb:.0f} KB) -- may consume significant context tokens",
                        fix=f"Consider splitting or trimming {rel_path}",
                    )
                )

    if not results:
        results.append(
            DoctorWarning(
                name="Context files",
                ok=True,
                detail="all present and well-formed",
            )
        )

    return results


# ---------------------------------------------------------------------------
# MCP server reachability
# ---------------------------------------------------------------------------


def check_mcp_servers(workdir: Path) -> list[DoctorWarning]:
    """Check MCP server configuration and reachability.

    Args:
        workdir: Project root directory.

    Returns:
        List of DoctorWarning results.
    """
    results: list[DoctorWarning] = []

    mcp_paths = [
        workdir / ".claude" / "settings.json",
        workdir / ".claude" / "settings.local.json",
        workdir / ".claude" / "mcp_settings.json",
    ]

    mcp_servers: dict[str, dict[str, Any]] = {}
    for mcp_path in mcp_paths:
        if mcp_path.exists():
            try:
                raw = json.loads(mcp_path.read_text(encoding="utf-8"))
                _collect_mcp_servers(raw, mcp_servers)
            except Exception:
                pass

    if not mcp_servers:
        results.append(
            DoctorWarning(
                name="MCP servers",
                ok=True,
                detail="none configured",
            )
        )
        return results

    for server_name, server_cfg in mcp_servers.items():
        command = str(server_cfg.get("command", ""))
        if not command:
            results.append(
                DoctorWarning(
                    name=f"MCP server: {server_name}",
                    ok=False,
                    detail="no command specified",
                    fix=f"Add a 'command' field for MCP server {server_name}",
                )
            )
            continue

        binary = command.split()[0]
        found = shutil.which(binary) is not None

        if not found and "/" in binary:
            bin_path = Path(binary)
            found = bin_path.exists() if bin_path.is_absolute() else (workdir / bin_path).exists()

        if found:
            missing = _check_mcp_env_credentials(server_cfg)
            if missing:
                results.append(
                    DoctorWarning(
                        name=f"MCP server: {server_name}",
                        ok=False,
                        detail=f"missing environment variables: {', '.join(missing)}",
                        fix=f"Set {', '.join(missing)} before running Bernstein",
                    )
                )
            else:
                results.append(
                    DoctorWarning(
                        name=f"MCP server: {server_name}",
                        ok=True,
                        detail=f"command '{command}' found",
                    )
                )
        else:
            results.append(
                DoctorWarning(
                    name=f"MCP server: {server_name}",
                    ok=False,
                    detail=f"command '{binary}' not found in PATH",
                    fix=f"Install '{binary}' or fix the command path for {server_name}",
                )
            )

    return results


# ---------------------------------------------------------------------------
# Permission rule health
# ---------------------------------------------------------------------------


def check_permission_rules(workdir: Path) -> list[DoctorWarning]:
    """Check .claude/ permission rules for unreachable conditions.

    Args:
        workdir: Project root directory.

    Returns:
        List of DoctorWarning results.
    """
    results: list[DoctorWarning] = []

    settings_paths = [
        workdir / ".claude" / "settings.json",
        workdir / ".claude" / "settings.local.json",
    ]

    for settings_path in settings_paths:
        if not settings_path.exists():
            continue

        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        env_obj = _get_dict(data, "env")
        if env_obj is None:
            continue

        allow: list[str] = _str_list(env_obj, "allow")
        deny: list[str] = _str_list(env_obj, "deny")
        allow_raw = env_obj.get("allow")
        deny_raw = env_obj.get("deny")

        if not isinstance(allow_raw, list) or not isinstance(deny_raw, list):
            results.append(
                DoctorWarning(
                    name=f"Permission rules: {settings_path.name}",
                    ok=False,
                    detail="env.allow and env.deny must be arrays",
                    fix=f"Fix JSON structure in {settings_path.name}",
                )
            )
            continue

        # Wildcard deny that blocks everything
        for rule in deny:
            if rule in ("*", "*/*") and not allow:
                results.append(
                    DoctorWarning(
                        name=f"Permission rules: {settings_path.name}",
                        ok=False,
                        detail=f"dry-run: deny='{rule}' with no allow rules blocks everything",
                        fix=f"Remove or refine deny rule '{rule}' in {settings_path.name}",
                    )
                )

        # Negative allow patterns
        for rule in allow:
            if rule.startswith("!"):
                results.append(
                    DoctorWarning(
                        name=f"Permission rules: {settings_path.name}",
                        ok=False,
                        detail=f"negative allow pattern: '{rule}' -- may cause unexpected blocks",
                        fix=f"Replace negative allow with explicit deny for '{rule[1:]}'",
                    )
                )

    if not results:
        results.append(
            DoctorWarning(
                name="Permission rules",
                ok=True,
                detail="no issues detected",
            )
        )

    return results


# ---------------------------------------------------------------------------
# Private helpers -- isolated to contain JSON-typing complexity
# These functions work with JSON-parsed dicts whose key/value types pyright
# cannot statically determine. Runtime isinstance guards are sufficient.
# pyright: reportUnknownVariableType=false
# ---------------------------------------------------------------------------

_SECRETS_PATTERNS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")


def _collect_mcp_servers(data: dict[str, Any], out: dict[str, dict[str, Any]]) -> None:
    """Recursively pull mcpServers dicts from parsed JSON."""
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return
    for k in servers:
        if not isinstance(k, str):
            continue
        v = servers[k]
        if isinstance(v, dict):
            out[k] = v


def _check_mcp_env_credentials(cfg: dict[str, Any]) -> list[str]:
    """Return list of missing env-var keys that look like secrets."""
    env_raw: Any = cfg.get("env")
    if not isinstance(env_raw, dict):
        return []
    env: dict[str, Any] = cast("dict[str, Any]", env_raw)
    missing: list[str] = []
    for ek in env:
        upper = ek.upper()
        has_secret = any(p in upper for p in _SECRETS_PATTERNS)
        if has_secret and not os.environ.get(ek) and not env.get(ek):
            missing.append(ek)
    return missing


def _get_dict(data: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Safely extract a dict value from parsed JSON."""
    val = data.get(key)
    if isinstance(val, dict):
        return val
    return None


def _str_list(obj: dict[str, Any], key: str) -> list[str]:
    """Extract a list of strings from a config dict."""
    val = obj.get(key)
    if isinstance(val, list):
        return [str(item) for item in val if isinstance(item, str)]
    return []
