"""Skill catalog fetcher with cache under ``.sdd/skills_catalog/``.

Structurally identical to
:class:`bernstein.core.protocols.mcp_catalog.fetcher.CatalogFetcher` but
points at the skill catalog URL and uses a project-local cache so
parallel worktrees can resolve the same digest deterministically.

The TTL honours ``BERNSTEIN_SKILLS_CATALOG_TTL`` (seconds) at the
process level. Operators can also pass an explicit ``revalidate_seconds``
to the constructor for tests.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from bernstein.core.security.url_allowlist import ensure_public_http_url
from bernstein.core.skills.catalog.manifest import (
    SkillCatalog,
    SkillCatalogValidationError,
    validate_catalog,
)

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


def default_cache_path(workdir: Path | None = None) -> Path:
    """Return the default cache file under ``.sdd/skills_catalog/``.

    Args:
        workdir: Project root. Defaults to ``Path.cwd()``.
    """
    root = workdir or Path.cwd()
    return root / ".sdd" / "skills_catalog" / "catalog.json"


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


@dataclass(frozen=True)
class HTTPResponse:
    """Minimal HTTP response shape."""

    status: int
    body: bytes
    etag: str | None


class HTTPTransport(Protocol):
    """Pluggable HTTP transport so tests don't hit the real network."""

    def get(self, url: str, *, headers: dict[str, str]) -> HTTPResponse:
        """Issue a GET request and return the response."""
        ...


class _UrllibTransport:
    """Default transport backed by :mod:`urllib.request`."""

    def get(self, url: str, *, headers: dict[str, str]) -> HTTPResponse:
        ensure_public_http_url(url, allow_http=False, source="skills_catalog.fetcher")
        request = urllib.request.Request(url, headers=headers)
        try:
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(request, timeout=15) as resp:
                body = resp.read()
                etag = resp.headers.get("ETag")
                return HTTPResponse(status=resp.status, body=body, etag=etag)
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp is not None else b""
            etag = exc.headers.get("ETag") if exc.headers is not None else None
            return HTTPResponse(status=exc.code, body=body, etag=etag)


@dataclass(frozen=True)
class CacheEntry:
    """A persisted catalog cache entry."""

    fetched_at: str
    etag: str | None
    source_url: str
    catalog: dict[str, Any]


@dataclass(frozen=True)
class FetchResult:
    """Outcome of :meth:`SkillCatalogFetcher.fetch`."""

    catalog: SkillCatalog
    from_cache: bool
    revalidated: bool
    source_url: str


def _read_cache(cache_path: Path) -> CacheEntry | None:
    """Load a cache file. Returns ``None`` on any read or parse error."""
    if not cache_path.exists():
        return None
    try:
        raw = cache_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    catalog_raw = data.get("catalog")
    if not isinstance(catalog_raw, dict):
        return None
    return CacheEntry(
        fetched_at=str(data.get("fetched_at", "")),
        etag=data.get("etag") if isinstance(data.get("etag"), str) else None,
        source_url=str(data.get("source_url", "")),
        catalog=catalog_raw,
    )


def _write_cache(cache_path: Path, entry: CacheEntry) -> None:
    """Persist a cache entry to disk atomically."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": entry.fetched_at,
        "etag": entry.etag,
        "source_url": entry.source_url,
        "catalog": entry.catalog,
    }
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(cache_path)


def _is_within_revalidate_window(
    fetched_at: str,
    *,
    revalidate_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Return True when the cache is fresh enough to skip revalidation."""
    if not fetched_at:
        return False
    try:
        ts = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    current = now or datetime.now(tz=UTC)
    return current - ts < timedelta(seconds=revalidate_seconds)


