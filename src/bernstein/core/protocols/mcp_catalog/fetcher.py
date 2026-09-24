"""MCP catalog fetcher: conditional revalidation, shared cache, mirror fallback.

The primary source is ``https://bernstein.run/mcp-catalog.json``. On any
5xx response Bernstein falls back to the public GitHub mirror. The fetched
payload is validated against the strict schema. On validation failure the
last valid cached copy is preserved, and the rejected body is remembered by
its ``ETag`` so an unchanged invalid document is not downloaded again.

The request and cache mechanics live in
:mod:`bernstein.core.protocols.catalog_fetch`, shared with the skill catalog.
The cache is one user-level file (``$XDG_CACHE_HOME/bernstein/mcp-catalog.json``
or ``~/.cache/bernstein/mcp-catalog.json``) shared by every worker;
``BERNSTEIN_MCP_CATALOG_CACHE_PATH`` overrides it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.protocols.catalog_fetch import (
    CacheEntry,
    ConditionalCatalogFetcher,
    FetchResult,
    HTTPResponse,
    HTTPTransport,
    UrllibTransport,
    user_cache_path,
)
from bernstein.core.protocols.mcp_catalog.manifest import (
    Catalog,
    CatalogValidationError,
    validate_catalog,
)

if TYPE_CHECKING:
    from pathlib import Path

#: Primary catalog URL.
DEFAULT_CATALOG_URL = "https://bernstein.run/mcp-catalog.json"

#: GitHub mirror used as fallback on 5xx errors against the primary URL.
DEFAULT_MIRROR_URL = "https://raw.githubusercontent.com/chernistry/bernstein-mcp-catalog/main/mcp-catalog.json"

#: Default revalidation window (6h, configurable via mcp.catalog.revalidate_interval).
DEFAULT_REVALIDATE_SECONDS = 6 * 3600

#: Default upgrade-cadence check interval (24h).
DEFAULT_CHECK_INTERVAL_SECONDS = 24 * 3600

#: Environment variable that overrides the cache file location.
CACHE_PATH_ENV = "BERNSTEIN_MCP_CATALOG_CACHE_PATH"


def default_cache_path() -> Path:
    """Return the user-level cache file shared by every worker.

    ``BERNSTEIN_MCP_CATALOG_CACHE_PATH`` wins when set; otherwise the file
    lives under ``$XDG_CACHE_HOME/bernstein/`` or ``~/.cache/bernstein/``.
    """
    return user_cache_path("mcp-catalog.json", env_override=CACHE_PATH_ENV)


class _UrllibTransport(UrllibTransport):
    """Default transport; plain ``http://`` is allowed for operator mirrors."""

    def __init__(self) -> None:
        super().__init__(allow_http=True, source="mcp_catalog.fetcher")


class CatalogFetcher(ConditionalCatalogFetcher[Catalog]):
    """Fetch the MCP catalog with conditional revalidation and a mirror fallback.

    Args:
        primary_url: Primary catalog URL.
        mirror_url: GitHub mirror URL used on 5xx from the primary.
        cache_path: Cache file location. Defaults to :func:`default_cache_path`.
        revalidate_seconds: Skip the network entirely while the last
            response is this fresh.
        transport: HTTP transport. Defaults to :mod:`urllib`-backed.
    """

    def __init__(
        self,
        *,
        primary_url: str = DEFAULT_CATALOG_URL,
        mirror_url: str = DEFAULT_MIRROR_URL,
        cache_path: Path | None = None,
        revalidate_seconds: int = DEFAULT_REVALIDATE_SECONDS,
        transport: HTTPTransport | None = None,
    ) -> None:
        super().__init__(
            primary_url=primary_url,
            mirror_url=mirror_url,
            cache_path=cache_path or default_cache_path(),
            revalidate_seconds=revalidate_seconds,
            transport=transport or _UrllibTransport(),
            validate=validate_catalog,
            error_type=CatalogValidationError,
            user_agent="bernstein-mcp-catalog/1.0",
            label="MCP catalog",
        )


__all__ = [
    "CACHE_PATH_ENV",
    "DEFAULT_CATALOG_URL",
    "DEFAULT_CHECK_INTERVAL_SECONDS",
    "DEFAULT_MIRROR_URL",
    "DEFAULT_REVALIDATE_SECONDS",
    "CacheEntry",
    "CatalogFetcher",
    "FetchResult",
    "HTTPResponse",
    "HTTPTransport",
    "default_cache_path",
]
