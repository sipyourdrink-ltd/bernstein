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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from bernstein.core.persistence.atomic_write import write_atomic_text
from bernstein.core.persistence.file_locks import cross_process_lock
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


class ServerCapabilitiesStore:
    """Persistent store for MCP server capabilities.

    Uses content-addressed JSON canonicalization for stable digests.
    Capabilities are persisted to a JSON file at ``<sdd_dir>/.sdd/mcp_server_capabilities.json``.
    """

    def __init__(self, sdd_dir: str | Path) -> None:
        self._file_path = Path(sdd_dir) / ".sdd" / "mcp_server_capabilities.json"
        self._lock_path = self._file_path.with_suffix(".lock")
        self._capabilities: dict[str, MCPServerCapabilities] = {}
        self.load()

    def _read_capabilities(self) -> dict[str, MCPServerCapabilities]:
        try:
            data = json.loads(self._file_path.read_text(encoding="utf-8"))
            return {
                server_name: MCPServerCapabilities(
                    server_name=server_name,
                    tool_names=frozenset(caps_data["tool_names"]),
                    capability_digest=caps_data["capability_digest"],
                    first_contact_at=caps_data["first_contact_at"],
                )
                for server_name, caps_data in data.items()
            }
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return {}

    def load(self) -> None:
        """Reload capabilities from the current persisted snapshot."""
        self._capabilities = self._read_capabilities()

    def _write_capabilities(self, entries: dict[str, MCPServerCapabilities]) -> None:
        data = {
            server_name: {
                "tool_names": sorted(caps.tool_names),
                "capability_digest": caps.capability_digest,
                "first_contact_at": caps.first_contact_at,
            }
            for server_name, caps in entries.items()
        }
        write_atomic_text(self._file_path, json.dumps(data, indent=2))

    def save(self) -> None:
        """Persist the latest snapshot without overwriting newer observations."""
        with cross_process_lock(self._lock_path):
            entries = self._read_capabilities()
            self._write_capabilities(entries)
            self._capabilities = entries

    def get_digest(self, server_name: str) -> str | None:
        """Get current capability digest for server."""
        caps = self.get_capabilities(server_name)
        return caps.capability_digest if caps else None

    def set_capabilities(
        self,
        server_name: str,
        tool_names: frozenset[str],
        capability_digest: str,
        *,
        before_change: Callable[[MCPServerCapabilities | None], None] | None = None,
    ) -> MCPServerCapabilities | None:
        """Update server capabilities and detect drift.

        Args:
            server_name: Name of the MCP server.
            tool_names: Current set of tool names.
            capability_digest: Digest of current tool names.
            before_change: Optional observer for first contact and changed digests.
                Called under the snapshot lock before saving; an exception leaves
                the previous baseline in place. Must not re-enter the store.
                A crash after the observer commits but before the snapshot is
                written can cause the observation to be recorded again.

        Returns:
            Previous capabilities if drift detected, None otherwise.
        """
        # Observation is optional; avoid holding up gateway shutdown for
        # snapshot-lock contention after the probe is otherwise complete.
        with cross_process_lock(self._lock_path, timeout=5.0 if before_change is not None else None):
            entries = self._read_capabilities()
            old = entries.get(server_name)
            if before_change is not None:
                if old is not None and old.capability_digest == capability_digest:
                    return None
                before_change(old)
            entries[server_name] = MCPServerCapabilities(
                server_name=server_name,
                tool_names=tool_names,
                capability_digest=capability_digest,
                first_contact_at=datetime.now().isoformat() if old is None else old.first_contact_at,
            )
            self._write_capabilities(entries)
            self._capabilities = entries
        if old is None or old.capability_digest != capability_digest:
            return old
        return None

    def get_capabilities(self, server_name: str) -> MCPServerCapabilities | None:
        """Get full capabilities record for server."""
        return self._read_capabilities().get(server_name)
