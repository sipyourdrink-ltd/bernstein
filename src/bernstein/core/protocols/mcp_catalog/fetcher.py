"""ETag-aware catalog fetcher with GitHub mirror fallback.

The primary source is ``https://bernstein.run/mcp-catalog.json``. On any
5xx response Bernstein falls back to the public GitHub mirror. The
fetched payload is validated against the strict schema. On validation
failure any previously cached copy is preserved untouched (acceptance
criterion).
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

from bernstein.core.protocols.mcp_catalog.manifest import (
    Catalog,
    CatalogValidationError,
    validate_catalog,
)

logger = logging.getLogger(__name__)

#: Primary catalog URL.
DEFAULT_CATALOG_URL = "https://bernstein.run/mcp-catalog.json"

#: GitHub mirror used as fallback on 5xx errors against the primary URL.
DEFAULT_MIRROR_URL = (
    "https://raw.githubusercontent.com/chernistry/bernstein-mcp-catalog/main/mcp-catalog.json"
)

#: Default revalidation window (6h, configurable via mcp.catalog.revalidate_interval).
DEFAULT_REVALIDATE_SECONDS = 6 * 3600

#: Default upgrade-cadence check interval (24h).
DEFAULT_CHECK_INTERVAL_SECONDS = 24 * 3600


def default_cache_path() -> Path:
    """Return the default cache path under ``~/.cache/bernstein/``.

    Honours ``XDG_CACHE_HOME`` when set.
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "bernstein" / "mcp-catalog.json"


@dataclass(frozen=True)
class HTTPResponse:
    """Minimal HTTP response shape this module consumes.

    A class rather than a tuple so the fetcher can be unit-tested with a
    fake transport that doesn't import ``urllib``.

    Attributes:
        status: HTTP status code. ``304`` means "use the cached body".
        body: Response body bytes. Empty for ``304``.
        etag: Value of the ``ETag`` response header, if any.
    """

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
        request = urllib.request.Request(url, headers=headers)
        try:
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
    """Outcome of :meth:`CatalogFetcher.fetch`.

    Attributes:
        catalog: Validated :class:`Catalog`.
        from_cache: Whether the body was served from the local cache (no
            network revalidation, or revalidation returned 304).
        revalidated: Whether the cache was revalidated against the
            origin server during this fetch.
        source_url: Either the primary URL or the mirror URL.
    """

    catalog: Catalog
    from_cache: bool
    revalidated: bool
    source_url: str


def _read_cache(cache_path: Path) -> CacheEntry | None:
    """Load a cache file. Returns ``None`` on any read or parse error."""
    if not cache_path.exists():
        return None
    try:
        raw = cache_path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    catalog = data.get("catalog")
    if not isinstance(catalog, dict):
        return None
    return CacheEntry(
        fetched_at=str(data.get("fetched_at", "")),
        etag=data.get("etag") if isinstance(data.get("etag"), str) else None,
        source_url=str(data.get("source_url", "")),
        catalog=catalog,
    )


def _write_cache(cache_path: Path, entry: CacheEntry) -> None:
    """Persist a cache entry to disk."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": entry.fetched_at,
        "etag": entry.etag,
        "source_url": entry.source_url,
        "catalog": entry.catalog,
    }
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True))
    tmp.replace(cache_path)


def _is_within_revalidate_window(
    fetched_at: str, *, revalidate_seconds: int, now: datetime | None = None
) -> bool:
    """Return True when the cache is fresh enough to skip revalidation."""
    if not fetched_at:
        return False
    try:
        ts = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    current = now or datetime.now(tz=UTC)
    return current - ts < timedelta(seconds=revalidate_seconds)


class CatalogFetcher:
    """Fetch the MCP catalog with ETag revalidation and a mirror fallback.

    Args:
        primary_url: Primary catalog URL.
        mirror_url: GitHub mirror URL used on 5xx from the primary.
        cache_path: Cache file location.
        revalidate_seconds: Skip the network entirely while the cache is
            this fresh.
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
        self._primary_url = primary_url
        self._mirror_url = mirror_url
        self._cache_path = cache_path or default_cache_path()
        self._revalidate_seconds = revalidate_seconds
        self._transport: HTTPTransport = transport or _UrllibTransport()

    @property
    def cache_path(self) -> Path:
        """The cache file location."""
        return self._cache_path

    def cached(self) -> Catalog | None:
        """Return the cached catalog if any, validated; else ``None``.

        Validation errors on the cache are non-fatal: the cache is
        treated as missing.
        """
        entry = _read_cache(self._cache_path)
        if entry is None:
            return None
        try:
            return validate_catalog(entry.catalog)
        except CatalogValidationError as exc:
            logger.warning("Cached catalog failed re-validation: %s", exc)
            return None

    def fetch(self, *, force: bool = False, now: datetime | None = None) -> FetchResult:
        """Fetch the catalog, honouring the revalidation window.

        Args:
            force: Skip the freshness window and always revalidate.
            now: Override the wall clock (testing only).

        Returns:
            A :class:`FetchResult` with the validated catalog.

        Raises:
            CatalogValidationError: If the fetched body fails strict
                schema validation. The cache is preserved untouched.
            RuntimeError: If both primary and mirror requests fail and no
                cache is available.
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
            except CatalogValidationError:
                cached_entry = None
            else:
                return FetchResult(
                    catalog=catalog,
                    from_cache=True,
                    revalidated=False,
                    source_url=cached_entry.source_url or self._primary_url,
                )

        headers: dict[str, str] = {
            "User-Agent": "bernstein-mcp-catalog/1.0",
            "Accept": "application/json",
        }
        if cached_entry is not None and cached_entry.etag:
            headers["If-None-Match"] = cached_entry.etag

        try:
            response = self._transport.get(self._primary_url, headers=headers)
            source_url = self._primary_url
        except (TimeoutError, OSError) as exc:
            logger.info("Primary catalog fetch failed (%s); trying mirror", exc)
            response = self._transport.get(self._mirror_url, headers=headers)
            source_url = self._mirror_url
        else:
            if 500 <= response.status < 600:
                logger.info(
                    "Primary catalog returned %d; falling back to mirror",
                    response.status,
                )
                response = self._transport.get(self._mirror_url, headers=headers)
                source_url = self._mirror_url

        if response.status == 304 and cached_entry is not None:
            try:
                catalog = validate_catalog(cached_entry.catalog)
            except CatalogValidationError as exc:  # pragma: no cover - defensive
                raise CatalogValidationError(
                    "cached catalog failed re-validation after 304"
                ) from exc
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
                except CatalogValidationError:
                    pass
                else:
                    logger.warning(
                        "catalog fetch returned %d; serving stale cache",
                        response.status,
                    )
                    return FetchResult(
                        catalog=catalog,
                        from_cache=True,
                        revalidated=True,
                        source_url=cached_entry.source_url or source_url,
                    )
            raise RuntimeError(
                f"Catalog fetch failed: HTTP {response.status} from {source_url}"
            )

        try:
            payload = json.loads(response.body)
        except json.JSONDecodeError as exc:
            raise CatalogValidationError(
                f"catalog response from {source_url} was not valid JSON: {exc}"
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
