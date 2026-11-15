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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

_STDIO_DEFAULT_COMMAND = "npx"
_REMOTE_TRANSPORTS: frozenset[str] = frozenset({"http", "sse"})
_TRANSPORT_ALIASES: dict[str, str] = {
    "streamable-http": "http",
    "streamable_http": "http",
}
_ENV_REF_PATTERN: re.Pattern[str] = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SENSITIVE_HEADER_NAME_RE: re.Pattern[str] = re.compile(
    r"(?:^|[-_])(auth(?:orization)?|token|secret|api[-_]?key|key|password|passwd|credential|cookie)(?:$|[-_])",
    re.IGNORECASE,
)
_CREDENTIAL_SCHEME_RE: re.Pattern[str] = re.compile(
    r"^\s*(?:bearer|basic|token|apikey|digest)\s+(.+?)\s*$",
    re.IGNORECASE,
)
_SECRET_VALUE_PREFIXES: tuple[str, ...] = (
    "sk-",
    "pk-",
    "ghp_",
    "gho_",
    "ghs_",
    "ghu_",
    "github_pat_",
    "xoxb-",
    "xoxp-",
    "AIza",
    "AKIA",
    "glpat-",
    "npm_",
    "eyJ",
)


def _normalize_transport(raw_transport: str | None, *, has_url: bool) -> str:
    """Normalize and validate the transport name for a catalog entry."""
    if raw_transport is None:
        return "http" if has_url else "stdio"
    normalized = _TRANSPORT_ALIASES.get(raw_transport.strip().lower(), raw_transport.strip().lower())
    if has_url:
        if normalized not in _REMOTE_TRANSPORTS:
            raise ValueError(f"Remote MCP server with 'url' must use transport 'http' or 'sse', got {raw_transport!r}")
        return normalized
    if normalized != "stdio":
        raise ValueError(f"MCP server with transport {raw_transport!r} requires a 'url'")
    return "stdio"


def _validate_remote_url(url: str, *, server_name: str) -> str:
    """Validate an HTTP/SSE MCP server URL."""
    cleaned = url.strip()
    if not cleaned:
        raise ValueError(f"MCP server {server_name!r} has an empty 'url'")
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"MCP server {server_name!r} url must be an http(s) URL with a host")
    return cleaned


def validate_header_references(headers: Mapping[str, str], *, server_name: str = "") -> None:
    """Reject literal credential values in saved MCP header definitions.

    Credential-bearing headers (e.g. ``Authorization``, ``X-API-Key``) and
    credential-shaped values (e.g. ``Bearer ...``, ``sk-...``) must use
    ``${ENV_VAR}`` references instead of literal secrets. Error messages
    intentionally omit header values so secrets never leak into logs or errors.
    """
    label = server_name or "<unnamed>"
    for raw_key, raw_val in headers.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError(f"MCP server {label!r} contains an empty header name")
        if not isinstance(raw_val, str) or not raw_val.strip():
            raise ValueError(f"MCP server {label!r} header {raw_key!r} must be a non-empty string")

        has_env_ref = bool(_ENV_REF_PATTERN.search(raw_val))
        stripped_refs = _ENV_REF_PATTERN.sub("", raw_val).strip()

        if _SENSITIVE_HEADER_NAME_RE.search(raw_key):
            scheme_match = _CREDENTIAL_SCHEME_RE.match(raw_val)
            payload_after_scheme = scheme_match.group(1) if scheme_match else raw_val
            payload_remainder = _ENV_REF_PATTERN.sub("", payload_after_scheme).strip()
            if not has_env_ref or payload_remainder:
                raise ValueError(
                    f"MCP server {label!r} header {raw_key!r} looks like a credential and must "
                    "reference an environment variable (${VAR}) instead of storing a literal value"
                )
            continue

        # Non-sensitive header names still reject credential-shaped values unless referenced via ${VAR}.
        if not has_env_ref:
            if _CREDENTIAL_SCHEME_RE.match(raw_val) or stripped_refs.startswith(_SECRET_VALUE_PREFIXES):
                raise ValueError(
                    f"MCP server {label!r} header {raw_key!r} looks like a credential and must "
                    "reference an environment variable (${VAR}) instead of storing a literal value"
                )
        else:
            scheme_match = _CREDENTIAL_SCHEME_RE.match(raw_val)
            if scheme_match and _ENV_REF_PATTERN.sub("", scheme_match.group(1)).strip():
                raise ValueError(
                    f"MCP server {label!r} header {raw_key!r} mixes literal credential text with "
                    "an environment reference"
                )


