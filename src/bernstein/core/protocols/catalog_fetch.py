"""Conditional HTTP fetch with a shared on-disk cache for published catalogs.

The MCP catalog and the skill catalog are both a JSON document published at
a URL, validated against a strict schema, and cached on disk. This module
owns the part they share:

* **No request inside the revalidation window.** A cached response younger
  than ``revalidate_seconds`` is answered from disk.
* **Conditional revalidation after it.** The request carries
  ``If-None-Match`` with the ``ETag`` of the last response received, and a
  ``304`` means "what you hold is current".
* **Rejected bodies are remembered too.** A ``200`` whose body fails schema
  validation is recorded by its ``ETag`` next to the last valid catalog
  (which stays untouched). Until the body changes, every run gets the same
  validation error without downloading or parsing it again.
* **One cache file shared by parallel workers.** The default location is
  user-level (``$XDG_CACHE_HOME/bernstein/`` or ``~/.cache/bernstein/``),
  overridable per catalog by an environment variable. Writes go through a
  uniquely named temp file and an atomic rename, so concurrent writers never
  clobber each other and readers never see a torn file. A failed cache write
  is logged, not raised: the fetched catalog is still returned.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from bernstein.core.persistence.atomic_write import write_atomic_text
from bernstein.core.security.url_allowlist import (
    StrictHTTPRedirectHandler,
    ensure_public_http_url,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def user_cache_path(filename: str, *, env_override: str) -> Path:
    """Return the user-level cache file for a catalog.

    Resolution order: the ``env_override`` variable when set, then
    ``$XDG_CACHE_HOME/bernstein/<filename>``, then
    ``~/.cache/bernstein/<filename>``. The result never depends on the
    current directory, so every worktree and worker resolves the same file.
    """
    override = os.environ.get(env_override)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "bernstein" / filename


@dataclass(frozen=True)
class HTTPResponse:
    """Minimal HTTP response shape the fetcher consumes.

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


class UrllibTransport:
    """Default transport backed by :mod:`urllib.request`.

    The destination is checked before the request and on every redirect,
    so a hostile catalog URL cannot become a ``file://`` reader or reach an
    internal, loopback or link-local address (SSRF). TOCTOU between
    resolve and connect is inherent without IP pinning.
    """

    def __init__(self, *, allow_http: bool, source: str) -> None:
        self._allow_http = allow_http
        self._source = source

    def get(self, url: str, *, headers: dict[str, str]) -> HTTPResponse:
        ensure_public_http_url(url, allow_http=self._allow_http, source=self._source)
        request = urllib.request.Request(url, headers=headers)
        opener = urllib.request.build_opener(
            StrictHTTPRedirectHandler(allow_http=self._allow_http, source=self._source)
        )
        try:
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with opener.open(request, timeout=15) as resp:
                return HTTPResponse(status=resp.status, body=resp.read(), etag=resp.headers.get("ETag"))
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp is not None else b""
            etag = exc.headers.get("ETag") if exc.headers is not None else None
            return HTTPResponse(status=exc.code, body=body, etag=etag)


@dataclass(frozen=True)
class RejectedResponse:
    """The most recent ``200`` body that failed validation, kept by its ETag."""

    etag: str | None
    fetched_at: str
    source_url: str
    reason: str


@dataclass(frozen=True)
class CacheEntry:
    """A persisted catalog cache entry.

    ``catalog`` is the last body that passed validation (``None`` when none
    ever did). ``rejected`` is set only while the most recent response is a
    rejected body; accepting a valid body clears it.
    """

    fetched_at: str
    etag: str | None
    source_url: str
    catalog: dict[str, Any] | None
    rejected: RejectedResponse | None = None

    @property
    def last_checked_at(self) -> str:
        """When the origin was last asked, for either kind of response."""
        return self.rejected.fetched_at if self.rejected else self.fetched_at

    @property
    def validator(self) -> str | None:
        """The ETag of the most recent response, sent as ``If-None-Match``."""
        return self.rejected.etag if self.rejected else self.etag


@dataclass(frozen=True)
class FetchResult[C]:
    """Outcome of a catalog fetch.

    Attributes:
        catalog: The validated catalog.
        from_cache: Whether the body came from the local cache (no request,
            or the request returned 304).
        revalidated: Whether the origin was contacted during this fetch.
        source_url: Either the primary URL or the mirror URL.
    """

    catalog: C
    from_cache: bool
    revalidated: bool
    source_url: str


