"""Skill catalog fetcher: conditional revalidation over one shared cache.

Uses the same request and cache mechanics as the MCP catalog
(:mod:`bernstein.core.protocols.catalog_fetch`) with the skill catalog URL,
schema and an HTTPS-only transport.

The cache is one user-level file (``$XDG_CACHE_HOME/bernstein/skills-catalog.json``
or ``~/.cache/bernstein/skills-catalog.json``), so parallel workers in
separate worktrees read the same catalog, the same digests and the same
signed revocations. ``BERNSTEIN_SKILLS_CATALOG_CACHE_PATH`` overrides it.

The TTL honours ``BERNSTEIN_SKILLS_CATALOG_TTL`` (seconds) at the process
level. Callers can also pass an explicit ``revalidate_seconds``.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bernstein.core.protocols.catalog_fetch import (
    CacheEntry,
    ConditionalCatalogFetcher,
    FetchResult,
    HTTPResponse,
    HTTPTransport,
    UrllibTransport,
    user_cache_path,
    write_cache,
)
from bernstein.core.skills.catalog.manifest import (
    SkillCatalog,
    SkillCatalogValidationError,
    validate_catalog,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Primary catalog URL.
DEFAULT_SKILLS_CATALOG_URL = "https://bernstein.run/skills-catalog.json"

#: Public mirror used as fallback on 5xx.
DEFAULT_SKILLS_MIRROR_URL = (
    "https://raw.githubusercontent.com/chernistry/bernstein-skills-catalog/main/skills-catalog.json"
)

#: Default revalidation window (6 hours).
DEFAULT_REVALIDATE_SECONDS = 6 * 3600

#: Default upgrade-cadence check interval (24h).
DEFAULT_CHECK_INTERVAL_SECONDS = 24 * 3600

#: Environment variable that overrides the cache TTL.
TTL_ENV = "BERNSTEIN_SKILLS_CATALOG_TTL"

#: Environment variable that overrides the cache file location.
CACHE_PATH_ENV = "BERNSTEIN_SKILLS_CATALOG_CACHE_PATH"


def default_cache_path() -> Path:
    """Return the user-level cache file shared by every worker and worktree.

    ``BERNSTEIN_SKILLS_CATALOG_CACHE_PATH`` wins when set; otherwise the file
    lives under ``$XDG_CACHE_HOME/bernstein/`` or ``~/.cache/bernstein/``.
    """
    return user_cache_path("skills-catalog.json", env_override=CACHE_PATH_ENV)


def legacy_project_cache_path(workdir: Path) -> Path:
    """Where releases before the shared cache kept a project's copy.

    Read-only fallback so signed revocations cached there keep applying
    until the shared cache is first populated.
    """
    return workdir / ".sdd" / "skills_catalog" / "catalog.json"


def env_ttl_seconds(default: int = DEFAULT_REVALIDATE_SECONDS) -> int:
    """Resolve the TTL from ``BERNSTEIN_SKILLS_CATALOG_TTL`` or fall back."""
    raw = os.environ.get(TTL_ENV)
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using default %ds", TTL_ENV, raw, default)
        return default


class _UrllibTransport(UrllibTransport):
    """Default transport; the skill catalog is fetched over HTTPS only."""

    def __init__(self) -> None:
        super().__init__(allow_http=False, source="skills_catalog.fetcher")


class SkillCatalogFetcher(ConditionalCatalogFetcher[SkillCatalog]):
    """Fetch the skill catalog with conditional revalidation and a mirror fallback.

    Args:
        primary_url: Primary catalog URL.
        mirror_url: GitHub mirror URL used on 5xx from the primary.
        cache_path: Cache file location. Defaults to :func:`default_cache_path`.
        revalidate_seconds: Skip the network entirely while the last
            response is this fresh. ``None`` resolves to :func:`env_ttl_seconds`.
        transport: HTTP transport. Defaults to :mod:`urllib`-backed.
    """

    def __init__(
        self,
        *,
        primary_url: str = DEFAULT_SKILLS_CATALOG_URL,
        mirror_url: str = DEFAULT_SKILLS_MIRROR_URL,
        cache_path: Path | None = None,
        revalidate_seconds: int | None = None,
        transport: HTTPTransport | None = None,
    ) -> None:
        super().__init__(
            primary_url=primary_url,
            mirror_url=mirror_url,
            cache_path=cache_path or default_cache_path(),
            revalidate_seconds=revalidate_seconds if revalidate_seconds is not None else env_ttl_seconds(),
            transport=transport or _UrllibTransport(),
            validate=validate_catalog,
            error_type=SkillCatalogValidationError,
            user_agent="bernstein-skills-catalog/1.0",
            label="skill catalog",
        )

    @property
    def revalidate_seconds(self) -> int:
        """Effective revalidation window in seconds."""
        return self._revalidate_seconds

    def write_cache_payload(self, payload: dict[str, Any], *, source_url: str | None = None) -> None:
        """Write a payload directly to the cache.

        Useful for tests and for seeding the cache from a local bundled
        manifest so air-gapped installs can still browse the catalog.
        """
        _ = validate_catalog(payload)  # rejects malformed payloads upfront
        entry = CacheEntry(
            fetched_at=datetime.now(tz=UTC).isoformat(),
            etag=None,
            source_url=source_url or self._primary_url,
            catalog=payload,
        )
        write_cache(self._cache_path, entry)


__all__ = [
    "CACHE_PATH_ENV",
    "DEFAULT_CHECK_INTERVAL_SECONDS",
    "DEFAULT_REVALIDATE_SECONDS",
    "DEFAULT_SKILLS_CATALOG_URL",
    "DEFAULT_SKILLS_MIRROR_URL",
    "TTL_ENV",
    "CacheEntry",
    "FetchResult",
    "HTTPResponse",
    "HTTPTransport",
    "SkillCatalogFetcher",
    "default_cache_path",
    "env_ttl_seconds",
    "legacy_project_cache_path",
]