def resolve_header_env_vars(headers: Mapping[str, str], *, server_name: str = "") -> dict[str, str]:
    """Expand ``${VAR}`` placeholders in HTTP headers from ``os.environ``.

    Raises:
        ValueError: If a referenced environment variable is unset or empty.
            Neither resolved values nor raw header values are included in the
            exception message.
    """
    label = server_name or "<unnamed>"
    resolved: dict[str, str] = {}
    for key, template in headers.items():

        def _replace(match: re.Match[str], *, header_key: str = key) -> str:
            var_name = match.group(1)
            val = os.environ.get(var_name)
            if not val:
                raise ValueError(
                    f"MCP server {label!r} header {header_key!r} references unset environment variable {var_name!r}"
                )
            return val

        resolved[key] = _ENV_REF_PATTERN.sub(_replace, template)
    return resolved


@dataclass(frozen=True)
class MCPServerEntry:
    """A known MCP server from the catalog.

    Attributes:
        name: Human-readable identifier (used as mcpServers key).
        package: npm package name for npx installation (stdio entries).
        capabilities: Capability tags for programmatic matching.
        keywords: Phrases in task descriptions that trigger this server.
        env_required: Environment variables that must be set.
        command: Executable to run (stdio entries).
        args: Arguments to pass (stdio entries). Defaults to ["-y", <package>].
        plugin_name: Plugin that registered this server. When set, the
            server key in mcpServers is prefixed as ``<plugin_name>__<name>``
            to prevent naming collisions across plugins.
        url: Optional HTTP/SSE endpoint URL for remote MCP servers.
        transport: Transport type (``"stdio"``, ``"http"``, or ``"sse"``).
            Defaults to ``"http"`` when ``url`` is provided, otherwise ``"stdio"``.
        headers: Optional HTTP headers for remote servers. Credential values
            must use ``${ENV_VAR}`` references rather than literal secrets.
    """

    name: str
    package: str = ""
    capabilities: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    env_required: tuple[str, ...] = ()
    command: str = _STDIO_DEFAULT_COMMAND
    args: tuple[str, ...] | None = None
    plugin_name: str = ""
    url: str | None = None
    transport: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        has_url = self.url is not None
        normalized_transport = _normalize_transport(self.transport, has_url=has_url)
        object.__setattr__(self, "transport", normalized_transport)
        copied_headers = {str(k): str(v) for k, v in self.headers.items()}
        object.__setattr__(self, "headers", copied_headers)

        if has_url:
            assert self.url is not None
            if self.package or self.args is not None or self.command != _STDIO_DEFAULT_COMMAND:
                raise ValueError(
                    f"MCP server {self.name!r} cannot combine remote 'url' with stdio 'package', 'command', or 'args'"
                )
            validated_url = _validate_remote_url(self.url, server_name=self.name)
            object.__setattr__(self, "url", validated_url)
            validate_header_references(copied_headers, server_name=self.name)
        else:
            if copied_headers:
                raise ValueError(f"MCP server {self.name!r} cannot declare HTTP 'headers' without 'url'")

    @property
    def is_remote(self) -> bool:
        """Return True when this entry targets a remote HTTP or SSE endpoint."""
        return self.url is not None

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
            Dict suitable for ``mcpServers`` injection (either stdio
            ``command``/``args`` or remote ``type``/``url``/``headers``).
        """
        if self.url is not None:
            config: dict[str, Any] = {
                "type": self.transport,
                "url": self.url,
            }
            if self.headers:
                config["headers"] = resolve_header_env_vars(self.headers, server_name=self.name)
            return config

        args = list(self.args) if self.args else ["-y", self.package]
        config = {"command": self.command, "args": args}

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
        if self.url is not None:
            validate_header_references(self.headers, server_name=self.name)
            payload: dict[str, Any] = {
                "name": self.name,
                "url": self.url,
                "transport": self.transport,
            }
            if self.headers:
                payload["headers"] = dict(self.headers)
            if self.capabilities:
                payload["capabilities"] = list(self.capabilities)
            if self.keywords:
                payload["keywords"] = list(self.keywords)
            if self.env_required:
                payload["env_required"] = list(self.env_required)
            return payload

        payload = {
            "name": self.name,
            "package": self.package,
        }
        if self.capabilities:
            payload["capabilities"] = list(self.capabilities)
        if self.keywords:
            payload["keywords"] = list(self.keywords)
        if self.env_required:
            payload["env_required"] = list(self.env_required)
        if self.command != _STDIO_DEFAULT_COMMAND:
            payload["command"] = self.command
        if self.args is not None:
            payload["args"] = list(self.args)
        return payload


def load_catalog_entries(path: Path) -> list[MCPServerEntry]:
    """Load MCP entries from a catalog path."""
    return MCPRegistry(config_path=path).servers


def save_catalog_entries(path: Path, servers: list[MCPServerEntry]) -> None:
    """Write MCP entries to the standard YAML catalog format."""
    payload = {"servers": [server.to_catalog_dict() for server in servers]}
    path.parent.mkdir(parents=True, exist_ok=True)
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
        return self._servers.copy()

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
            if not name or not isinstance(name, str) or not name.strip():
                logger.warning("MCP server entry missing name: %r", entry_dict)
                continue

            raw_url = entry_dict.get("url")
            raw_package = entry_dict.get("package")
            raw_transport = entry_dict.get("transport")
            raw_headers = entry_dict.get("headers")

            if raw_url is None and (not raw_package or not isinstance(raw_package, str) or not raw_package.strip()):
                logger.warning("MCP server entry %r missing package or url", name)
                continue
            if raw_url is not None and not isinstance(raw_url, str):
                logger.warning("MCP server entry %r url must be a string", name)
                continue
            if raw_transport is not None and not isinstance(raw_transport, str):
                logger.warning("MCP server entry %r transport must be a string", name)
                continue

            headers_dict: dict[str, str] = {}
            if raw_headers is not None:
                if not isinstance(raw_headers, dict):
                    logger.warning("MCP server entry %r headers must be a mapping", name)
                    continue
                bad_header = False
                for hk, hv in cast("dict[Any, Any]", raw_headers).items():
                    if not isinstance(hk, str) or not isinstance(hv, str):
                        logger.warning("MCP server entry %r headers keys and values must be strings", name)
                        bad_header = True
                        break
                    headers_dict[hk] = hv
                if bad_header:
                    continue

            if raw_url is not None and any(
                entry_dict.get(k) not in (None, "", []) for k in ("package", "command", "args")
            ):
                logger.warning("MCP server entry %r mixes remote url with stdio package/command/args", name)
                continue

            capabilities_raw: list[Any] = list(entry_dict.get("capabilities", []))
            keywords_raw: list[Any] = list(entry_dict.get("keywords", []))
            env_req_raw: list[Any] = list(entry_dict.get("env_required", []))
            try:
                raw_args = entry_dict.get("args") if raw_url is None else None
                raw_cmd = entry_dict.get("command")
                command_val = str(raw_cmd) if raw_url is None and raw_cmd is not None else _STDIO_DEFAULT_COMMAND
                entry = MCPServerEntry(
                    name=str(name),
                    package="" if raw_url is not None else str(raw_package),
                    url=raw_url,
                    transport=raw_transport,
                    headers=headers_dict,
                    capabilities=tuple(str(c) for c in capabilities_raw),
                    keywords=tuple(str(k) for k in keywords_raw),
                    env_required=tuple(str(e) for e in env_req_raw),
                    command=command_val,
                    args=tuple(str(a) for a in raw_args) if raw_args is not None else None,
                )
            except ValueError as exc:
                logger.warning("Skipping invalid MCP server entry %r: %s", name, exc)
                continue
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
                url=server.url,
                transport=server.transport,
                headers=dict(server.headers),
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
        if base_config is auto_config is None:
            return None
        if base_config is None:
            return auto_config
        if auto_config is None:
            return base_config

        # Both exist - merge auto into base (base takes precedence).
        # Non-mcpServers keys from base_config (per-spawn sampling and
        # endpoint overrides such as temperature/base_url) must survive
        # the merge so the adapter capability gate can see them.
        merged_servers = dict(auto_config.get("mcpServers", {}))
        merged_servers.update(base_config.get("mcpServers", {}))
        return {**base_config, "mcpServers": merged_servers}
