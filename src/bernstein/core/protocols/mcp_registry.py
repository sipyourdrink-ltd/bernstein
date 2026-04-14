"""MCP server auto-discovery and per-task configuration.

Loads a catalog of known MCP servers from .sdd/config/mcp_servers.yaml,
detects which servers a task needs based on description keywords and file
patterns, checks that required environment variables are available, and
builds a per-agent MCP config dict ready for --mcp-config injection.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MCPServerEntry:
    """A known MCP server from the catalog.

    Attributes:
        name: Human-readable identifier (used as mcpServers key).
        package: npm package name for npx installation.
        capabilities: Capability tags for programmatic matching.
        keywords: Phrases in task descriptions that trigger this server.
        env_required: Environment variables that must be set.
        command: Executable to run.
        args: Arguments to pass. Defaults to ["-y", <package>].
        plugin_name: Plugin that registered this server. When set, the
            server key in mcpServers is prefixed as ``<plugin_name>__<name>``
            to prevent naming collisions across plugins.
    """

    name: str
    package: str
    capabilities: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    env_required: tuple[str, ...] = ()
    command: str = "npx"
    args: tuple[str, ...] | None = None
    plugin_name: str = ""

    @property
    def namespaced_name(self) -> str:
        """Return the plugin-scoped server name.

        When ``plugin_name`` is set, returns ``<plugin_name>__<name>`` to
        prevent collisions when multiple plugins provide servers with the
        same base name.  Otherwise returns the plain ``name``.
        """
        if self.plugin_name:
            return f"{self.plugin_name}__{self.name}"
        return self.name

    def env_available(self) -> bool:
        """Check if all required environment variables are set."""
        return all(os.environ.get(var) for var in self.env_required)

    def to_mcp_config(self) -> dict[str, Any]:
        """Build the mcpServers entry for this server.

        Returns:
            Dict with command and args suitable for MCP config.
        """
        args = list(self.args) if self.args else ["-y", self.package]
        config: dict[str, Any] = {"command": self.command, "args": args}

        # Include env vars that are set
        env_vals: dict[str, str] = {}
        for var in self.env_required:
            val = os.environ.get(var)
            if val:
                env_vals[var] = val
        if env_vals:
            config["env"] = env_vals

        return config

    def to_catalog_dict(self) -> dict[str, Any]:
        """Serialize the entry to the on-disk catalog format."""
        payload: dict[str, Any] = {
            "name": self.name,
            "package": self.package,
        }
        if self.capabilities:
            payload["capabilities"] = list(self.capabilities)
        if self.keywords:
            payload["keywords"] = list(self.keywords)
        if self.env_required:
            payload["env_required"] = list(self.env_required)
        if self.command != "npx":
            payload["command"] = self.command
        if self.args is not None:
            payload["args"] = list(self.args)
        return payload


def load_catalog_entries(path: Path) -> list[MCPServerEntry]:
    """Load MCP entries from a catalog path."""
    return MCPRegistry(config_path=path).servers


def save_catalog_entries(path: Path, servers: list[MCPServerEntry]) -> None:
    """Write MCP entries to the standard YAML catalog format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"servers": [server.to_catalog_dict() for server in servers]}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def upsert_catalog_entry(path: Path, entry: MCPServerEntry) -> tuple[list[MCPServerEntry], bool]:
    """Append or update a catalog entry by ``name`` without duplicating it.

    Args:
        path: Catalog file path.
        entry: Entry to insert or replace.

    Returns:
        Tuple of ``(updated_entries, created_new)``.
    """
    existing = load_catalog_entries(path) if path.exists() else []
    updated: list[MCPServerEntry] = []
    replaced = False
    for current in existing:
        if current.name == entry.name:
            updated.append(entry)
            replaced = True
        else:
            updated.append(current)
    if not replaced:
        updated.append(entry)
    save_catalog_entries(path, updated)
    return updated, not replaced


