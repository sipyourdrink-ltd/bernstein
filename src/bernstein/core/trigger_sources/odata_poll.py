"""Generic OData v4 poll trigger source (watermark + opt-in delta).

Operations teams run on business systems of record whose integration surface
is OData v4. This module turns "a record changed in the system of record" into
the orchestrator's normal :class:`TriggerEvent` flow, so a changed order /
purchase / service object can seed a task through the same pipeline every other
trigger source uses.

Two polling modes share one deterministic cursor:

* **Watermark polling is the baseline.** Each poll queries
  ``$filter=<ts_prop> gt <watermark>`` ordered by the timestamp, emits one
  :class:`TriggerEvent` per changed entity, and advances the watermark. There
  is no assumption of a standard timestamp property name -- it is per-connection
  config, because none exists across vendors.
* **Delta links are an opt-in optimization, never assumed.** With
  ``prefer_delta`` set, the first poll probes with ``Prefer:
  odata.track-changes``; only if the service actually returns
  ``@odata.deltaLink`` does the source switch to delta-token paging. Any delta
  failure downgrades to watermark polling *without losing the cursor* -- the
  maintained watermark carries the resume position across the downgrade.

The cursor is a deterministic, content-addressable record so a restarted
operator resumes byte-identically: the same fake timeline replayed against the
same persisted cursor yields the same events with no duplicate and no drop.

Auth (OAuth2 client-credentials, static bearer, or API-key headers) is resolved
through environment variables and is never written to a log; header values for
the auth headers are redacted before any debug line is emitted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from bernstein.core.tasks.models import TriggerEvent

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

try:  # pragma: no cover - dep declared in pyproject
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__all__ = [
    "Clock",
    "OdataAuth",
    "OdataAuthError",
    "OdataConflict",
    "OdataConnection",
    "OdataCursor",
    "OdataError",
    "OdataHttpClient",
    "OdataPollSource",
    "PollResult",
    "RateLimited",
    "RealClock",
    "build_key_predicate",
    "discover_keys",
    "load_cursor",
    "save_cursor",
]

# Header names whose values must never be logged.
_SENSITIVE_HEADER_PREFIXES = ("authorization", "x-api-key", "api-key", "apikey", "cookie")

_METADATA_KEY_RE = re.compile(
    r'<EntityType\s+Name="(?P<name>[^"]+)".*?<Key>(?P<keys>.*?)</Key>',
    re.DOTALL,
)
_PROPERTY_REF_RE = re.compile(r'<PropertyRef\s+Name="(?P<name>[^"]+)"')


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


class Clock(Protocol):
    """Injected clock so rate-limit waits are testable without real sleeps."""

    def now(self) -> float:
        """Return a monotonic-ish timestamp in seconds."""
        ...

    def sleep(self, seconds: float) -> None:
        """Block for ``seconds`` (a no-op for non-positive values)."""
        ...


class RealClock:
    """Production clock backed by :mod:`time`."""

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OdataError(Exception):
    """Base class for OData integration errors."""


class OdataAuthError(OdataError):
    """Raised when credentials are missing or the service rejects them."""


class RateLimited(OdataError):
    """Raised when 429s persist beyond the retry budget."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class OdataConflict(OdataError):
    """Raised on an optimistic-concurrency conflict (412 / 428).

    ``status`` distinguishes ``412`` (a stale ``If-Match`` precondition) from
    ``428`` (a required precondition was absent). Neither is retried blindly so
    a concurrent human edit is never clobbered.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OdataAuth:
    """Auth configuration for an OData connection. Secrets stay in env vars.

    Attributes:
        kind: ``"none"``, ``"bearer"``, ``"api_key"``, or
            ``"oauth2_client_credentials"``.
        token_env: Env var holding the bearer / API-key secret.
        header_name: Header to place the credential in (default
            ``Authorization``).
        header_prefix: Prefix for a bearer credential (default ``"Bearer "``).
        token_url: OAuth2 token endpoint (client-credentials grant).
        client_id_env / client_secret_env: Env vars for the OAuth2 client.
        scope: Optional OAuth2 scope string.
    """

    kind: Literal["none", "bearer", "api_key", "oauth2_client_credentials"] = "none"
    token_env: str = ""
    header_name: str = "Authorization"
    header_prefix: str = "Bearer "
    token_url: str = ""
    client_id_env: str = ""
    client_secret_env: str = ""
    scope: str = ""


@dataclass(frozen=True)
class OdataConnection:
    """A single OData v4 connection.

    Attributes:
        service_root: Service root URL (e.g. ``https://host/odata/v4/svc``).
        entity_set: Entity-set name to poll / write back.
        timestamp_property: Change-timestamp property used by watermark polling.
            Per-connection because no standard name exists across vendors.
        key_properties: Key property name(s). When empty they are derived from
            ``$metadata`` via :func:`discover_keys`.
        filter: Optional extra ``$filter`` clause ANDed with the watermark
            predicate.
        start_watermark: Initial watermark for a fresh cursor.
        page_size: ``$top`` page hint (0 = server default; paging still follows
            ``@odata.nextLink``).
        poll_interval_s: Advisory minimum seconds between polls (the scheduler
            owns cadence; recorded here for operators).
        rate_limit_min_interval_s: Minimum seconds between HTTP calls.
        prefer_delta: Opt into probing for ``@odata.deltaLink``.
        auth: Auth configuration.
        draft_flow: Whether writes go through a draft-activate flow.
        draft_activate_action: Bound-action name that activates a draft.
        max_retry_after_s: Cap on an honored ``Retry-After`` value.
        name: Stable connection label used in event source / metadata.
    """

    service_root: str
    entity_set: str
    timestamp_property: str = ""
    key_properties: tuple[str, ...] = ()
    filter: str = ""
    start_watermark: str = "1970-01-01T00:00:00Z"
    page_size: int = 0
    poll_interval_s: float = 60.0
    rate_limit_min_interval_s: float = 0.0
    prefer_delta: bool = False
    auth: OdataAuth = field(default_factory=OdataAuth)
    draft_flow: bool = False
    draft_activate_action: str = ""
    max_retry_after_s: float = 300.0
    name: str = "odata"


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OdataCursor:
    """Deterministic, chain-anchorable poll cursor.

    Attributes:
        entity_set: Entity set the cursor belongs to.
        mode: ``"watermark"`` or ``"delta"``.
        watermark: Last-seen maximum change timestamp; always maintained, even
            in delta mode, so a delta downgrade never loses the resume point.
        delta_link: The ``@odata.deltaLink`` to follow next (delta mode only).
        last_page_hash: ``sha256:`` digest of the last result page, so an
            auditor can prove which window of changes produced which tasks.
        probed: Whether a delta probe has already been attempted (so a
            watermark-only service is not re-probed every poll).
    """

    entity_set: str
    mode: Literal["watermark", "delta"]
    watermark: str
    delta_link: str = ""
    last_page_hash: str = ""
    probed: bool = False

    @classmethod
    def initial(cls, connection: OdataConnection) -> OdataCursor:
        """Return a fresh watermark cursor for ``connection``."""
        return cls(
            entity_set=connection.entity_set,
            mode="watermark",
            watermark=connection.start_watermark,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping (stable field order)."""
        return {
            "entity_set": self.entity_set,
            "mode": self.mode,
            "watermark": self.watermark,
            "delta_link": self.delta_link,
            "last_page_hash": self.last_page_hash,
            "probed": self.probed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OdataCursor:
        """Rebuild a cursor from :meth:`to_dict` output."""
        return cls(
            entity_set=str(data["entity_set"]),
            mode="delta" if data.get("mode") == "delta" else "watermark",
            watermark=str(data["watermark"]),
            delta_link=str(data.get("delta_link", "")),
            last_page_hash=str(data.get("last_page_hash", "")),
            probed=bool(data.get("probed", False)),
        )

    def content_hash(self) -> str:
        """Return the content-addressed identifier for this cursor."""
        return "sha256:" + hashlib.sha256(_canonical_bytes(self.to_dict())).hexdigest()


def save_cursor(path: Path, cursor: OdataCursor) -> None:
    """Persist ``cursor`` to ``path`` as canonical bytes (byte-stable)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_bytes(cursor.to_dict()) + b"\n")


def load_cursor(path: Path) -> OdataCursor | None:
    """Load a cursor from ``path``; return ``None`` when the file is absent."""
    if not path.exists():
        return None
    return OdataCursor.from_dict(json.loads(path.read_bytes().decode("utf-8")))


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class OdataHttpClient:
    """Thin OData HTTP client: auth, paging, 429/Retry-After, throttle.

    Args:
        connection: The connection this client serves.
        http_client: Optional pre-built ``httpx.Client`` (tests inject one bound
            to the in-process fake). When omitted, one is built against
            ``connection.service_root``.
        clock: Injected clock; defaults to :class:`RealClock`.
        max_429_retries: Retry budget for 429 responses.
        token_fetcher: Optional override for OAuth2 token acquisition (tests).
    """

    def __init__(
        self,
        connection: OdataConnection,
        *,
        http_client: Any | None = None,
        clock: Clock | None = None,
        max_429_retries: int = 5,
        token_fetcher: Callable[[OdataAuth], str] | None = None,
    ) -> None:
        if http_client is None and httpx is None:  # pragma: no cover - dep present
            msg = "httpx is required for OdataHttpClient"
            raise RuntimeError(msg)
        self._conn = connection
        self._client: Any = http_client or httpx.Client(base_url=connection.service_root, timeout=30.0)
        self._owns_client = http_client is None
        self._clock: Clock = clock or RealClock()
        self._max_429_retries = max_429_retries
        self._token_fetcher = token_fetcher
        self._last_call_at: float | None = None
        self._oauth_token: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Release the HTTP client if this instance owns it."""
        if self._owns_client:
            try:
                self._client.close()
            except Exception:  # pragma: no cover - best effort
                logger.debug("ignoring error while closing httpx client", exc_info=True)

    # -- auth --------------------------------------------------------------

    def auth_headers(self) -> dict[str, str]:
        """Return the auth headers for this connection (never logged raw)."""
        auth = self._conn.auth
        if auth.kind == "none":
            return {}
        if auth.kind == "bearer":
            return {auth.header_name: f"{auth.header_prefix}{_require_env(auth.token_env)}"}
        if auth.kind == "api_key":
            return {auth.header_name: _require_env(auth.token_env)}
        if auth.kind == "oauth2_client_credentials":
            token = self._resolve_oauth_token(auth)
            return {auth.header_name: f"{auth.header_prefix}{token}"}
        raise OdataAuthError(f"unsupported auth kind: {auth.kind!r}")  # pragma: no cover - guarded by Literal

    def _resolve_oauth_token(self, auth: OdataAuth) -> str:
        if self._oauth_token is not None:
            return self._oauth_token
        if self._token_fetcher is not None:
            self._oauth_token = self._token_fetcher(auth)
            return self._oauth_token
        client_id = _require_env(auth.client_id_env)
        client_secret = _require_env(auth.client_secret_env)
        form: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if auth.scope:
            form["scope"] = auth.scope
        resp = self._client.post(auth.token_url, data=form, headers={"Accept": "application/json"})
        if resp.status_code >= 400:
            raise OdataAuthError(f"OAuth2 token endpoint returned {resp.status_code}")
        token = resp.json().get("access_token")
        if not token:
            raise OdataAuthError("OAuth2 token response missing access_token")
        self._oauth_token = str(token)
        return self._oauth_token

    @staticmethod
    def sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
        """Return a copy of ``headers`` with sensitive values redacted."""
        redacted: dict[str, str] = {}
        for name, value in headers.items():
            if name.lower().startswith(_SENSITIVE_HEADER_PREFIXES):
                redacted[name] = "***"
            else:
                redacted[name] = value
        return redacted

    # -- request pipeline --------------------------------------------------

    def _throttle(self) -> None:
        interval = self._conn.rate_limit_min_interval_s
        if interval <= 0:
            self._last_call_at = self._clock.now()
            return
        now = self._clock.now()
        if self._last_call_at is not None:
            wait = interval - (now - self._last_call_at)
            if wait > 0:
                self._clock.sleep(wait)
        self._last_call_at = self._clock.now()

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, str] | None = None,
        if_match: str | None = None,
        prefer: str | None = None,
    ) -> Any:
        headers: dict[str, str] = {"Accept": "application/json"}
        headers.update(self.auth_headers())
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if if_match is not None:
            headers["If-Match"] = if_match
        if prefer is not None:
            headers["Prefer"] = prefer

        attempt = 0
        while True:
            self._throttle()
            try:
                resp = self._client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    data=form_body,
                    headers=headers,
                )
            except Exception as exc:  # pragma: no cover - transport failure
                logger.warning("OData transport error: %s %s", method, url)
                raise OdataError(f"OData transport error: {exc}") from exc

            status = getattr(resp, "status_code", 0)
            logger.debug("OData %s %s -> %s headers=%s", method, url, status, self.sanitize_headers(headers))

            if status == 429:
                retry_after = _parse_retry_after(resp)
                if attempt < self._max_429_retries:
                    wait = retry_after if retry_after is not None else self._conn.rate_limit_min_interval_s
                    wait = min(wait, self._conn.max_retry_after_s)
                    if wait > 0:
                        self._clock.sleep(wait)
                    attempt += 1
                    continue
                raise RateLimited(f"OData rate-limited after {attempt} retries", retry_after)
            return resp

    # -- typed operations --------------------------------------------------

    def get_metadata(self) -> str:
        """Return the raw ``$metadata`` CSDL document."""
        resp = self._request("GET", f"{self._conn.service_root.rstrip('/')}/$metadata")
        if getattr(resp, "status_code", 0) >= 400:
            raise OdataError(f"metadata request failed: {resp.status_code}")
        return str(resp.text)

    def get_json(self, url: str, *, params: dict[str, Any] | None = None, prefer: str | None = None) -> Any:
        """GET ``url`` and return the parsed JSON body (raises on >= 400)."""
        resp = self._request("GET", url, params=params, prefer=prefer)
        status = getattr(resp, "status_code", 0)
        if status >= 400:
            raise OdataError(f"OData GET {url} failed: {status}")
        return resp.json()

    def get_entity(self, predicate: str) -> tuple[dict[str, Any], str | None]:
        """GET a single entity by ``<EntitySet>(<key>)`` predicate + ETag."""
        url = f"{self._conn.service_root.rstrip('/')}/{predicate}"
        resp = self._request("GET", url)
        status = getattr(resp, "status_code", 0)
        if status == 404:
            raise OdataError(f"entity not found: {predicate}")
        if status >= 400:
            raise OdataError(f"OData GET {predicate} failed: {status}")
        body = resp.json()
        return body, _etag_of(resp, body)

    def patch_entity(
        self,
        predicate: str,
        patch: dict[str, Any],
        *,
        if_match: str | None,
    ) -> tuple[int, dict[str, Any] | None, str | None]:
        """PATCH an entity; surface 412 / 428 as :class:`OdataConflict`."""
        url = f"{self._conn.service_root.rstrip('/')}/{predicate}"
        resp = self._request("PATCH", url, json_body=patch, if_match=if_match)
        status = getattr(resp, "status_code", 0)
        if status == 412:
            raise OdataConflict("If-Match precondition failed (ETag stale)", status=412)
        if status == 428:
            raise OdataConflict("If-Match precondition required", status=428)
        if status >= 400:
            raise OdataError(f"OData PATCH {predicate} failed: {status}")
        body = resp.json() if getattr(resp, "content", b"") else None
        return status, body, _etag_of(resp, body)

    def post(
        self,
        predicate: str,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any] | None, str | None]:
        """POST to ``<EntitySet>`` (create) or a bound action path."""
        url = f"{self._conn.service_root.rstrip('/')}/{predicate}"
        resp = self._request("POST", url, json_body=json_body if json_body is not None else {})
        status = getattr(resp, "status_code", 0)
        if status >= 400:
            raise OdataError(f"OData POST {predicate} failed: {status}")
        body = resp.json() if getattr(resp, "content", b"") else None
        return status, body, _etag_of(resp, body)


