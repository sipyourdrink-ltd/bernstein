"""Volunteer project registry: fetch, merge, and filter opt-in indexes.

Indexes are JSON documents served over HTTPS. Each index lists projects that
have opted into the volunteer-workers program. The project's own
``.bernstein/volunteer.json`` (loaded by :func:`manifest.load_manifest`) is
the trust anchor; an index row is only a pointer.

Unknown fields in an index entry are ignored silently -- an index format that
grows a field in the future should not break every older ``browse`` pointed
at it. The trust anchor is still the project's own manifest, validated
separately by :func:`load_manifest`.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from bernstein.core.security.url_allowlist import (
    StrictHTTPRedirectHandler,
    UrlSchemeError,
    ensure_http_url,
    ensure_public_http_url,
)
from bernstein.core.volunteer.manifest import (
    VolunteerManifest,
    VolunteerManifestError,
    load_manifest,
)

logger = logging.getLogger(__name__)

#: Path components for a project's volunteer manifest, relative to the repo root.
MANIFEST_SUBPATH = ".bernstein/volunteer.json"

#: Default revalidation window (6 hours).
DEFAULT_REVALIDATE_SECONDS = 6 * 3600

#: Environment variable that overrides the cache TTL.
TTL_ENV = "BERNSTEIN_VOLUNTEER_TTL"


def env_ttl_seconds(default: int = DEFAULT_REVALIDATE_SECONDS) -> int:
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
    status: int
    body: bytes
    etag: str | None


class HTTPTransport(Protocol):
    def get(self, url: str, *, headers: dict[str, str]) -> HTTPResponse: ...


class _UrllibTransport:
    def get(self, url: str, *, headers: dict[str, str]) -> HTTPResponse:
        ensure_public_http_url(url, allow_http=False, source="volunteer.registry")
        request = urllib.request.Request(url, headers=headers)
        # ``urlopen`` follows redirects automatically; a public host that
        # redirects to an internal address would otherwise be unchecked.
        # ``StrictHTTPRedirectHandler`` re-runs the strict check on every
        # ``Location`` URL and raises ``UrlSchemeError`` to abort.
        # TOCTOU (resolve-then-connect) is inherent without IP pinning.
        opener = urllib.request.build_opener(StrictHTTPRedirectHandler(allow_http=False, source="volunteer.registry"))
        try:
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with opener.open(request, timeout=15) as resp:
                body = resp.read()
                etag = resp.headers.get("ETag")
                return HTTPResponse(status=resp.status, body=body, etag=etag)
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp is not None else b""
            etag = exc.headers.get("ETag") if exc.headers is not None else None
            return HTTPResponse(status=exc.code, body=body, etag=etag)


@dataclass(frozen=True)
class IndexEntry:
    repo_url: str
    default_branch: str
    topics: tuple[str, ...]
    license: str
    local_ok: bool


@dataclass(frozen=True)
class BrowseResult:
    repo_url: str
    default_branch: str
    manifest: VolunteerManifest
    manifest_url: str
    topics: tuple[str, ...]
    digest: str


@dataclass(frozen=True)
class DroppedEntry:
    repo_url: str
    reason: str


def _validate_index_url(url: str) -> tuple[bool, str | None]:
    """Validate an operator-configured index URL (permissive, scheme-only)."""
    try:
        ensure_http_url(url, allow_http=False, source="volunteer.registry")
        return True, None
    except Exception as exc:
        return False, str(exc)


def _validate_manifest_url(url: str) -> tuple[bool, str | None]:
    """Validate a third-party-derived manifest URL (strict, internal rejected)."""
    try:
        ensure_public_http_url(url, allow_http=False, source="volunteer.registry")
        return True, None
    except Exception as exc:
        return False, str(exc)


def _validate_url(url: str) -> tuple[bool, str | None]:
    """Validate URL is HTTPS before any transport call.

    Kept for backwards compatibility; delegates to the index (permissive) path.
    """
    return _validate_index_url(url)


def _parse_index(body: bytes, source_url: str) -> list[IndexEntry]:
    """Parse an index JSON document into entries.

    Unknown fields in an index entry are ignored silently.
    """
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"index from {source_url} was not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"index from {source_url} is not a JSON object")
    projects = raw.get("projects", [])
    if not isinstance(projects, list):
        raise ValueError(f"index from {source_url}: 'projects' is not a list")

    entries: list[IndexEntry] = []
    for entry in projects:
        if not isinstance(entry, dict):
            continue
        repo_url = str(entry.get("repo_url", ""))
        if not repo_url:
            continue
        entries.append(
            IndexEntry(
                repo_url=repo_url,
                default_branch=str(entry.get("default_branch", "main")),
                topics=tuple(entry.get("topics", [])),
                license=str(entry.get("license", "")),
                local_ok=bool(entry.get("local_ok", False)),
            )
        )
    return entries


def _merge_dedupe(entries: list[IndexEntry]) -> list[IndexEntry]:
    """Merge entries from multiple indexes, deduplicating by repo_url."""
    seen: dict[str, IndexEntry] = {}
    for entry in entries:
        if entry.repo_url not in seen:
            seen[entry.repo_url] = entry
    return list(seen.values())


def _manifest_url_for(entry: IndexEntry) -> str:
    """Construct the raw manifest URL for a project."""
    base = entry.repo_url.rstrip("/")
    return f"{base}/raw/{entry.default_branch}/{MANIFEST_SUBPATH}"


def _fetch_manifest(
    entry: IndexEntry,
    transport: HTTPTransport,
    *,
    headers: dict[str, str],
) -> tuple[VolunteerManifest | None, str | None]:
    """Fetch and validate the project's volunteer manifest."""
    url = _manifest_url_for(entry)
    ok, err = _validate_manifest_url(url)
    if not ok:
        return None, f"manifest URL rejected: {err}"

    try:
        response = transport.get(url, headers=headers)
    except (TimeoutError, OSError, UrlSchemeError) as exc:
        return None, f"fetch failed: {exc}"

    if response.status >= 400:
        return None, f"manifest fetch returned HTTP {response.status}"

    try:
        manifest = load_manifest(response.body)
    except VolunteerManifestError as exc:
        return None, f"{exc.field}: {exc}"

    return manifest, None