def read_cache(cache_path: Path) -> CacheEntry | None:
    """Load a cache file. Returns ``None`` on any read or parse error."""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    catalog = data.get("catalog")
    rejected = _parse_rejected(data.get("rejected"))
    if not isinstance(catalog, dict):
        if rejected is None:
            return None
        catalog = None
    etag = data.get("etag")
    return CacheEntry(
        fetched_at=str(data.get("fetched_at") or ""),
        etag=etag if isinstance(etag, str) else None,
        source_url=str(data.get("source_url") or ""),
        catalog=catalog,
        rejected=rejected,
    )


def _parse_rejected(raw: object) -> RejectedResponse | None:
    if not isinstance(raw, dict):
        return None
    etag = raw.get("etag")
    return RejectedResponse(
        etag=etag if isinstance(etag, str) else None,
        fetched_at=str(raw.get("fetched_at") or ""),
        source_url=str(raw.get("source_url") or ""),
        reason=str(raw.get("reason") or "catalog failed validation"),
    )


def write_cache(cache_path: Path, entry: CacheEntry) -> None:
    """Persist a cache entry atomically; log instead of raising on failure."""
    payload: dict[str, Any] = {
        "fetched_at": entry.fetched_at,
        "etag": entry.etag,
        "source_url": entry.source_url,
        "catalog": entry.catalog,
    }
    if entry.rejected is not None:
        payload["rejected"] = {
            "etag": entry.rejected.etag,
            "fetched_at": entry.rejected.fetched_at,
            "source_url": entry.rejected.source_url,
            "reason": entry.rejected.reason,
        }
    try:
        write_atomic_text(cache_path, json.dumps(payload, sort_keys=True), mode=0o644)
    except OSError as exc:
        logger.warning("Could not write catalog cache %s: %s", cache_path, exc)


def _is_fresh(checked_at: str, *, revalidate_seconds: int, now: datetime) -> bool:
    if not checked_at:
        return False
    try:
        ts = datetime.fromisoformat(checked_at)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return now - ts < timedelta(seconds=revalidate_seconds)