# ---------------------------------------------------------------------------
# Key discovery + predicate building
# ---------------------------------------------------------------------------


def discover_keys(connection: OdataConnection, client: OdataHttpClient) -> tuple[str, ...]:
    """Return the key property names for ``connection.entity_set``.

    Uses ``connection.key_properties`` when configured; otherwise parses
    ``$metadata`` for the entity type's ``<Key>`` property refs.
    """
    if connection.key_properties:
        return tuple(connection.key_properties)
    metadata = client.get_metadata()
    for match in _METADATA_KEY_RE.finditer(metadata):
        if match.group("name") == connection.entity_set:
            refs = tuple(m.group("name") for m in _PROPERTY_REF_RE.finditer(match.group("keys")))
            if refs:
                return refs
    raise OdataError(f"could not derive key properties for entity set {connection.entity_set!r}")


def build_key_predicate(entity_set: str, key: dict[str, Any]) -> str:
    """Build an OData key predicate, e.g. ``Widgets(1)`` or ``Widgets(a=1,b='x')``.

    A single key renders as a bare literal; a composite key renders as
    ``name=value`` pairs in sorted order for determinism.
    """
    if not key:
        raise OdataError("key predicate requires at least one key property")
    if len(key) == 1:
        (only_value,) = key.values()
        return f"{entity_set}({_key_literal(only_value)})"
    parts = ",".join(f"{name}={_key_literal(key[name])}" for name in sorted(key))
    return f"{entity_set}({parts})"