def _filter_results(
    results: list[BrowseResult],
    *,
    size: str | None = None,
    language: str | None = None,
    local_ok_only: bool = False,
    budget_minutes: int | None = None,
) -> list[BrowseResult]:
    """Apply donor filters to the list of joinable projects."""
    filtered = results
    filtered = [r for r in filtered if r.manifest.is_active]
    if local_ok_only:
        filtered = [r for r in filtered if r.manifest.local_ok]
    if budget_minutes is not None:
        filtered = [r for r in filtered if r.manifest.max_wall_clock_minutes <= budget_minutes]
    if language is not None:
        filtered = [r for r in filtered if language in r.topics]
    if size is not None:
        filtered = [r for r in filtered if f"size/{size}" in r.topics]
    return filtered


def browse_indexes(
    index_urls: list[str],
    *,
    transport: HTTPTransport | None = None,
    size: str | None = None,
    language: str | None = None,
    local_ok_only: bool = False,
    budget_minutes: int | None = None,
) -> tuple[list[BrowseResult], list[DroppedEntry]]:
    """Fetch volunteer indexes, merge, dedupe, validate manifests, and filter.

    Returns:
        (joinable, dropped) where ``joinable`` is the list of projects with
        valid manifests passing all filters, and ``dropped`` is the list of
        projects that were skipped with a reason.
    """
    transport = transport or _UrllibTransport()
    headers: dict[str, str] = {
        "User-Agent": "bernstein-volunteer/1.0",
        "Accept": "application/json",
    }

    all_entries: list[IndexEntry] = []
    dropped: list[DroppedEntry] = []

    for index_url in index_urls:
        ok, err = _validate_index_url(index_url)
        if not ok:
            dropped.append(DroppedEntry(repo_url=index_url, reason=f"URL scheme rejected: {err}"))
            continue

        try:
            response = transport.get(index_url, headers=headers)
        except (TimeoutError, OSError, UrlSchemeError) as exc:
            dropped.append(DroppedEntry(repo_url=index_url, reason=f"index fetch failed: {exc}"))
            continue

        if response.status >= 400:
            dropped.append(DroppedEntry(repo_url=index_url, reason=f"index fetch returned HTTP {response.status}"))
            continue

        try:
            entries = _parse_index(response.body, index_url)
        except ValueError as exc:
            dropped.append(DroppedEntry(repo_url=index_url, reason=str(exc)))
            continue

        all_entries.extend(entries)

    merged = _merge_dedupe(all_entries)

    joinable: list[BrowseResult] = []
    for entry in merged:
        manifest, reason = _fetch_manifest(entry, transport, headers=headers)
        if manifest is None:
            dropped.append(DroppedEntry(repo_url=entry.repo_url, reason=reason or "unknown error"))
            continue

        joinable.append(
            BrowseResult(
                repo_url=entry.repo_url,
                default_branch=entry.default_branch,
                manifest=manifest,
                manifest_url=_manifest_url_for(entry),
                topics=entry.topics,
                digest=manifest.digest,
            )
        )

    filtered = _filter_results(
        joinable,
        size=size,
        language=language,
        local_ok_only=local_ok_only,
        budget_minutes=budget_minutes,
    )

    # Record filtered-out entries in dropped with their filter reasons
    filtered_urls = {r.repo_url for r in filtered}
    for result in joinable:
        if result.repo_url not in filtered_urls:
            reasons: list[str] = []
            if not result.manifest.is_active:
                reasons.append("status=paused")
            if local_ok_only and not result.manifest.local_ok:
                reasons.append("local_ok=False")
            if budget_minutes is not None and result.manifest.max_wall_clock_minutes > budget_minutes:
                reasons.append(f"budget {result.manifest.max_wall_clock_minutes} > {budget_minutes}")
            if language is not None and language not in result.topics:
                reasons.append(f"language {language} not in topics")
            if size is not None and f"size/{size}" not in result.topics:
                reasons.append(f"size/{size} not in topics")
            dropped.append(DroppedEntry(repo_url=result.repo_url, reason=", ".join(reasons) or "filtered"))

    return filtered, dropped