class SkillCatalogFetcher:
    """Fetch the skill catalog with ETag revalidation and a mirror fallback.

    Args:
        primary_url: Primary catalog URL.
        mirror_url: GitHub mirror URL used on 5xx from the primary.
        cache_path: Cache file location. Defaults to
            ``<cwd>/.sdd/skills_catalog/catalog.json``.
        revalidate_seconds: Skip the network entirely while the cache is
            this fresh. ``None`` resolves to :func:`env_ttl_seconds`.
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
        self._primary_url = primary_url
        self._mirror_url = mirror_url
        self._cache_path = cache_path or default_cache_path()
        self._revalidate_seconds = revalidate_seconds if revalidate_seconds is not None else env_ttl_seconds()
        self._transport: HTTPTransport = transport or _UrllibTransport()

    @property
    def cache_path(self) -> Path:
        """The cache file location."""
        return self._cache_path

    @property
    def revalidate_seconds(self) -> int:
        """Effective revalidation window in seconds."""
        return self._revalidate_seconds

    def cached(self) -> SkillCatalog | None:
        """Return the cached catalog if any, validated; else ``None``."""
        entry = _read_cache(self._cache_path)
        if entry is None:
            return None
        try:
            return validate_catalog(entry.catalog)
        except SkillCatalogValidationError as exc:
            logger.warning("Cached skill catalog failed re-validation: %s", exc)
            return None

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
        _write_cache(self._cache_path, entry)

    def fetch(self, *, force: bool = False, now: datetime | None = None) -> FetchResult:
        """Fetch the catalog, honouring the revalidation window.

        Args:
            force: Skip the freshness window and always revalidate.
            now: Override the wall clock (testing only).

        Returns:
            :class:`FetchResult` with the validated catalog.

        Raises:
            SkillCatalogValidationError: If the fetched body fails schema
                validation. The cache is preserved untouched.
            RuntimeError: If both primary and mirror fail without a cache.
        """
        cached_entry = _read_cache(self._cache_path)

        if (
            not force
            and cached_entry is not None
            and _is_within_revalidate_window(
                cached_entry.fetched_at,
                revalidate_seconds=self._revalidate_seconds,
                now=now,
            )
        ):
            try:
                catalog = validate_catalog(cached_entry.catalog)
            except SkillCatalogValidationError:
                cached_entry = None
            else:
                return FetchResult(
                    catalog=catalog,
                    from_cache=True,
                    revalidated=False,
                    source_url=cached_entry.source_url or self._primary_url,
                )

        headers: dict[str, str] = {
            "User-Agent": "bernstein-skills-catalog/1.0",
            "Accept": "application/json",
        }
        if cached_entry is not None and cached_entry.etag:
            headers["If-None-Match"] = cached_entry.etag

        try:
            response = self._transport.get(self._primary_url, headers=headers)
            source_url = self._primary_url
        except (TimeoutError, OSError) as exc:
            logger.info("Primary skill catalog fetch failed (%s); trying mirror", exc)
            response = self._transport.get(self._mirror_url, headers=headers)
            source_url = self._mirror_url
        else:
            if 500 <= response.status < 600:
                logger.info(
                    "Primary skill catalog returned %d; falling back to mirror",
                    response.status,
                )
                response = self._transport.get(self._mirror_url, headers=headers)
                source_url = self._mirror_url

        if response.status == 304 and cached_entry is not None:
            try:
                catalog = validate_catalog(cached_entry.catalog)
            except SkillCatalogValidationError as exc:  # pragma: no cover
                raise SkillCatalogValidationError("cached catalog failed re-validation after 304") from exc
            updated = CacheEntry(
                fetched_at=datetime.now(tz=UTC).isoformat(),
                etag=cached_entry.etag,
                source_url=source_url,
                catalog=cached_entry.catalog,
            )
            _write_cache(self._cache_path, updated)
            return FetchResult(
                catalog=catalog,
                from_cache=True,
                revalidated=True,
                source_url=source_url,
            )

        if response.status >= 400:
            if cached_entry is not None:
                try:
                    catalog = validate_catalog(cached_entry.catalog)
                except SkillCatalogValidationError:
                    pass
                else:
                    logger.warning(
                        "skills catalog fetch returned %d; serving stale cache",
                        response.status,
                    )
                    return FetchResult(
                        catalog=catalog,
                        from_cache=True,
                        revalidated=True,
                        source_url=cached_entry.source_url or source_url,
                    )
            raise RuntimeError(f"Skill catalog fetch failed: HTTP {response.status} from {source_url}")

        try:
            payload = json.loads(response.body)
        except json.JSONDecodeError as exc:
            raise SkillCatalogValidationError(
                f"catalog response from {source_url} was not valid JSON: {exc}",
            ) from exc

        catalog = validate_catalog(payload)

        new_entry = CacheEntry(
            fetched_at=datetime.now(tz=UTC).isoformat(),
            etag=response.etag,
            source_url=source_url,
            catalog=payload,
        )
        _write_cache(self._cache_path, new_entry)
        return FetchResult(
            catalog=catalog,
            from_cache=False,
            revalidated=True,
            source_url=source_url,
        )


__all__ = [
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
]