def key_signature(key: dict[str, Any]) -> str:
    """Return the inner key text (without the entity-set wrapper), sorted."""
    if len(key) == 1:
        (name,) = key
        return f"{name}={_key_literal(key[name])}"
    return ",".join(f"{name}={_key_literal(key[name])}" for name in sorted(key))


# ---------------------------------------------------------------------------
# Poll source
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PollResult:
    """Outcome of one poll: emitted events plus the advanced cursor."""

    events: tuple[TriggerEvent, ...]
    cursor: OdataCursor


class OdataPollSource:
    """Poll an OData entity set and normalise changes into TriggerEvents."""

    def __init__(self, connection: OdataConnection, *, http_client: OdataHttpClient | None = None) -> None:
        self._conn = connection
        self._client = http_client or OdataHttpClient(connection)
        self._keys: tuple[str, ...] | None = tuple(connection.key_properties) or None

    # -- TriggerSource protocol -------------------------------------------

    def normalize(self, raw_event: dict[str, Any]) -> TriggerEvent:
        """Convert one OData entity into a normalised TriggerEvent."""
        return self._to_event(raw_event, change_kind="upsert")

    # -- polling ----------------------------------------------------------

    def poll(self, cursor: OdataCursor | None) -> PollResult:
        """Run one poll cycle and return emitted events + the next cursor."""
        cursor = cursor or OdataCursor.initial(self._conn)

        if cursor.mode == "delta" and cursor.delta_link:
            try:
                return self._poll_delta(cursor)
            except OdataError as exc:
                # Fall back to watermark polling WITHOUT losing the cursor: the
                # maintained watermark is the resume position. Do not re-probe
                # this cycle (probed stays True so we do not thrash).
                logger.info("OData delta read failed (%s); falling back to watermark polling", exc)
                cursor = replace(cursor, mode="watermark", delta_link="")
                return self._poll_watermark(cursor, allow_probe=False)

        allow_probe = self._conn.prefer_delta and not cursor.probed
        return self._poll_watermark(cursor, allow_probe=allow_probe)

    def _poll_watermark(self, cursor: OdataCursor, *, allow_probe: bool) -> PollResult:
        prefer = "odata.track-changes" if allow_probe else None
        entities, delta_link = self._drain(self._first_watermark_url(cursor), prefer=prefer)

        events = tuple(self._to_event(e, change_kind="upsert") for e in entities if "@removed" not in e)
        new_watermark = self._advance_watermark(cursor.watermark, entities)
        page_hash = _hash_entities(entities)

        if allow_probe and delta_link:
            next_cursor = OdataCursor(
                entity_set=self._conn.entity_set,
                mode="delta",
                watermark=new_watermark,
                delta_link=delta_link,
                last_page_hash=page_hash,
                probed=True,
            )
        else:
            next_cursor = OdataCursor(
                entity_set=self._conn.entity_set,
                mode="watermark",
                watermark=new_watermark,
                last_page_hash=page_hash,
                probed=cursor.probed or allow_probe,
            )
        return PollResult(events=events, cursor=next_cursor)

    def _poll_delta(self, cursor: OdataCursor) -> PollResult:
        entities, delta_link = self._drain(cursor.delta_link, prefer=None)
        if not delta_link:
            # A delta read that returns no fresh deltaLink is treated as a delta
            # failure so the caller falls back to watermark polling.
            raise OdataError("delta response did not include @odata.deltaLink")

        events: list[TriggerEvent] = []
        new_watermark = cursor.watermark
        for entity in entities:
            if "@removed" in entity:
                events.append(self._to_event(entity, change_kind="delete"))
                continue
            events.append(self._to_event(entity, change_kind="upsert"))
            new_watermark = _max_str(new_watermark, str(entity.get(self._conn.timestamp_property, "")))

        return PollResult(
            events=tuple(events),
            cursor=OdataCursor(
                entity_set=self._conn.entity_set,
                mode="delta",
                watermark=new_watermark,
                delta_link=delta_link,
                last_page_hash=_hash_entities(entities),
                probed=True,
            ),
        )

    # -- helpers ----------------------------------------------------------

    def _first_watermark_url(self, cursor: OdataCursor) -> str:
        root = self._conn.service_root.rstrip("/")
        ts = self._conn.timestamp_property
        clauses = [f"{ts} gt {cursor.watermark}"]
        if self._conn.filter:
            clauses.append(f"({self._conn.filter})")
        params: list[str] = [
            f"$filter={' and '.join(clauses)}",
            f"$orderby={ts} asc",
        ]
        if self._conn.page_size > 0:
            params.append(f"$top={self._conn.page_size}")
        return f"{root}/{self._conn.entity_set}?{'&'.join(params)}"

    def _drain(self, url: str, *, prefer: str | None) -> tuple[list[dict[str, Any]], str]:
        """Follow ``@odata.nextLink`` pages; return all rows + final deltaLink."""
        collected: list[dict[str, Any]] = []
        delta_link = ""
        next_url: str | None = url
        while next_url:
            body = self._client.get_json(next_url, prefer=prefer)
            collected.extend(body.get("value", []))
            delta_link = body.get("@odata.deltaLink", delta_link)
            next_url = body.get("@odata.nextLink")
            # Prefer is only meaningful on the initial request of a sequence.
            prefer = None
        return collected, delta_link

    def _advance_watermark(self, current: str, entities: list[dict[str, Any]]) -> str:
        ts = self._conn.timestamp_property
        watermark = current
        for entity in entities:
            if "@removed" in entity:
                continue
            watermark = _max_str(watermark, str(entity.get(ts, "")))
        return watermark

    def _key_dict(self, entity: dict[str, Any]) -> dict[str, Any]:
        return {name: entity.get(name) for name in self._key_names()}

    def _key_names(self) -> tuple[str, ...]:
        if self._keys is None:
            self._keys = discover_keys(self._conn, self._client)
        return self._keys

    def _to_event(self, entity: dict[str, Any], *, change_kind: str) -> TriggerEvent:
        key = self._key_dict(entity)
        predicate_inner = key_signature(key)
        ts_value = str(entity.get(self._conn.timestamp_property, ""))
        return TriggerEvent(
            source=f"odata:{self._conn.name}",
            timestamp=_parse_ts(ts_value),
            raw_payload=dict(entity),
            message=f"{self._conn.entity_set} {predicate_inner} {change_kind}"[:500],
            metadata={
                "source_type": "odata_poll",
                "connection": self._conn.name,
                "entity_set": self._conn.entity_set,
                "entity_key": predicate_inner,
                "change_kind": change_kind,
                "watermark": ts_value,
            },
        )


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name, "") if name else ""
    if not value:
        raise OdataAuthError(f"required credential env var {name!r} is not set")
    return value


def _key_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return f"'{value}'"


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _hash_entities(entities: list[dict[str, Any]]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes({"value": entities})).hexdigest()


def _max_str(current: str, candidate: str) -> str:
    if not candidate:
        return current
    return candidate if candidate > current else current


def _parse_ts(value: str) -> float:
    if not value:
        return 0.0
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _parse_retry_after(response: Any) -> float | None:
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _etag_of(response: Any, body: dict[str, Any] | None) -> str | None:
    if isinstance(body, dict) and body.get("@odata.etag"):
        return str(body["@odata.etag"])
    headers = getattr(response, "headers", {}) or {}
    etag = headers.get("ETag") if hasattr(headers, "get") else None
    return str(etag) if etag else None