class MCPRegistry:
    """Registry of known MCP servers with auto-detection capabilities.

    Loads server definitions from a YAML catalog and provides methods to
    detect which servers a task needs and build per-agent MCP config.

    Args:
        config_path: Path to mcp_servers.yaml. If None or missing, registry is empty.
    """

    def __init__(self, config_path: Path | None = None) -> None:
        self._servers: list[MCPServerEntry] = []
        self._keyword_patterns: list[tuple[re.Pattern[str], MCPServerEntry]] = []
        if config_path is not None:
            self._load(config_path)

    @property
    def servers(self) -> list[MCPServerEntry]:
        """All registered MCP server entries."""
        return list(self._servers)

    def _load(self, path: Path) -> None:
        """Load server catalog from YAML file.

        Args:
            path: Path to mcp_servers.yaml.
        """
        if not path.exists():
            logger.debug("MCP server catalog not found at %s", path)
            return

        try:
            raw_data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Failed to load MCP server catalog: %s", exc)
            return

        if not isinstance(raw_data, dict):
            logger.warning("MCP server catalog must be a mapping, got %s", type(raw_data).__name__)
            return

        raw: dict[str, Any] = cast("dict[str, Any]", raw_data)

        entries_raw: Any = raw.get("servers", [])
        if not isinstance(entries_raw, list):
            logger.warning("MCP server catalog 'servers' must be a list")
            return

        entries_list: list[Any] = cast("list[Any]", entries_raw)
        for entry_raw in entries_list:
            if not isinstance(entry_raw, dict):
                continue
            entry_dict: dict[str, Any] = cast("dict[str, Any]", entry_raw)
            name: str | None = entry_dict.get("name")
            package: str | None = entry_dict.get("package")
            if not name or not package:
                logger.warning("MCP server entry missing name or package: %r", entry_dict)
                continue

            capabilities_raw: list[Any] = list(entry_dict.get("capabilities", []))
            keywords_raw: list[Any] = list(entry_dict.get("keywords", []))
            env_req_raw: list[Any] = list(entry_dict.get("env_required", []))
            entry = MCPServerEntry(
                name=str(name),
                package=str(package),
                capabilities=tuple(str(c) for c in capabilities_raw),
                keywords=tuple(str(k) for k in keywords_raw),
                env_required=tuple(str(e) for e in env_req_raw),
                command=str(entry_dict.get("command", "npx")),
                args=tuple(str(a) for a in entry_dict["args"]) if "args" in entry_dict else None,
            )
            self._servers.append(entry)

            # Pre-compile keyword patterns for efficient matching
            for keyword in entry.keywords:
                pattern = re.compile(re.escape(keyword), re.IGNORECASE)
                self._keyword_patterns.append((pattern, entry))

        logger.info("Loaded %d MCP server entries from catalog", len(self._servers))

    def detect_servers(
        self,
        task_description: str,
        owned_files: list[str] | None = None,
        requested_capabilities: list[str] | None = None,
    ) -> list[MCPServerEntry]:
        """Detect which MCP servers a task needs.

        Scans the task description for keyword matches and checks file
        extensions. Also matches explicit capability requests from manager.

        Args:
            task_description: Full task description text.
            owned_files: Files the task owns (for extension-based matching).
            requested_capabilities: Explicit capability requests from task metadata.

        Returns:
            De-duplicated list of matching MCPServerEntry objects.
        """
        matched: dict[str, MCPServerEntry] = {}
        self._match_by_keywords(task_description, matched)
        self._match_by_file_extensions(owned_files, matched)
        self._match_by_capabilities(requested_capabilities, matched)
        return list(matched.values())

    def _match_by_keywords(self, task_description: str, matched: dict[str, MCPServerEntry]) -> None:
        """Match servers by keyword patterns against the task description."""
        for pattern, entry in self._keyword_patterns:
            if entry.name not in matched and pattern.search(task_description):
                matched[entry.name] = entry

    def _match_by_file_extensions(self, owned_files: list[str] | None, matched: dict[str, MCPServerEntry]) -> None:
        """Match servers by file extension keywords in owned file paths."""
        if not owned_files:
            return
        file_text = " ".join(owned_files)
        for entry in self._servers:
            if entry.name in matched:
                continue
            for keyword in entry.keywords:
                if keyword.startswith(".") and keyword in file_text:
                    matched[entry.name] = entry
                    break

    def _match_by_capabilities(
        self, requested_capabilities: list[str] | None, matched: dict[str, MCPServerEntry]
    ) -> None:
        """Match servers by explicit capability requests."""
        if not requested_capabilities:
            return
        cap_set = set(requested_capabilities)
        for entry in self._servers:
            if entry.name not in matched and cap_set & set(entry.capabilities):
                matched[entry.name] = entry

    def filter_available(self, servers: list[MCPServerEntry]) -> list[MCPServerEntry]:
        """Filter servers to only those with required env vars available.

        Args:
            servers: Server entries to filter.

        Returns:
            Subset of servers where all env_required vars are set.
        """
        available: list[MCPServerEntry] = []
        for server in servers:
            if server.env_available():
                available.append(server)
            else:
                missing = [v for v in server.env_required if not os.environ.get(v)]
                logger.debug(
                    "MCP server '%s' skipped: missing env vars %s",
                    server.name,
                    missing,
                )
        return available

    def register_plugin_servers(self, plugin_name: str, servers: list[MCPServerEntry]) -> None:
        """Register MCP servers contributed by a plugin, namespacing their names.

        Each server is stored with ``plugin_name`` set so that
        :attr:`MCPServerEntry.namespaced_name` returns ``<plugin_name>__<name>``.
        This prevents collisions when two plugins provide servers with the same
        base name.

        Args:
            plugin_name: Plugin identifier used as the namespace prefix.
            servers: Server entries to register under this plugin's namespace.
        """
        for server in servers:
            namespaced = MCPServerEntry(
                name=server.name,
                package=server.package,
                capabilities=server.capabilities,
                keywords=server.keywords,
                env_required=server.env_required,
                command=server.command,
                args=server.args,
                plugin_name=plugin_name,
            )
            self._servers.append(namespaced)
            for keyword in namespaced.keywords:
                pattern = re.compile(re.escape(keyword), re.IGNORECASE)
                self._keyword_patterns.append((pattern, namespaced))

        if servers:
            logger.debug(
                "Registered %d MCP servers from plugin %r: %s",
                len(servers),
                plugin_name,
                [s.namespaced_name for s in self._servers[-len(servers) :]],
            )

    def build_mcp_config(self, servers: list[MCPServerEntry]) -> dict[str, Any] | None:
        """Build MCP config dict from a list of server entries.

        Args:
            servers: Server entries to include.

        Returns:
            Dict with {"mcpServers": {...}} structure, or None if empty.
        """
        if not servers:
            return None

        mcp_servers: dict[str, Any] = {}
        for server in servers:
            mcp_servers[server.namespaced_name] = server.to_mcp_config()

        return {"mcpServers": mcp_servers}

    def resolve_for_tasks(
        self,
        tasks: list[Task],
        base_config: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Detect, filter, and build MCP config for a task batch.

        Combines auto-detected servers with any existing base MCP config.
        Base config servers are preserved; auto-detected servers are merged
        in without overriding existing entries.

        Args:
            tasks: Batch of tasks to analyze.
            base_config: Existing MCP config (from bernstein.yaml / ~/.claude/mcp.json).

        Returns:
            Merged MCP config dict, or None if no servers needed.
        """
        # Combine descriptions from all tasks in batch
        combined_description = "\n".join(t.description for t in tasks)
        combined_files: list[str] = []
        for t in tasks:
            combined_files.extend(t.owned_files)

        # Detect and filter
        detected = self.detect_servers(combined_description, combined_files)
        available = self.filter_available(detected)

        if available:
            names = [s.name for s in available]
            logger.info("Auto-detected MCP servers for tasks: %s", names)

        auto_config = self.build_mcp_config(available)

        # Merge: base_config wins on conflicts (user explicitly configured those)
        if base_config is None and auto_config is None:
            return None
        if base_config is None:
            return auto_config
        if auto_config is None:
            return base_config

        # Both exist — merge auto into base (base takes precedence)
        merged_servers = dict(auto_config.get("mcpServers", {}))
        merged_servers.update(base_config.get("mcpServers", {}))
        return {"mcpServers": merged_servers}
