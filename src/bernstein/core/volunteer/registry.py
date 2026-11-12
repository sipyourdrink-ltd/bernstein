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
from collections import deque
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from bernstein.core.protocols.volunteer.documents import canonical_bytes, verify_document
from bernstein.core.security.audit_dsse import EnvelopeFormatError, parse_envelope
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

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger(__name__)

#: Path components for a project's volunteer manifest, relative to the repo root.
MANIFEST_SUBPATH = ".bernstein/volunteer.json"

#: Default revalidation window (6 hours).
DEFAULT_REVALIDATE_SECONDS = 6 * 3600

#: Environment variable that overrides the cache TTL.
TTL_ENV = "BERNSTEIN_VOLUNTEER_TTL"


def env_ttl_seconds(default: int = DEFAULT_REVALIDATE_SECONDS) -> int:
    """Read the registry cache TTL override, falling back for invalid values."""
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
    """Minimal HTTP response used by the registry client."""

    status: int
    body: bytes
    etag: str | None


class HTTPTransport(Protocol):
    """Injectable HTTP GET boundary for volunteer registry fetches."""

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
    """A repository listing advertised by a volunteer index."""

    repo_url: str
    default_branch: str
    topics: tuple[str, ...]
    license: str
    local_ok: bool
    added_by: str | None = None


@dataclass(frozen=True)
class BrowseResult:
    """A volunteer listing whose manifest was loaded and accepted."""

    repo_url: str
    default_branch: str
    manifest: VolunteerManifest
    manifest_url: str
    topics: tuple[str, ...]
    digest: str


