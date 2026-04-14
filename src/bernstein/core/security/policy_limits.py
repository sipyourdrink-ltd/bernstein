"""Enterprise policy limits with fail-open defaults and ETag caching.

Policy limits are an enterprise/team feature: restrictions are fetched from the
API with ETag conditional requests, cached locally, and polled in the background
every hour.

Fail-open semantics:
  - By default, if the policy fetch fails the feature is *allowed* (fail-open).
  - Features in ``ESSENTIAL_TRAFFIC_DENY_ON_MISS`` are *denied* on fetch failure.
    This set is intentionally small and reserved for compliance-critical paths
    (e.g. HIPAA controls).

A 30-second timeout on ``initialize()`` prevents policy-fetch deadlocks from
blocking startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Security-critical features that DENY when policy cannot be fetched.
# Fail-open is the default; this set is the explicit exception list.
# ---------------------------------------------------------------------------

ESSENTIAL_TRAFFIC_DENY_ON_MISS: frozenset[str] = frozenset(
    {
        # HIPAA compliance: do not allow product feedback when policy is unavailable
        "allow_product_feedback",
    }
)

# ---------------------------------------------------------------------------
# Default API endpoint.  Operators can override via ``api_url`` constructor arg.
# ---------------------------------------------------------------------------

_DEFAULT_API_URL = "https://api.bernstein.dev/v1/policy-limits"
_CACHE_FILENAME = "policy-limits.json"
_POLL_INTERVAL_SECONDS = 3600  # 1 hour
_INIT_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyLimitEntry:
    """A single feature restriction returned by the policy API."""

    feature: str
    enabled: bool
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {"feature": self.feature, "enabled": self.enabled, "metadata": self.metadata}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PolicyLimitEntry:
        """Deserialise from a cached dict."""
        raw_metadata: dict[str, Any] = dict(data.get("metadata") or {})
        return cls(
            feature=str(data["feature"]),
            enabled=bool(data.get("enabled", True)),
            metadata=raw_metadata,
        )


@dataclass
class PolicyLimitsSnapshot:
    """The full set of policy limits at a point in time."""

    limits: dict[str, PolicyLimitEntry] = field(default_factory=dict[str, PolicyLimitEntry])
    etag: str | None = None
    fetched_at: datetime | None = None

    @property
    def age_seconds(self) -> float | None:
        """Seconds since the snapshot was fetched, or None if never fetched."""
        if self.fetched_at is None:
            return None
        return (datetime.now(UTC) - self.fetched_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict for local caching."""
        return {
            "etag": self.etag,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "limits": {k: v.to_dict() for k, v in self.limits.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PolicyLimitsSnapshot:
        """Deserialise from a cached dict."""
        fetched_raw = data.get("fetched_at")
        fetched_at: datetime | None = None
        if fetched_raw:
            with contextlib.suppress(ValueError):
                fetched_at = datetime.fromisoformat(fetched_raw)

        raw_limits: dict[str, Any] = data.get("limits") or {}
        limits = {k: PolicyLimitEntry.from_dict(v) for k, v in raw_limits.items()}
        return cls(
            limits=limits,
            etag=data.get("etag"),
            fetched_at=fetched_at,
        )


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------


def _default_cache_dir() -> Path:
    """Return ``~/.bernstein/`` as the default cache directory."""
    return Path.home() / ".bernstein"


def _read_cache(cache_path: Path) -> PolicyLimitsSnapshot | None:
    """Load a cached snapshot from disk, returning None on any error."""
    if not cache_path.exists():
        return None
    try:
        raw: object = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return PolicyLimitsSnapshot.from_dict(cast("dict[str, Any]", raw))
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.debug("Could not read policy limits cache %s: %s", cache_path, exc)
        return None


def _write_cache(cache_path: Path, snapshot: PolicyLimitsSnapshot) -> None:
    """Persist a snapshot to disk.  Silently drops any I/O errors."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(snapshot.to_dict(), indent=2), encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not write policy limits cache %s: %s", cache_path, exc)


# ---------------------------------------------------------------------------
# HTTP fetch (async)
# ---------------------------------------------------------------------------


async def _fetch_limits_from_api(
    api_url: str,
    etag: str | None = None,
    timeout: float = 10.0,
) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch policy limits from the remote API.

    Uses an ``If-None-Match`` header when an ETag is available so the server
    can return 304 Not Modified and skip re-parsing unchanged policy.

    Returns:
        ``(payload, etag)`` where *payload* is None on 304 Not Modified or
        network error, and *etag* is the new ETag (or the old one on 304).
    """
    try:
        import httpx as _httpx
    except ImportError:
        logger.debug("httpx not available; skipping policy limits fetch")
        return None, etag

    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag

    try:
        async with _httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(api_url, headers=headers)

            if resp.status_code == 304:
                # Not Modified — reuse existing snapshot
                logger.debug("Policy limits not modified (ETag match)")
                return None, etag

            if resp.status_code != 200:
                logger.warning("Policy limits API returned %d; will fail-open", resp.status_code)
                return None, etag

            new_etag: str | None = resp.headers.get("ETag")
            payload: dict[str, Any] = resp.json()
            logger.debug("Fetched policy limits (etag=%s)", new_etag)
            return payload, new_etag

    except Exception as exc:
        logger.debug("Policy limits fetch failed: %s", exc)
        return None, etag


def _parse_payload(payload: dict[str, Any]) -> dict[str, PolicyLimitEntry]:
    """Parse the API response payload into a limits map."""
    raw_limits: list[Any] = payload.get("limits") or []
    result: dict[str, PolicyLimitEntry] = {}
    for item in raw_limits:
        if not isinstance(item, dict):
            continue
        item_dict = cast("dict[str, Any]", item)
        try:
            entry = PolicyLimitEntry.from_dict(item_dict)
            result[entry.feature] = entry
        except (KeyError, TypeError) as exc:
            logger.debug("Skipping malformed policy limit entry: %s — %s", item_dict, exc)
    return result


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------


class PolicyLimitsClient:
    """Manages fetching, caching, and querying enterprise policy limits.

    Usage::

        client = PolicyLimitsClient()
        await client.initialize()          # blocks up to 30 s
        allowed = client.is_allowed("allow_product_feedback")
        client.start_background_polling()  # non-blocking, asyncio task

    The client is *fail-open* by default — if the policy cannot be fetched, all
    features are considered enabled except those in
    :data:`ESSENTIAL_TRAFFIC_DENY_ON_MISS`.
    """

    def __init__(
        self,
        api_url: str = _DEFAULT_API_URL,
        cache_dir: Path | None = None,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
        init_timeout: float = _INIT_TIMEOUT_SECONDS,
        deny_on_miss: frozenset[str] = ESSENTIAL_TRAFFIC_DENY_ON_MISS,
    ) -> None:
        self._api_url = api_url
        self._cache_path = (cache_dir or _default_cache_dir()) / _CACHE_FILENAME
        self._poll_interval = poll_interval
        self._init_timeout = init_timeout
        self._deny_on_miss = deny_on_miss

        self._snapshot: PolicyLimitsSnapshot = PolicyLimitsSnapshot()
        self._initialized = False
        self._poll_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Load the policy limits, respecting the initialization timeout.

        1. Reads the local cache first so startup is instant on repeated runs.
        2. Attempts a live fetch (with ETag) to refresh stale data.

        The entire operation is bounded by ``init_timeout`` seconds so a
        slow or unreachable API never blocks agent startup.
        """
        # Try cache first — instant
        cached = _read_cache(self._cache_path)
        if cached is not None:
            self._snapshot = cached
            logger.debug(
                "Loaded %d policy limits from cache (age=%.0fs)",
                len(self._snapshot.limits),
                self._snapshot.age_seconds or 0,
            )

        # Attempt live fetch within the timeout
        try:
            async with asyncio.timeout(self._init_timeout):
                await self._refresh()
        except TimeoutError:
            logger.warning(
                "Policy limits fetch timed out after %ss; using %s",
                self._init_timeout,
                "cache" if cached else "fail-open defaults",
            )
        except Exception as exc:
            logger.warning("Policy limits initialization failed: %s; using fail-open defaults", exc)

        self._initialized = True

    def is_allowed(self, feature: str) -> bool:
        """Return True if *feature* is allowed under the current policy.

        Fail-open semantics:
        - If the policy has never been fetched and the feature is not in
          ``deny_on_miss``, return True (fail-open).
        - If the policy has never been fetched and the feature *is* in
          ``deny_on_miss``, return False (deny-on-miss).
        - If the policy was fetched, return the explicit value or True when the
          feature is absent from the response.
        """
        entry = self._snapshot.limits.get(feature)

        if entry is None:
            # Never fetched or feature absent from policy
            if feature in self._deny_on_miss:
                logger.debug("Policy limit missing for %r (deny-on-miss); denying", feature)
                return False
            # Fail-open
            return True

        return entry.enabled

    def get_snapshot(self) -> PolicyLimitsSnapshot:
        """Return the current in-memory snapshot (may be empty)."""
        return self._snapshot

    def start_background_polling(self) -> None:
        """Schedule background polling every ``poll_interval`` seconds.

        Safe to call multiple times — only one task is created.
        """
        if self._poll_task is not None and not self._poll_task.done():
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("No running event loop; cannot start background polling")
            return

        self._poll_task = loop.create_task(self._poll_loop(), name="policy-limits-poller")
        logger.debug("Started policy limits background polling (interval=%ss)", self._poll_interval)

    def stop_background_polling(self) -> None:
        """Cancel the background polling task if running."""
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            self._poll_task = None
            logger.debug("Stopped policy limits background polling")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _refresh(self) -> None:
        """Fetch fresh limits from the API and update cache."""
        payload, new_etag = await _fetch_limits_from_api(
            self._api_url,
            etag=self._snapshot.etag,
        )

        if payload is None:
            # 304 Not Modified or network error — keep existing snapshot
            return

        new_limits = _parse_payload(payload)
        self._snapshot = PolicyLimitsSnapshot(
            limits=new_limits,
            etag=new_etag,
            fetched_at=datetime.now(UTC),
        )
        _write_cache(self._cache_path, self._snapshot)
        logger.info(
            "Policy limits refreshed: %d features (etag=%s)",
            len(self._snapshot.limits),
            new_etag,
        )

    async def _poll_loop(self) -> None:
        """Periodically refresh policy limits until cancelled."""
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Background policy refresh failed: %s", exc)


# ---------------------------------------------------------------------------
# Module-level singleton helper
# ---------------------------------------------------------------------------

_global_client: PolicyLimitsClient | None = None


def get_client(
    api_url: str = _DEFAULT_API_URL,
    cache_dir: Path | None = None,
) -> PolicyLimitsClient:
    """Return a (lazily created) module-level :class:`PolicyLimitsClient`.

    The singleton is useful for CLI commands that don't manage their own event
    loop lifecycle.  Tests should create their own instances.
    """
    global _global_client
    if _global_client is None:
        _global_client = PolicyLimitsClient(api_url=api_url, cache_dir=cache_dir)
    return _global_client


def is_allowed_sync(
    feature: str,
    *,
    snapshot: PolicyLimitsSnapshot | None = None,
    deny_on_miss: frozenset[str] = ESSENTIAL_TRAFFIC_DENY_ON_MISS,
) -> bool:
    """Synchronous feature-gate check against a snapshot.

    Suitable for use in non-async code that already has a cached snapshot.
    Falls back to fail-open (or deny-on-miss) when *snapshot* is None or
    the feature is absent.

    Args:
        feature: The feature name to check.
        snapshot: A pre-fetched :class:`PolicyLimitsSnapshot`, or None.
        deny_on_miss: Features that should deny when not present in policy.

    Returns:
        True if the feature is allowed.
    """
    if snapshot is None:
        return feature not in deny_on_miss

    entry = snapshot.limits.get(feature)
    if entry is None:
        return feature not in deny_on_miss

    return entry.enabled


# ---------------------------------------------------------------------------
# Async context-manager convenience
# ---------------------------------------------------------------------------


class managed_policy_limits:
    """Async context manager that initialises and tears down a client.

    Example::

        async with managed_policy_limits() as client:
            if client.is_allowed("allow_product_feedback"):
                ...
    """

    def __init__(
        self,
        api_url: str = _DEFAULT_API_URL,
        cache_dir: Path | None = None,
        poll: bool = True,
    ) -> None:
        self._client = PolicyLimitsClient(api_url=api_url, cache_dir=cache_dir)
        self._poll = poll

    async def __aenter__(self) -> PolicyLimitsClient:
        await self._client.initialize()
        if self._poll:
            self._client.start_background_polling()
        return self._client

    async def __aexit__(self, *_: object) -> None:
        self._client.stop_background_polling()
