"""In-process fake OData v4 service for hermetic integration tests.

The fake is a Starlette ASGI app driven through ``httpx.ASGITransport`` so no
socket, port, or real clock is involved. It models the slice of the OData v4
protocol the ``odata_poll`` trigger source and the ``odata_writeback`` helper
exercise:

* ``GET /$metadata`` -- a minimal CSDL XML document whose ``EntityType`` names
  the key property, so ``discover_keys`` has something real to parse.
* ``GET /<EntitySet>`` -- a collection page with ``$filter=<ts> gt <watermark>``
  support, ``$orderby``, and server-driven paging via ``@odata.nextLink``
  (opaque ``$skiptoken``).
* ``Prefer: odata.track-changes`` -- when delta tracking is enabled the final
  page carries ``@odata.deltaLink`` and the response sets
  ``Preference-Applied``; otherwise the header is ignored (the real-world
  fallback the source must tolerate).
* ``GET /<EntitySet>?$deltatoken=...`` -- changed entities since the token plus
  a fresh ``@odata.deltaLink``; deletions surface as ``@removed`` tombstones.
* ``GET /<EntitySet>(<key>)`` -- a single entity with ``@odata.etag`` and an
  ``ETag`` header.
* ``PATCH /<EntitySet>(<key>)`` -- ``If-Match`` enforced: missing precondition
  is ``428``, a stale precondition is ``412``, a match applies the patch and
  bumps the ETag.
* Draft-activate flow -- ``POST`` a draft, ``PATCH`` the draft, ``POST`` the
  bound activate action to promote it to an active entity.
* A configurable ``429 + Retry-After`` throttle plan.

Every request is appended to :attr:`FakeODataService.calls` so tests can assert
on inter-call spacing, ``Prefer`` negotiation, and ``If-Match`` values without
reaching into the transport.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

BASE_URL = "http://odata.test"

_KEY_PREDICATE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\((.*)\)$")


@dataclass
class FakeClock:
    """Deterministic clock: ``sleep`` advances virtual time and is recorded.

    Satisfies the ``Clock`` protocol the OData HTTP client depends on, so the
    rate-limit and ``Retry-After`` paths are exercised without any real sleep.
    """

    _t: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.sleeps.append(seconds)
            self._t += seconds


@dataclass
class CallRecord:
    """One recorded HTTP call against the fake."""

    method: str
    path: str
    query: str
    prefer: str
    if_match: str | None


@dataclass
class _Entity:
    """Server-side entity row with change bookkeeping."""

    key: int
    fields: dict[str, Any]
    version: int
    seq: int


@dataclass
class FakeODataService:
    """Configurable in-process OData v4 service.

    Args:
        entity_set: Entity-set name served at ``/<entity_set>``.
        key_property: Single integer key property name.
        timestamp_property: Change-timestamp property used by watermark polling.
        page_size: Server-driven page size for collection reads.
        delta_enabled: Whether ``Prefer: odata.track-changes`` yields a delta
            link. When ``False`` the header is accepted but ignored.
        activate_action: Bound-action name used by the draft-activate flow.
    """

    entity_set: str = "Widgets"
    key_property: str = "id"
    timestamp_property: str = "modified"
    page_size: int = 2
    delta_enabled: bool = True
    activate_action: str = "Activate"

    _entities: dict[int, _Entity] = field(default_factory=dict)
    _deleted: dict[int, int] = field(default_factory=dict)
    _drafts: dict[int, _Entity] = field(default_factory=dict)
    _change_seq: int = 0
    _draft_seq: int = 0
    calls: list[CallRecord] = field(default_factory=list)
    # Throttle plan: number of leading requests to reject with 429 + Retry-After.
    throttle_first_n: int = 0
    throttle_retry_after: int = 10
    _throttled: int = 0
    # When True, delta-token reads fail (models a view-backed set that silently
    # drops delta support), forcing the client to fall back to watermark polling.
    delta_broken: bool = False
    # When True, a single-entity GET bumps the entity version right after it is
    # served, so a following PATCH with the just-read ETag hits a 412 (models a
    # concurrent human edit landing between GET and PATCH).
    bump_on_entity_get: bool = False
    # When False, single-entity reads omit the ETag (models a service that does
    # not surface concurrency tokens), so a write-back has no fresh ETag -> 428.
    expose_etag: bool = True

    # -- seeding / mutation (test-facing) ----------------------------------

    def seed(self, key: int, *, timestamp: str, **fields: Any) -> None:
        """Insert or replace an entity, advancing its change sequence."""
        self._change_seq += 1
        row = self._entities.get(key)
        version = (row.version + 1) if row is not None else 1
        payload = {self.key_property: key, self.timestamp_property: timestamp, **fields}
        self._entities[key] = _Entity(key=key, fields=payload, version=version, seq=self._change_seq)
        self._deleted.pop(key, None)

    def delete(self, key: int) -> None:
        """Delete an entity, recording a delta tombstone."""
        self._change_seq += 1
        self._entities.pop(key, None)
        self._deleted[key] = self._change_seq

    # -- transport ---------------------------------------------------------

    def client(self) -> httpx.Client:
        """Return a sync ``httpx.Client`` bound to this ASGI app (no sockets).

        The transport bridges each sync request into the real Starlette ASGI
        app via a short-lived event loop, so the fake stays a genuine ASGI
        application while remaining drivable by the synchronous production
        HTTP client (whose ``httpx`` build ships an async-only
        ``ASGITransport``).
        """
        app = self.app()
        return httpx.Client(transport=httpx.MockTransport(lambda req: _asgi_roundtrip(app, req)), base_url=BASE_URL)

    def app(self) -> Starlette:
        """Build the Starlette app dispatching every OData route."""
        return Starlette(routes=[Route("/{full_path:path}", self._dispatch, methods=["GET", "PATCH", "POST"])])

    # -- dispatch ----------------------------------------------------------

    async def _dispatch(self, request: Request) -> Response:
        full_path = request.path_params["full_path"]
        prefer = request.headers.get("prefer", "")
        if_match = request.headers.get("if-match")
        self.calls.append(
            CallRecord(
                method=request.method,
                path=full_path,
                query=request.url.query,
                prefer=prefer,
                if_match=if_match,
            )
        )

        throttled = self._maybe_throttle()
        if throttled is not None:
            return throttled

        if full_path == "$metadata":
            return self._metadata_response()

        predicate = _KEY_PREDICATE_RE.match(full_path)
        if predicate is not None:
            name, inner = predicate.group(1), predicate.group(2)
            if name != self.entity_set:
                return _error(404, "unknown entity set")
            return await self._entity_route(request, inner, if_match)

        # Bound-action call: ``<EntitySet>(<key>)/<Action>``.
        action = re.match(rf"^{re.escape(self.entity_set)}\((.*)\)/(.+)$", full_path)
        if action is not None:
            return self._activate_draft(action.group(1), action.group(2))

        if full_path == self.entity_set:
            if request.method == "POST":
                body = await _json_body(request)
                return self._create_draft(body)
            return self._collection_response(request, prefer)

        return _error(404, f"no route for {full_path!r}")

    def _maybe_throttle(self) -> Response | None:
        if self._throttled < self.throttle_first_n:
            self._throttled += 1
            return Response(
                content=b'{"error":{"code":"429","message":"Too Many Requests"}}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(self.throttle_retry_after)},
            )
        return None

    # -- collection --------------------------------------------------------

    def _collection_response(self, request: Request, prefer: str) -> Response:
        params = dict(request.query_params)
        delta_token = params.get("$deltatoken")
        if delta_token is not None:
            if self.delta_broken:
                return _error(400, "delta tracking not supported for this entity set")
            return self._delta_page(int(delta_token))

        track_changes = "odata.track-changes" in prefer and self.delta_enabled
        rows = self._watermark_rows(params.get("$filter", ""))
        skiptoken = params.get("$skiptoken")
        start = int(skiptoken) if skiptoken is not None else 0
        page = rows[start : start + self.page_size]
        next_start = start + self.page_size

        body: dict[str, Any] = {
            "@odata.context": f"{BASE_URL}/$metadata#{self.entity_set}",
            "value": [self._as_wire(r) for r in page],
        }
        headers: dict[str, str] = {}
        if next_start < len(rows):
            body["@odata.nextLink"] = f"{BASE_URL}/{self.entity_set}?$skiptoken={next_start}"
        elif track_changes:
            body["@odata.deltaLink"] = f"{BASE_URL}/{self.entity_set}?$deltatoken={self._change_seq}"
            headers["Preference-Applied"] = "odata.track-changes"
        return JSONResponse(body, headers=headers)

    def _watermark_rows(self, filter_expr: str) -> list[_Entity]:
        rows = list(self._entities.values())
        match = re.search(r"(\w+)\s+gt\s+(\S+)", filter_expr)
        if match is not None:
            prop, raw = match.group(1), match.group(2)
            watermark = raw.strip("'")
            rows = [r for r in rows if str(r.fields.get(prop, "")) > watermark]
        rows.sort(key=lambda r: (str(r.fields.get(self.timestamp_property, "")), r.key))
        return rows

    def _delta_page(self, token: int) -> Response:
        changed = [r for r in self._entities.values() if r.seq > token]
        changed.sort(key=lambda r: r.seq)
        removed = sorted((k for k, s in self._deleted.items() if s > token))
        value: list[dict[str, Any]] = [self._as_wire(r) for r in changed]
        for key in removed:
            value.append({"@removed": {"reason": "deleted"}, self.key_property: key})
        body = {
            "@odata.context": f"{BASE_URL}/$metadata#{self.entity_set}/$delta",
            "value": value,
            "@odata.deltaLink": f"{BASE_URL}/{self.entity_set}?$deltatoken={self._change_seq}",
        }
        return JSONResponse(body)

    # -- single entity -----------------------------------------------------

    async def _entity_route(self, request: Request, inner: str, if_match: str | None) -> Response:
        key = self._parse_key(inner)
        if key is None:
            return _error(400, "unparseable key")
        if request.method == "GET":
            row = self._entities.get(key) or self._drafts.get(key)
            if row is None:
                return _error(404, "not found")
            response = self._entity_response(row, with_etag=self.expose_etag)
            if self.bump_on_entity_get and key in self._entities:
                # A concurrent edit lands right after the read: bump the version.
                self._change_seq += 1
                row.version += 1
                row.seq = self._change_seq
            return response
        if request.method == "PATCH":
            return await self._patch_entity(request, key, if_match)
        return _error(405, "method not allowed")

    async def _patch_entity(self, request: Request, key: int, if_match: str | None) -> Response:
        row = self._entities.get(key) or self._drafts.get(key)
        if row is None:
            return _error(404, "not found")
        if if_match is None:
            return _error(428, "precondition required")
        if if_match != _etag(row):
            return _error(412, "precondition failed")
        patch = await _json_body(request)
        self._change_seq += 1
        row.fields.update({k: v for k, v in patch.items() if not k.startswith("@")})
        row.version += 1
        row.seq = self._change_seq
        return self._entity_response(row)

    # -- draft flow --------------------------------------------------------

    def _create_draft(self, body: dict[str, Any]) -> Response:
        self._draft_seq += 1
        draft_key = 100_000 + self._draft_seq
        payload = {k: v for k, v in body.items() if not k.startswith("@")}
        payload[self.key_property] = draft_key
        row = _Entity(key=draft_key, fields=payload, version=1, seq=0)
        self._drafts[draft_key] = row
        return self._entity_response(row, status=201)

    def _activate_draft(self, inner: str, action: str) -> Response:
        if action != self.activate_action:
            return _error(404, f"unknown action {action!r}")
        key = self._parse_key(inner)
        draft = self._drafts.pop(key, None) if key is not None else None
        if draft is None:
            return _error(404, "no such draft")
        self._change_seq += 1
        draft.seq = self._change_seq
        self._entities[draft.key] = draft
        return self._entity_response(draft)

    # -- rendering ---------------------------------------------------------

    def _entity_response(self, row: _Entity, *, status: int = 200, with_etag: bool = True) -> Response:
        headers = {"ETag": _etag(row)} if with_etag else {}
        return JSONResponse(self._as_wire(row, with_etag=with_etag), status_code=status, headers=headers)

    def _as_wire(self, row: _Entity, *, with_etag: bool = True) -> dict[str, Any]:
        if with_etag:
            return {"@odata.etag": _etag(row), **row.fields}
        return dict(row.fields)

    def _metadata_response(self) -> Response:
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">'
            "<edmx:DataServices>"
            '<Schema Namespace="Fake" xmlns="http://docs.oasis-open.org/odata/ns/edm">'
            f'<EntityType Name="{self.entity_set}">'
            f'<Key><PropertyRef Name="{self.key_property}"/></Key>'
            f'<Property Name="{self.key_property}" Type="Edm.Int32" Nullable="false"/>'
            f'<Property Name="{self.timestamp_property}" Type="Edm.DateTimeOffset"/>'
            '<Property Name="name" Type="Edm.String"/>'
            "</EntityType>"
            f'<EntityContainer Name="Container"><EntitySet Name="{self.entity_set}" '
            f'EntityType="Fake.{self.entity_set}"/></EntityContainer>'
            "</Schema></edmx:DataServices></edmx:Edmx>"
        )
        return PlainTextResponse(xml, media_type="application/xml")

    # -- helpers -----------------------------------------------------------

    def _parse_key(self, inner: str) -> int | None:
        token = inner.strip()
        if "=" in token:  # keyed form ``id=1``
            token = token.split("=", 1)[1].strip()
        token = token.strip("'")
        try:
            return int(token)
        except ValueError:
            return None


def _etag(row: _Entity) -> str:
    return f'W/"{row.version}"'


def _error(status: int, message: str) -> Response:
    return JSONResponse({"error": {"code": str(status), "message": message}}, status_code=status)


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    import json

    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def split_delta_token(delta_link: str) -> str:
    """Return the ``$deltatoken`` value embedded in a delta link (test helper)."""
    query = parse_qs(urlparse(delta_link).query)
    return query.get("$deltatoken", [""])[0]


def _asgi_roundtrip(app: Starlette, request: httpx.Request) -> httpx.Response:
    """Drive one sync request through the ASGI *app* and return the response."""
    return asyncio.run(_call_asgi(app, request))


async def _call_asgi(app: Starlette, request: httpx.Request) -> httpx.Response:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": request.method,
        "scheme": request.url.scheme,
        "path": request.url.path,
        "raw_path": request.url.path.encode("utf-8"),
        "query_string": request.url.query.encode("utf-8") if isinstance(request.url.query, str) else request.url.query,
        "root_path": "",
        "headers": [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in request.headers.items()],
        "server": (request.url.host, request.url.port or 80),
        "client": ("testclient", 50000),
    }
    body = request.content
    pending: list[dict[str, Any]] = [{"type": "http.request", "body": body, "more_body": False}]
    captured: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        captured.append(message)

    await app(scope, receive, send)

    status = 500
    raw_headers: list[tuple[bytes, bytes]] = []
    out = b""
    for message in captured:
        if message["type"] == "http.response.start":
            status = message["status"]
            raw_headers = message.get("headers", [])
        elif message["type"] == "http.response.body":
            out += message.get("body", b"")
    headers = [(key.decode("latin-1"), value.decode("latin-1")) for key, value in raw_headers]
    return httpx.Response(status_code=status, headers=headers, content=out, request=request)
