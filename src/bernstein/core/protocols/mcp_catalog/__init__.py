"""Bernstein MCP catalog client (release 1.9).

Public API for ``bernstein mcp catalog`` operations.

The catalog at ``https://bernstein.run/mcp-catalog.json`` (with a
GitHub mirror fallback) lists installable MCP servers. This package
fetches and validates the manifest, runs each install command in a
sandboxed dry-run preview before touching the host MCP config, and
emits HMAC-chained audit events so ``bernstein audit verify`` can
attest catalog activity.

Install pattern::

    from bernstein.core.protocols.mcp_catalog import (
        CatalogFetcher,
        CatalogService,
    )

    service = CatalogService(
        fetcher=CatalogFetcher(),
        user_config_path=default_user_config_path(),
    )
    outcome = service.install("fs-readonly", skip_confirmation=True)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime

from bernstein.core.protocols.mcp_catalog.audit import (
    AUDIT_ACTOR,
    AUDIT_RESOURCE_TYPE,
    CatalogAuditor,
)
from bernstein.core.protocols.mcp_catalog.fetcher import (
    DEFAULT_CATALOG_URL,
    DEFAULT_CHECK_INTERVAL_SECONDS,
    DEFAULT_MIRROR_URL,
    DEFAULT_REVALIDATE_SECONDS,
    CacheEntry,
    CatalogFetcher,
    FetchResult,
    HTTPResponse,
    HTTPTransport,
    default_cache_path,
)
from bernstein.core.protocols.mcp_catalog.local_manifests import (
    LOCAL_MANIFESTS_DIR,
    find_local_entry,
    load_local_manifests,
)
from bernstein.core.protocols.mcp_catalog.manifest import (
    Catalog,
    CatalogEntry,
    CatalogValidationError,
    validate_catalog,
)
from bernstein.core.protocols.mcp_catalog.sandbox_preview import (
    FileDiff,
    InstallPreview,
    SandboxRunner,
    preview_local_manifest,
    run_install_preview,
)
from bernstein.core.protocols.mcp_catalog.service import (
    CatalogService,
    CatalogServiceConfig,
    CatalogStatus,
    InstallOutcome,
    UpgradeOutcome,
)
from bernstein.core.protocols.mcp_catalog.user_config import (
    BERNSTEIN_MANAGED_KEY,
    SERVERS_KEY,
    InstalledEntry,
    default_user_config_path,
    install_entry,
    list_installed,
    touch_upgrade_check,
    uninstall_entry,
    upgrade_entry,
)

__all__ = [
    "AUDIT_ACTOR",
    "AUDIT_RESOURCE_TYPE",
    "BERNSTEIN_MANAGED_KEY",
    "DEFAULT_CATALOG_URL",
    "DEFAULT_CHECK_INTERVAL_SECONDS",
    "DEFAULT_MIRROR_URL",
    "DEFAULT_REVALIDATE_SECONDS",
    "LOCAL_MANIFESTS_DIR",
    "SERVERS_KEY",
    "CacheEntry",
    "Catalog",
    "CatalogAuditor",
    "CatalogEntry",
    "CatalogFetcher",
    "CatalogService",
    "CatalogServiceConfig",
    "CatalogStatus",
    "CatalogValidationError",
    "FetchResult",
    "FileDiff",
    "HTTPResponse",
    "HTTPTransport",
    "InstallOutcome",
    "InstallPreview",
    "InstalledEntry",
    "MCPServerCapabilities",
    "SandboxRunner",
    "ServerCapabilitiesStore",
    "UpgradeOutcome",
    "default_cache_path",
    "default_user_config_path",
    "find_local_entry",
    "install_entry",
    "list_installed",
    "load_local_manifests",
    "preview_local_manifest",
    "run_install_preview",
    "touch_upgrade_check",
    "uninstall_entry",
    "upgrade_entry",
    "validate_catalog",
]


@dataclass
class MCPServerCapabilities:
    """Server capabilities fingerprint.

    Attributes:
        server_name: Name of the MCP server.
        tool_names: Immutable set of tool names advertised by the server.
        capability_digest: SHA256 digest of sorted canonical JSON.
        first_contact_at: ISO timestamp of first observed capabilities.
    """

    server_name: str
    tool_names: frozenset[str]
    capability_digest: str
    first_contact_at: str


def _canonicalize_tool_names(tool_names: frozenset[str]) -> str:
    """Return JSON-canonicalized sorted tool name list for digest."""
    return json.dumps(sorted(tool_names), separators=(",", ":"))


class ServerCapabilitiesStore:
    """Persistent store for MCP server capabilities.

    Uses content-addressed JSON canonicalization for stable digests.
    Capabilities are persisted to a JSON file at ``<sdd_dir>/.sdd/mcp_server_capabilities.json``.
    """

    def __init__(self, sdd_dir: str):
        self._sdd_dir = sdd_dir
        self._file_path = os.path.join(sdd_dir, ".sdd", "mcp_server_capabilities.json")
        self._capabilities: dict[str, MCPServerCapabilities] = {}
        self.load()

    def load(self) -> None:
        """Load capabilities from JSON file."""
        if os.path.exists(self._file_path):
            try:
                with open(self._file_path) as f:
                    data = json.load(f)
                for server_name, caps_data in data.items():
                    tool_names = frozenset(caps_data["tool_names"])
                    self._capabilities[server_name] = MCPServerCapabilities(
                        server_name=server_name,
                        tool_names=tool_names,
                        capability_digest=caps_data["capability_digest"],
                        first_contact_at=caps_data["first_contact_at"],
                    )
            except (json.JSONDecodeError, KeyError):
                # Corrupt file - start fresh
                self._capabilities = {}

    def save(self) -> None:
        """Persist capabilities to JSON file."""
        os.makedirs(os.path.dirname(self._file_path), exist_ok=True)
        data = {}
        for server_name, caps in self._capabilities.items():
            data[server_name] = {
                "tool_names": sorted(caps.tool_names),
                "capability_digest": caps.capability_digest,
                "first_contact_at": caps.first_contact_at,
            }
        with open(self._file_path, "w") as f:
            json.dump(data, f, indent=2)

    def get_digest(self, server_name: str) -> str | None:
        """Get current capability digest for server."""
        caps = self._capabilities.get(server_name)
        return caps.capability_digest if caps else None

    def set_capabilities(
        self, server_name: str, tool_names: frozenset[str], capability_digest: str
    ) -> MCPServerCapabilities | None:
        """Update server capabilities and detect drift.

        Args:
            server_name: Name of the MCP server.
            tool_names: Current set of tool names.
            capability_digest: Digest of current tool names.

        Returns:
            Previous capabilities if drift detected, None otherwise.
        """
        old = self._capabilities.get(server_name)
        now = datetime.now().isoformat()
        new_caps = MCPServerCapabilities(
            server_name=server_name,
            tool_names=tool_names,
            capability_digest=capability_digest,
            first_contact_at=now if old is None else old.first_contact_at,
        )
        self._capabilities[server_name] = new_caps
        self.save()
        if old is None or old.capability_digest != capability_digest:
            return old
        return None

    def get_capabilities(self, server_name: str) -> MCPServerCapabilities | None:
        """Get full capabilities record for server."""
        return self._capabilities.get(server_name)