@dataclass(frozen=True)
class DroppedEntry:
    """A repository omitted from volunteer results and the reason."""

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
        added_by = entry.get("added_by")
        if not isinstance(added_by, str):
            added_by = None
        entries.append(
            IndexEntry(
                repo_url=repo_url,
                default_branch=str(entry.get("default_branch", "main")),
                topics=tuple(entry.get("topics", [])),
                license=str(entry.get("license", "")),
                local_ok=bool(entry.get("local_ok", False)),
                added_by=added_by,
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


@dataclass(frozen=True)
class FederationPolicy:
    """Donor-selected trust and budgets; unsigned indexes require explicit opt-in.

    Pins and denials use exact URLs. A pin always requires a valid signature;
    allowing unsigned documents never bypasses a pin or an invalid signature.
    """

    max_depth: int = 3
    max_indexes: int = 100
    pinned_keys: Mapping[str, Ed25519PublicKey] = field(default_factory=dict)
    allow_unsigned: bool = False
    denied_indexes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name, minimum in (("max_depth", 0), ("max_indexes", 1)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if not isinstance(self.allow_unsigned, bool):
            raise ValueError("allow_unsigned must be a boolean")
        object.__setattr__(self, "pinned_keys", MappingProxyType(dict(self.pinned_keys)))
        object.__setattr__(self, "denied_indexes", frozenset(self.denied_indexes))


@dataclass(frozen=True)
class FederationResolution:
    """Discovered pointers and diagnostics; entries never replace real manifests."""

    entries: tuple[IndexEntry, ...]
    visited_indexes: tuple[str, ...]
    skipped: tuple[DroppedEntry, ...]
    cycle_members: tuple[str, ...]
    conflicting_projects: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """Whether traversal completed without refusals or budget truncation."""
        return not self.skipped

    def as_record(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "projects": [entry.repo_url for entry in self.entries],
            "visited_indexes": list(self.visited_indexes),
            "skipped": [{"index_url": item.repo_url, "reason": item.reason} for item in self.skipped],
            "cycle_members": list(self.cycle_members),
            "conflicting_projects": list(self.conflicting_projects),
        }


def _federated_document(body: bytes, url: str, policy: FederationPolicy) -> dict[str, Any]:
    raw = json.loads(body)
    if not isinstance(raw, dict):
        raise ValueError("index must be a JSON object")
    # Presence matters: an explicit null signature is malformed, not unsigned.
    has_signature = "signature" in raw
    signature = raw.pop("signature", None)
    pin = policy.pinned_keys.get(url)
    if has_signature:
        if pin is None:
            raise ValueError("signature requires a donor-pinned key")
        if not isinstance(signature, dict):
            raise ValueError("signature must be a DSSE envelope")
        try:
            verified = verify_document(parse_envelope(signature), pin, expected_document_kind="registry-index")
        except (EnvelopeFormatError, ValueError, TypeError) as exc:
            raise ValueError(f"signature invalid: {exc}") from exc
        if not verified.ok:
            raise ValueError("signature invalid: " + "; ".join(verified.errors))
        if canonical_bytes(verified.document) != canonical_bytes(raw):
            raise ValueError("signature document does not match the index")
    elif pin is not None:
        raise ValueError("signature required for a pinned index")
    elif not policy.allow_unsigned:
        raise ValueError("unsigned index refused by donor policy")
    version = raw.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version not in (1, 2):
        raise ValueError("index version must be 1 or 2")
    children = raw.get("indexes", [])
    if not isinstance(children, list) or any(not isinstance(child, str) or not child for child in children):
        raise ValueError("indexes must be a list of nonempty URL strings")
    return raw


def _cycle_for_edge(source: str, target: str, graph: dict[str, list[str]]) -> tuple[str, ...]:
    """Name all members of cycles closed by this edge, excluding diamonds."""

    def reachable(start: str, edges: dict[str, list[str]]) -> set[str]:
        pending = deque([start])
        seen: set[str] = set()
        while pending:
            node = pending.popleft()
            if node in seen:
                continue
            seen.add(node)
            pending.extend(child for child in edges.get(node, []) if child not in seen)
        return seen

    forward = reachable(target, graph)
    if source not in forward:
        return ()
    reverse: dict[str, list[str]] = {}
    for parent, children in graph.items():
        for child in children:
            reverse.setdefault(child, []).append(parent)
    return tuple(sorted(forward & reachable(source, reverse)))


def resolve_indexes(
    index_urls: list[str],
    *,
    policy: FederationPolicy,
    transport: HTTPTransport | None = None,
) -> FederationResolution:
    """Resolve index-of-indexes breadth-first, using exact URLs as identities.

    Root depth is zero. At most max_indexes distinct URLs are scheduled/fetched;
    mirrored content remains distinct and consumes the same budget. No project
    manifests are fetched here: conflicts are reported for the browse/join caller.
    """
    transport = transport or _UrllibTransport()
    pending: deque[tuple[str, int]] = deque()
    seen: set[str] = set()
    admitted = 0
    skipped: list[DroppedEntry] = []
    visited: list[str] = []
    entries: list[IndexEntry] = []
    graph: dict[str, list[str]] = {}
    cycles: set[str] = set()
    claims: dict[str, bytes] = {}
    conflicts: set[str] = set()

    def schedule(url: str, depth: int) -> None:
        nonlocal admitted
        if url in seen:
            return
        seen.add(url)
        if url in policy.denied_indexes:
            reason = "index denied by donor policy"
        elif depth > policy.max_depth:
            reason = "index exceeds depth limit"
        elif admitted >= policy.max_indexes:
            reason = "index count limit reached"
        else:
            ok, error = _validate_index_url(url)
            if ok:
                pending.append((url, depth))
                admitted += 1
                return
            reason = f"index URL rejected: {error}"
        skipped.append(DroppedEntry(url, reason))

    for url in index_urls:
        schedule(url, 0)
    while pending:
        url, depth = pending.popleft()
        visited.append(url)
        try:
            response = transport.get(
                url, headers={"User-Agent": "bernstein-volunteer/1.0", "Accept": "application/json"}
            )
            if response.status >= 400:
                raise ValueError(f"index fetch returned HTTP {response.status}")
            raw = _federated_document(response.body, url, policy)
            parsed = _parse_index(canonical_bytes(raw), url)
        except (ValueError, TypeError, OSError, UrlSchemeError) as exc:
            skipped.append(DroppedEntry(url, str(exc)))
            continue
        entries.extend(parsed)
        for project in raw.get("projects", []):
            if not isinstance(project, dict) or not project.get("repo_url"):
                continue
            repo = str(project["repo_url"])
            claim = canonical_bytes({key: value for key, value in project.items() if key != "added_by"})
            if repo in claims and claims[repo] != claim:
                conflicts.add(repo)
            claims.setdefault(repo, claim)
        # Keep only admitted nodes in the graph so the index budget also bounds
        # graph traversal. Check each edge before adding it to detect real cycles.
        graph[url] = []
        for child in dict.fromkeys(raw.get("indexes", [])):
            schedule(child, depth + 1)
            if child not in {item[0] for item in pending} and child not in graph:
                continue
            cycles.update(_cycle_for_edge(url, child, graph))
            graph[url].append(child)
    return FederationResolution(
        tuple(_merge_dedupe(entries)), tuple(visited), tuple(skipped), tuple(sorted(cycles)), tuple(sorted(conflicts))
    )


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
    federation: FederationPolicy | None = None,
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

    if federation is not None:
        resolution = resolve_indexes(index_urls, policy=federation, transport=transport)
        all_entries.extend(resolution.entries)
        dropped.extend(resolution.skipped)
    else:
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
