"""Stateless MCP protocol core with audit-chain-anchored cross-call continuity.

Issue #2307. A recent MCP spec revision removes the ``initialize`` handshake
and ``Mcp-Session-Id``, moves client capabilities into a per-request ``_meta``
field, and deprecates Roots, Sampling, and Logging. Any request can now land
on any server instance, so the protocol no longer provides cross-call
ordering. For a determinism-and-audit orchestrator that is where the
Merkle-chained event journal (see :mod:`bernstein.core.replay.journal`)
becomes the only authoritative source of call ordering: continuity is
chain-anchored, not session-anchored.

This module supplies the stateless core primitives so a client and server can
run with no server-side session state:

* :func:`build_request_meta` carries client capabilities in the per-request
  ``_meta`` and emits W3C Trace Context (``traceparent`` / ``tracestate`` /
  ``baggage``). The span id is derived from the call's content hash and the
  trace id from the run's root hash, so two replays of the same run emit
  byte-identical ``_meta`` (AC1, AC2). No session id is ever set.

* :class:`InputRequiredResult` implements the retry mechanism: the pending
  call state is base64-encoded into ``requestState`` and echoed back to the
  client, so a *different* server instance with no shared memory can process
  the retry from the echoed state alone (AC3).

* :func:`resolve_sampling_in_orchestrator` migrates off Sampling by resolving
  the decision in-orchestrator with a deterministic resolver, never an LLM
  callback into the client.

* :class:`CacheDirective` / :class:`CacheReference` honour ``ttlMs`` /
  ``cacheScope`` and record a cache hit as a reference to the content hash of
  the run that produced the cached value (AC5).

* :class:`StatelessCallRecord` projects a call and its ``_meta`` into a
  journal payload; :func:`record_mcp_call_in_journal` appends it as an ordered
  entry with its content-derived span id (AC4). The audit-chain anchor lives
  in :func:`bernstein.core.security.audit_chain.record_mcp_stateless_call`.

* Deprecated Roots/Sampling/Logging stay behind a 12-month compatibility shim
  (:data:`DEPRECATED_CAPABILITIES`, :func:`compat_shim_active`).

Determinism
-----------
Every function here is pure of clocks, sockets, and session stores. The
content-derived ids and the base64 request state are canonical
(sorted-key, compact-separator JSON), so byte-identical replays produce
byte-identical wire values.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = [
    "DEPRECATED_CAPABILITIES",
    "DEPRECATION_DATE",
    "LEGACY_SESSION_HEADER",
    "REMOVAL_DATE",
    "SHIM_WINDOW_MONTHS",
    "CacheDirective",
    "CacheReference",
    "InputRequiredResult",
    "StatelessCallRecord",
    "anchor_stateless_call",
    "build_request_meta",
    "compat_shim_active",
    "decode_request_state",
    "derive_span_id",
    "derive_trace_id",
    "encode_request_state",
    "format_traceparent",
    "legacy_session_header_value",
    "months_since_deprecation",
    "record_mcp_call_in_journal",
    "request_span_id",
    "resolve_sampling_in_orchestrator",
]

#: Journal event type appended for every stateless MCP call. Ordering across
#: calls lives in the journal chain rather than a session store (AC4).
JOURNAL_EVENT_MCP_CALL = "mcp.stateless_call"

#: Protocol features deprecated by the stateless spec revision. Roots,
#: Sampling, and Logging are deprecated capabilities; ``sessions`` covers the
#: removed protocol-session lifecycle (the ``Mcp-Session-Id`` round-trip).
#: All of them are honoured only behind the compatibility shim.
DEPRECATED_CAPABILITIES: frozenset[str] = frozenset({"roots", "sampling", "logging", "sessions"})

#: Length of the backward-compatibility shim for deprecated capabilities.
#: The spec deprecates them; we honour them for one year and then refuse.
SHIM_WINDOW_MONTHS: int = 12

#: The stateless spec revision that removes the ``initialize`` handshake and
#: protocol sessions. Deprecated features are shimmed from this date.
DEPRECATION_DATE: date = date(2026, 7, 28)

#: Explicit removal date for shimmed features: ``SHIM_WINDOW_MONTHS`` after
#: the deprecating revision. From this date the legacy surfaces are refused.
REMOVAL_DATE: date = date(2027, 7, 28)

#: Wire name of the removed protocol-session header. This module is the only
#: sanctioned home for the token; transports import the constant so a
#: repo-wide guard can assert no other MCP wire path names it.
LEGACY_SESSION_HEADER: str = "mcp-session-id"

#: W3C Trace Context version prefix and sampled-flag suffix. We always emit
#: version ``00`` with the ``sampled`` flag set so a downstream collector
#: records the projected trace.
_TRACEPARENT_VERSION = "00"
_TRACEPARENT_FLAGS = "01"

#: ``tracestate`` vendor key identifying a Bernstein-projected trace.
_TRACESTATE_VENDOR = "bernstein"


# ---------------------------------------------------------------------------
# W3C Trace Context id derivation
# ---------------------------------------------------------------------------


def _derive_hex(*, domain: str, fields: dict[str, Any], nbytes: int) -> str:
    """Return the first ``nbytes`` of ``H(domain, fields)`` as hex.

    Domain-separated so a trace id and a span id derived from overlapping
    inputs never collide. The pre-image is a canonical field tuple, so the
    digest is stable across processes and platforms.
    """
    preimage = json.dumps(
        {"domain": domain, "fields": fields},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(preimage).hexdigest()[: nbytes * 2]


def derive_trace_id(*, run_root_hash: str) -> str:
    """Return the 16-byte (32-hex-char) W3C trace id for a run.

    Derived from the run's root hash so every call in the run shares one
    trace id and two replays derive the same id (AC2).
    """
    return _derive_hex(domain="mcp.trace", fields={"run_root_hash": run_root_hash}, nbytes=16)


def derive_span_id(*, params_content_hash: str, call_index: int) -> str:
    """Return the 8-byte (16-hex-char) W3C span id for one call.

    The span id is a function of the call's parameter content hash and its
    ordered index in the run, so it is content-derived (never random) and
    two replays derive the same id (AC2). A different call index yields a
    different span id, preserving ordering.
    """
    return _derive_hex(
        domain="mcp.span",
        fields={"params_content_hash": params_content_hash, "call_index": call_index},
        nbytes=8,
    )


def format_traceparent(*, trace_id: str, span_id: str) -> str:
    """Return the ``traceparent`` header value ``version-trace-span-flags``."""
    return f"{_TRACEPARENT_VERSION}-{trace_id}-{span_id}-{_TRACEPARENT_FLAGS}"


def build_request_meta(
    *,
    method: str,
    params_content_hash: str,
    run_root_hash: str,
    call_index: int,
    client_capabilities: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the per-request ``_meta`` for a stateless MCP call.

    Carries the client capabilities (which used to travel through the
    ``initialize`` handshake) and W3C Trace Context whose ids are derived
    from content hashes. No session id is set: any request may land on any
    server instance, and continuity is anchored in the journal, not a
    session store (AC1).

    Args:
        method: The MCP method (e.g. ``tools/call``). Recorded in baggage
            so an operator can correlate the span with the method offline.
        params_content_hash: SHA-256 of the canonical request params; the
            span-id seed.
        run_root_hash: The run's root hash (the journal genesis / first
            entry hash); the trace-id seed shared across the run.
        call_index: 0-based ordered index of this call within the run.
        client_capabilities: The client capability map to advertise per
            request in place of the removed handshake.

    Returns:
        A ``_meta`` dict with ``client.capabilities``, ``traceparent``,
        ``tracestate`` and ``baggage``. Deterministic in its inputs.
    """
    trace_id = derive_trace_id(run_root_hash=run_root_hash)
    span_id = derive_span_id(params_content_hash=params_content_hash, call_index=call_index)
    baggage = f"mcp.method={method},mcp.call_index={call_index}"
    return {
        "client": {"capabilities": dict(client_capabilities)},
        "traceparent": format_traceparent(trace_id=trace_id, span_id=span_id),
        "tracestate": f"{_TRACESTATE_VENDOR}=r:{run_root_hash[:16]}",
        "baggage": baggage,
    }


