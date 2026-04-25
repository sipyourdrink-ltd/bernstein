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
    "SandboxRunner",
    "UpgradeOutcome",
    "default_cache_path",
    "default_user_config_path",
    "install_entry",
    "list_installed",
    "run_install_preview",
    "touch_upgrade_check",
    "uninstall_entry",
    "upgrade_entry",
    "validate_catalog",
]