class ConditionalCatalogFetcher[C]:
    """Fetch a published catalog with conditional revalidation and a mirror.

    Subclasses bind the catalog kind: its validator, the error it raises,
    the ``User-Agent`` and the URLs.

    Args:
        primary_url: Primary catalog URL.
        mirror_url: Mirror used when the primary answers 5xx or is unreachable.
        cache_path: Cache file location.
        revalidate_seconds: Answer from the cache with no request while the
            last response is this fresh.
        transport: HTTP transport.
        validate: Parses a JSON object into the catalog or raises ``error_type``.
        error_type: The catalog's validation error.
        user_agent: ``User-Agent`` header value.
        label: Human name used in log and error messages.
    """

    def __init__(
        self,
        *,
        primary_url: str,
        mirror_url: str,
        cache_path: Path,
        revalidate_seconds: int,
        transport: HTTPTransport,
        validate: Callable[[Any], C],
        error_type: type[ValueError],
        user_agent: str,
        label: str,
    ) -> None:
        self._primary_url = primary_url
        self._mirror_url = mirror_url
        self._cache_path = cache_path
        self._revalidate_seconds = revalidate_seconds
        self._transport = transport
        self._validate = validate
        self._error_type = error_type
        self._user_agent = user_agent
        self._label = label

    @property
    def cache_path(self) -> Path:
        """The cache file location."""
        return self._cache_path

    def cached(self) -> C | None:
        """Return the last validated catalog, or ``None``.

        A cached copy that no longer validates is treated as missing.
        """
        entry = read_cache(self._cache_path)
        return self._valid_catalog(entry, warn=True)

    def _valid_catalog(self, entry: CacheEntry | None, *, warn: bool = False) -> C | None:
        if entry is None or entry.catalog is None:
            return None
        try:
            return self._validate(entry.catalog)
        except self._error_type as exc:
            if warn:
                logger.warning("Cached %s failed re-validation: %s", self._label, exc)
            return None

    def fetch(self, *, force: bool = False, now: datetime | None = None) -> FetchResult[C]:
        """Fetch the catalog, honouring the revalidation window.

        Args:
            force: Skip the window and revalidate now (still conditional).
            now: Override the wall clock (testing only).

        Raises:
            ValueError: The catalog's ``error_type`` when the body fails
                validation, including an unchanged body rejected earlier.
                The last valid catalog in the cache is kept.
            RuntimeError: When the origin and the mirror fail and no valid
                cached copy exists.
        """
        current = now or datetime.now(tz=UTC)
        entry = read_cache(self._cache_path)

        if (
            not force
            and entry is not None
            and _is_fresh(entry.last_checked_at, revalidate_seconds=self._revalidate_seconds, now=current)
        ):
            if entry.rejected is not None:
                logger.debug("%s body unchanged since %s; not re-downloaded", self._label, entry.rejected.fetched_at)
                raise self._error_type(entry.rejected.reason)
            catalog = self._valid_catalog(entry)
            if catalog is not None:
                return FetchResult(
                    catalog=catalog,
                    from_cache=True,
                    revalidated=False,
                    source_url=entry.source_url or self._primary_url,
                )

        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        if entry is not None and entry.validator:
            headers["If-None-Match"] = entry.validator

        response, source_url = self._request(headers)
        stamp = current.isoformat()

        if response.status == 304 and entry is not None:
            return self._not_modified(entry, source_url=source_url, stamp=stamp)

        if response.status >= 400:
            catalog = self._valid_catalog(entry)
            if entry is not None and catalog is not None:
                logger.warning("%s fetch returned %d; serving stale cache", self._label, response.status)
                return FetchResult(
                    catalog=catalog,
                    from_cache=True,
                    revalidated=True,
                    source_url=entry.source_url or source_url,
                )
            raise RuntimeError(f"{self._label} fetch failed: HTTP {response.status} from {source_url}")

        try:
            payload = json.loads(response.body)
            catalog = self._validate(payload)
        except (ValueError, self._error_type) as exc:
            reason = str(exc)
            if not isinstance(exc, self._error_type):
                reason = f"{self._label} response from {source_url} was not valid JSON: {exc}"
            self._remember_rejection(entry, response.etag, source_url=source_url, stamp=stamp, reason=reason)
            raise self._error_type(reason) from exc

        write_cache(
            self._cache_path,
            CacheEntry(fetched_at=stamp, etag=response.etag, source_url=source_url, catalog=payload),
        )
        return FetchResult(catalog=catalog, from_cache=False, revalidated=True, source_url=source_url)

    def _request(self, headers: dict[str, str]) -> tuple[HTTPResponse, str]:
        try:
            response = self._transport.get(self._primary_url, headers=headers)
        except (TimeoutError, OSError) as exc:
            logger.info("Primary %s fetch failed (%s); trying mirror", self._label, exc)
            return self._transport.get(self._mirror_url, headers=headers), self._mirror_url
        if 500 <= response.status < 600:
            logger.info("Primary %s returned %d; falling back to mirror", self._label, response.status)
            return self._transport.get(self._mirror_url, headers=headers), self._mirror_url
        return response, self._primary_url

    def _not_modified(self, entry: CacheEntry, *, source_url: str, stamp: str) -> FetchResult[C]:
        if entry.rejected is not None:
            rejected = RejectedResponse(
                etag=entry.rejected.etag,
                fetched_at=stamp,
                source_url=source_url,
                reason=entry.rejected.reason,
            )
            write_cache(self._cache_path, replace(entry, rejected=rejected))
            raise self._error_type(rejected.reason)
        catalog = self._valid_catalog(entry)
        if catalog is None:
            raise self._error_type(f"cached {self._label} failed re-validation after 304")
        write_cache(self._cache_path, replace(entry, fetched_at=stamp, source_url=source_url))
        return FetchResult(catalog=catalog, from_cache=True, revalidated=True, source_url=source_url)

    def _remember_rejection(
        self,
        entry: CacheEntry | None,
        etag: str | None,
        *,
        source_url: str,
        stamp: str,
        reason: str,
    ) -> None:
        rejected = RejectedResponse(etag=etag, fetched_at=stamp, source_url=source_url, reason=reason)
        base = entry or CacheEntry(fetched_at="", etag=None, source_url="", catalog=None)
        write_cache(self._cache_path, replace(base, rejected=rejected))


__all__ = [
    "CacheEntry",
    "ConditionalCatalogFetcher",
    "FetchResult",
    "HTTPResponse",
    "HTTPTransport",
    "RejectedResponse",
    "UrllibTransport",
    "read_cache",
    "user_cache_path",
    "write_cache",
]