# ---------------------------------------------------------------------------
# Request state (InputRequiredResult retry)
# ---------------------------------------------------------------------------


def encode_request_state(state: Mapping[str, Any]) -> str:
    """Return the base64 of canonical JSON of ``state``.

    Deterministic: the JSON is sorted-key and compact, so the same logical
    state always encodes to the same string, and any server instance decodes
    it identically.
    """
    canonical = json.dumps(dict(state), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return base64.b64encode(canonical).decode("ascii")


def decode_request_state(encoded: str) -> dict[str, Any]:
    """Return the state dict from a base64 ``requestState`` string.

    Raises:
        ValueError: When ``encoded`` is not valid base64 of a JSON object.
            The message names ``requestState`` so a caller can surface a
            precise wire error rather than a bare decode failure.
    """
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (binascii.Error, ValueError) as exc:
        msg = "requestState is not valid base64"
        raise ValueError(msg) from exc
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        msg = "requestState is not valid JSON"
        raise ValueError(msg) from exc
    if not isinstance(parsed, dict):
        msg = "requestState must decode to a JSON object"
        raise ValueError(msg)
    return parsed


@dataclass(frozen=True, slots=True)
class InputRequiredResult:
    """A tool result that needs more input before it can complete.

    The pending call state is echoed to the client as a base64
    ``requestState``. When the client re-submits with the supplied input,
    *any* server instance - even one that never saw the original call -
    resumes purely from the echoed state (AC3): there is no server-side
    session to consult.

    Attributes:
        prompt: Human-facing prompt describing the input required.
        request_state: The opaque state needed to resume the call. Carried
            base64-encoded on the wire.
    """

    prompt: str
    request_state: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_wire(self) -> dict[str, Any]:
        """Return the ``input_required`` result body for the client."""
        return {
            "type": "input_required",
            "prompt": self.prompt,
            "requestState": encode_request_state(self.request_state),
        }

    @staticmethod
    def resume_from_wire(wire: Mapping[str, Any], *, supplied_input: Mapping[str, Any]) -> dict[str, Any]:
        """Rebuild the resumable call state from an echoed wire result.

        A different server instance calls this to process the retry. It
        decodes ``requestState`` and merges the client-supplied input,
        producing the full context needed to continue - without any shared
        session memory.

        Raises:
            ValueError: When ``requestState`` is missing or malformed.
        """
        encoded = wire.get("requestState")
        if not isinstance(encoded, str):
            msg = "requestState missing from input_required result"
            raise ValueError(msg)
        state = decode_request_state(encoded)
        state["input"] = dict(supplied_input)
        return state


# ---------------------------------------------------------------------------
# Sampling resolved in-orchestrator (migrate off client callback)
# ---------------------------------------------------------------------------


def _refuse_sampling(_request: Mapping[str, Any]) -> str:
    """Default resolver: refuse an LLM callback into the client."""
    msg = "sampling must be resolved in-orchestrator; no LLM callback into the client is permitted"
    raise RuntimeError(msg)


def resolve_sampling_in_orchestrator(
    request: Mapping[str, Any],
    *,
    resolver: Callable[[Mapping[str, Any]], str] | None = None,
) -> str:
    """Resolve a sampling request in the orchestrator, never via the client.

    The stateless spec deprecates Sampling. Rather than call back into the
    client for an LLM completion, the orchestrator resolves the decision
    locally with a deterministic ``resolver``. When no resolver is supplied
    the call is refused, so a stray sampling request cannot silently reach
    back into a client.

    Args:
        request: The sampling request (messages / params).
        resolver: An in-orchestrator function returning the resolved text.
            Defaults to a refusing resolver.

    Returns:
        The resolved completion text.
    """
    chosen = resolver or _refuse_sampling
    return chosen(request)


# ---------------------------------------------------------------------------
# Cache directives / references (ttlMs / cacheScope)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CacheDirective:
    """Parsed ``ttlMs`` / ``cacheScope`` cache directive from ``_meta``."""

    ttl_ms: int = 0
    cache_scope: str = ""

    @property
    def is_cacheable(self) -> bool:
        """Whether the directive requests caching (positive TTL)."""
        return self.ttl_ms > 0

    @staticmethod
    def from_meta(meta: Mapping[str, Any]) -> CacheDirective:
        """Parse a directive from a request ``_meta`` mapping.

        An absent or non-positive ``ttlMs`` yields a non-cacheable
        directive.
        """
        raw_ttl = meta.get("ttlMs", 0)
        try:
            ttl_ms = int(raw_ttl)
        except (TypeError, ValueError):
            ttl_ms = 0
        scope = str(meta.get("cacheScope", "")) if ttl_ms > 0 else ""
        return CacheDirective(ttl_ms=max(0, ttl_ms), cache_scope=scope)


@dataclass(frozen=True, slots=True)
class CacheReference:
    """A cache hit expressed as a reference to the producing run.

    A hit does not re-assert the cached bytes; it points at the content hash
    of the value and the head hash of the run that produced it, so a verifier
    holding the journal can bind the reused value back to a real prior run
    (AC5).

    Attributes:
        cache_hit: ``True`` for a hit; the reference is meaningless otherwise.
        content_hash: SHA-256 of the cached value.
        producing_run_head: Journal head of the run that produced the value.
        cache_scope: The scope the hit was served under.
    """

    cache_hit: bool
    content_hash: str
    producing_run_head: str
    cache_scope: str

    @staticmethod
    def for_hit(*, content_hash: str, producing_run_head: str, cache_scope: str) -> CacheReference:
        """Build a cache-hit reference to the producing run's content hash."""
        return CacheReference(
            cache_hit=True,
            content_hash=content_hash,
            producing_run_head=producing_run_head,
            cache_scope=cache_scope,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_hit": self.cache_hit,
            "content_hash": self.content_hash,
            "producing_run_head": self.producing_run_head,
            "cache_scope": self.cache_scope,
        }


# ---------------------------------------------------------------------------
# Compatibility shim for deprecated capabilities
# ---------------------------------------------------------------------------


def compat_shim_active(capability: str, *, months_since_deprecation: int) -> bool:
    """Whether the compatibility shim still honours a deprecated capability.

    Roots, Sampling, Logging, and protocol sessions are deprecated by the
    stateless spec but kept working for a 12-month window ending at
    :data:`REMOVAL_DATE`. A capability that was never deprecated is never
    shimmed.
    """
    if capability not in DEPRECATED_CAPABILITIES:
        return False
    return months_since_deprecation < SHIM_WINDOW_MONTHS


def months_since_deprecation(today: date | None = None) -> int:
    """Return whole months elapsed since :data:`DEPRECATION_DATE` (min 0).

    Feed the result into :func:`compat_shim_active` to evaluate the shim
    window against wall-clock time. Days before a full month has elapsed do
    not count, so the shim expires exactly at :data:`REMOVAL_DATE`.
    """
    current = today if today is not None else date.today()
    months = (current.year - DEPRECATION_DATE.year) * 12 + (current.month - DEPRECATION_DATE.month)
    if current.day < DEPRECATION_DATE.day:
        months -= 1
    return max(0, months)


def legacy_session_header_value(headers: Mapping[str, str]) -> str | None:
    """Return the legacy protocol-session header value, if a client sent one.

    Case-insensitive lookup. The value is only ever inspected to decide the
    shim response; it is never stored and never echoed back.
    """
    for name, value in headers.items():
        if name.lower() == LEGACY_SESSION_HEADER:
            return value
    return None


# ---------------------------------------------------------------------------
# Journal projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatelessCallRecord:
    """A stateless MCP call projected for the run journal.

    Carries the method, ordered call index, the content-derived trace/span
    ids read back from ``_meta``, and an optional cache reference. No session
    id is projected: ordering lives in the journal chain (AC4).
    """

    method: str
    call_index: int
    trace_id: str
    span_id: str
    cache: CacheReference | None = None

    @staticmethod
    def from_meta(
        *,
        method: str,
        call_index: int,
        meta: Mapping[str, Any],
        cache: CacheReference | None = None,
    ) -> StatelessCallRecord:
        """Project a record from a built ``_meta`` and optional cache ref."""
        traceparent = str(meta.get("traceparent", ""))
        parts = traceparent.split("-")
        trace_id = parts[1] if len(parts) == 4 else ""
        span_id = parts[2] if len(parts) == 4 else ""
        return StatelessCallRecord(
            method=method,
            call_index=call_index,
            trace_id=trace_id,
            span_id=span_id,
            cache=cache,
        )

    def to_journal_payload(self) -> dict[str, Any]:
        """Return the decision payload appended to the run journal."""
        payload: dict[str, Any] = {
            "method": self.method,
            "call_index": self.call_index,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
        }
        if self.cache is not None:
            payload["cache"] = self.cache.to_dict()
        return payload


def record_mcp_call_in_journal(journal: EventJournal, record: StatelessCallRecord) -> None:
    """Append a stateless MCP call as an ordered journal entry.

    The call and its content-derived span id become one Merkle-chained row,
    so cross-call ordering and continuity are chain-anchored rather than
    session-anchored (AC4).
    """
    journal.record(JOURNAL_EVENT_MCP_CALL, **record.to_journal_payload())


# ---------------------------------------------------------------------------
# Server-side call identity and anchoring (issue #2506)
# ---------------------------------------------------------------------------


def _params_without_meta(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``params`` with the ``_meta`` envelope removed."""
    return {key: value for key, value in params.items() if key != "_meta"}


def _meta_of(params: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the ``_meta`` mapping of a request's params (empty if absent)."""
    meta = params.get("_meta")
    return meta if isinstance(meta, Mapping) else {}


def _ids_from_traceparent(meta: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(trace_id, span_id)`` parsed from ``_meta.traceparent``.

    Returns empty strings when the header is absent or malformed, so the
    caller can fall back to server-side content derivation.
    """
    parts = str(meta.get("traceparent", "")).split("-")
    if len(parts) == 4 and parts[1] and parts[2]:
        return parts[1], parts[2]
    return "", ""


def _call_index_from_baggage(meta: Mapping[str, Any]) -> int | None:
    """Return the ordered call index carried in ``_meta.baggage``, if any."""
    for item in str(meta.get("baggage", "")).split(","):
        key, _, value = item.strip().partition("=")
        if key == "mcp.call_index":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def request_span_id(message: Mapping[str, Any]) -> str:
    """Return the content-derived span id correlating a request/response pair.

    Prefers the span id the client derived into ``_meta.traceparent``; a
    request without one gets a deterministic projection of its own content
    (method, params, JSON-RPC id). Either way the id is a pure function of
    the request bytes, so any transport instance derives the same value and
    per-request correlation needs no shared state (issue #2506, gateway
    phase).
    """
    raw_params = message.get("params")
    params: Mapping[str, Any] = raw_params if isinstance(raw_params, Mapping) else {}
    _, span_id = _ids_from_traceparent(_meta_of(params))
    if span_id:
        return span_id
    preimage = {
        "id": message.get("id"),
        "method": message.get("method"),
        "params": _params_without_meta(params),
    }
    return _derive_hex(domain="mcp.request", fields=preimage, nbytes=8)


def anchor_stateless_call(
    *,
    journal: EventJournal,
    method: str,
    params: Mapping[str, Any],
    chain: AuditChainStore | None = None,
    cache: CacheReference | None = None,
) -> StatelessCallRecord:
    """Anchor one served or proxied MCP call in the journal and audit chain.

    Replaces the deleted session stores as the continuity mechanism: the
    call becomes an ordered Merkle-chained journal row, and (when a chain is
    wired in) an ``mcp.stateless_call`` audit event binding the
    content-derived ids to the journal head. A verifier reconstructs the full
    call ordering of a run from chain entries alone (issue #2506).

    Identity resolution prefers the ids the client derived into ``_meta``;
    a request without ``_meta`` gets server-side derivation from the call
    content and the count of previously anchored calls, which is a projection
    of chain state rather than session state.

    Args:
        journal: The run journal receiving the ordered entry.
        method: The MCP method (e.g. ``tools/call``).
        params: The request params (``_meta`` is consumed, not recorded).
        chain: Optional audit chain store to anchor the call into.
        cache: Optional cache-hit reference recorded with the call.

    Returns:
        The projected :class:`StatelessCallRecord`.
    """
    from bernstein.core.replay.journal import load_events
    from bernstein.core.security.audit_chain import record_mcp_stateless_call

    meta = _meta_of(params)
    trace_id, span_id = _ids_from_traceparent(meta)
    call_index = _call_index_from_baggage(meta)
    if call_index is None:
        call_index = sum(1 for row in load_events(journal.path) if row.get("event") == JOURNAL_EVENT_MCP_CALL)
    if not span_id:
        content_hash = hashlib.sha256(
            json.dumps(
                {"method": method, "params": _params_without_meta(params)},
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        run_root_hash = hashlib.sha256(journal.run_id.encode("utf-8")).hexdigest()
        trace_id = derive_trace_id(run_root_hash=run_root_hash)
        span_id = derive_span_id(params_content_hash=content_hash, call_index=call_index)

    record = StatelessCallRecord(
        method=method,
        call_index=call_index,
        trace_id=trace_id,
        span_id=span_id,
        cache=cache,
    )
    record_mcp_call_in_journal(journal, record)
    if chain is not None:
        record_mcp_stateless_call(
            chain=chain,
            run_id=journal.run_id,
            method=method,
            call_index=call_index,
            trace_id=trace_id,
            span_id=span_id,
            journal_head=journal.head(),
            cache_content_hash=cache.content_hash if cache is not None else "",
        )
    return record
