"""OTel GenAI spans as a signed, deterministic projection of the event journal.

Issue #2300. Bernstein already ships an OTLP exporter for the OpenTelemetry
GenAI semantic conventions (see :mod:`bernstein.core.observability.otlp_exporter`),
but it is env-gated off by default and its spans are ordinary
random-id telemetry: any tool can emit a look-alike span and no verifier
can tell a Bernstein trace from a forgery.

This module makes the OTel export a **projection** of the canonical
per-run event journal (see :mod:`bernstein.core.replay.journal`) rather
than a separately-asserted stream of telemetry:

* Every span id is a deterministic function of the journal entry hash it
  projects (``span_id = H("otel.span", entry_hash)[:8 bytes]``), and the whole
  trace id is derived from the run's first journal entry hash. Two replays
  of the same run therefore export a byte-identical id tree (AC1).
* Every span carries ``bernstein.journal.entry_hash`` -- the exact journal
  row it projects -- so a verifier can bind each span back to the chain and
  reject one whose id was recomputed from a different row (AC2/AC3).
* The projection maps journal events onto the OTel GenAI span layers named
  in the issue (``invoke_workflow`` root, ``invoke_agent``, ``execute_tool``,
  ``chat``) using ``gen_ai.operation.name`` values so operator dashboards
  recognise Bernstein traffic (AC4).
* A detached Ed25519 signature over the canonical projected span set (the
  install identity) makes the set an attestable receipt: strip the journal
  and the span ids are unrecomputable; tamper with a span and the signature
  or the entry-hash binding fails (Primary artifact).

The canonical record stays the journal. OTel attribute names live behind a
stability flag (the GenAI conventions are still Development), so instability
in the convention never corrupts truth: the ids and the entry-hash binding
are journal-anchored, independent of any attribute rename.

Determinism
-----------
:func:`project_spans` is a pure function of its input events and never reads
a clock, environment, or socket. The canonical signing bytes are sorted-key,
compact-separator JSON. Ed25519 is deterministic (RFC 8032), so two replays
of the same run produce byte-identical spans *and* signature bytes.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidSignature

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

__all__ = [
    "ATTR_JOURNAL_ENTRY_HASH",
    "ATTR_JOURNAL_INDEX",
    "ATTR_JOURNAL_RUN_HEAD",
    "GENAI_ATTR_NAMESPACE",
    "OP_CHAT",
    "OP_EXECUTE_TOOL",
    "OP_INVOKE_AGENT",
    "OP_INVOKE_WORKFLOW",
    "PROJECTION_SCHEMA_VERSION",
    "ProjectedSpan",
    "ProjectionError",
    "ProjectionVerification",
    "SpanProjection",
    "canonical_projection_bytes",
    "derive_span_id",
    "derive_trace_id",
    "project_iterable",
    "project_spans",
    "projection_from_dict",
    "projection_to_dict",
    "recompute_span_ids",
    "sign_projection",
    "to_otlp_spans",
    "verify_projection",
]

#: Schema version of this projection envelope. Bumped on breaking changes
#: to the signed-payload shape.
PROJECTION_SCHEMA_VERSION: str = "1.0.0"

#: Namespace under which the GenAI semantic-convention attributes live.
#: Emitted only when the caller opts into the (Development-stage) GenAI
#: conventions; the journal-anchored ids and entry-hash binding do not
#: depend on it. See the OTel GenAI spec:
#: https://opentelemetry.io/docs/specs/semconv/gen-ai/
GENAI_ATTR_NAMESPACE: str = "gen_ai"

#: ``gen_ai.operation.name`` values for the four span layers the issue
#: names. These match the OTel GenAI semantic conventions.
OP_INVOKE_WORKFLOW: str = "invoke_workflow"
OP_INVOKE_AGENT: str = "invoke_agent"
OP_EXECUTE_TOOL: str = "execute_tool"
OP_CHAT: str = "chat"

#: Bernstein-specific span attribute pinning the journal row a span
#: projects. A verifier recomputes the span id from this entry hash and
#: rejects the span when the two disagree (AC2/AC3).
ATTR_JOURNAL_ENTRY_HASH: str = "bernstein.journal.entry_hash"

#: 0-based journal index the span projects. Kept for operators; not part
#: of the id derivation.
ATTR_JOURNAL_INDEX: str = "bernstein.journal.index"

#: The run's journal head that anchors the trace id. Present on the root
#: span so a verifier can rederive the trace id offline.
ATTR_JOURNAL_RUN_HEAD: str = "bernstein.journal.run_head"

#: GenAI semantic-convention attribute names re-exported so the projection
#: does not need a hard dependency on the OTel SDK.
_ATTR_GEN_AI_OPERATION_NAME: str = "gen_ai.operation.name"
_ATTR_GEN_AI_SYSTEM: str = "gen_ai.system"

#: Default ``gen_ai.system`` for projected spans. The journal is not tied
#: to one provider, so the system tag identifies the producer, not a model
#: vendor; operators filter Bernstein traffic on it.
_DEFAULT_SYSTEM: str = "bernstein"

#: Deterministic mapping from journal event type to GenAI operation layer.
#: Events not in the map project no span (control-plane bookkeeping such as
#: ``tick_start`` and WAL recovery is not GenAI activity). The map is
#: additive: new event types can be added without changing existing ids.
_EVENT_TO_OPERATION: dict[str, str] = {
    "run_started": OP_INVOKE_WORKFLOW,
    "run_completed": OP_INVOKE_WORKFLOW,
    "agent_spawned": OP_INVOKE_AGENT,
    "agent_reaped": OP_INVOKE_AGENT,
    "task_claimed": OP_EXECUTE_TOOL,
    "task_completed": OP_EXECUTE_TOOL,
    "task_retried": OP_EXECUTE_TOOL,
    "task_verification_failed": OP_EXECUTE_TOOL,
    "workflow_phase_advanced": OP_CHAT,
    "workflow_approval_granted": OP_CHAT,
}


class ProjectionError(RuntimeError):
    """Raised when a projection cannot be built, signed, or parsed.

    Raised by :func:`project_spans` when the journal is empty: the span set
    is *unproducible* without the journal, not merely unsigned.
    """


# ---------------------------------------------------------------------------
# Id derivation
# ---------------------------------------------------------------------------


def _derive_hex(*, domain: str, entry_hash: str, nbytes: int) -> str:
    """Return the first ``nbytes`` of ``H(domain, entry_hash)`` as hex.

    Domain-separated so a trace id and a span id derived from the same
    entry hash never collide. The pre-image is a canonical field tuple so
    the digest is stable across processes and platforms.
    """
    preimage = json.dumps(
        {"domain": domain, "entry_hash": entry_hash},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(preimage).hexdigest()[: nbytes * 2]


def derive_trace_id(root_entry_hash: str) -> str:
    """Return the 16-byte (32-hex-char) OTLP trace id for a run.

    Derived from the run's first journal entry hash so every span in the
    run shares one trace id and two replays derive the same id (AC1).
    """
    return _derive_hex(domain="otel.trace", entry_hash=root_entry_hash, nbytes=16)


def derive_span_id(entry_hash: str) -> str:
    """Return the 8-byte (16-hex-char) OTLP span id for a journal entry.

    A verifier recomputes this from the entry hash carried on the span and
    rejects the span when the recomputed id differs (AC2/AC3).
    """
    return _derive_hex(domain="otel.span", entry_hash=entry_hash, nbytes=8)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectedSpan:
    """One OTel span projected from a journal entry.

    Attributes:
        name: Span name (``gen_ai.<operation>``).
        span_id: 16-hex-char id derived from :attr:`entry_hash`.
        parent_span_id: Parent span id, or ``""`` for the workflow root.
        operation: ``gen_ai.operation.name`` layer.
        entry_hash: Journal entry hash this span projects (AC2 binding).
        index: 0-based journal index (operator convenience).
        attributes: Full attribute map, including the GenAI convention
            attributes when the stability flag is on.
    """

    name: str
    span_id: str
    parent_span_id: str
    operation: str
    entry_hash: str
    index: int
    attributes: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "operation": self.operation,
            "entry_hash": self.entry_hash,
            "index": self.index,
            "attributes": dict(self.attributes),
        }


@dataclass(slots=True)
class SpanProjection:
    """A signed, deterministic OTLP span set projected from a run journal.

    The span order is the journal order, so two projections of the same
    journal produce byte-identical canonical bytes (AC1).
    """

    schema_version: str
    run_id: str
    trace_id: str
    run_head: str
    genai_stability: bool
    spans: list[ProjectedSpan]
    keyid: str = ""
    signature_b64: str = ""

    @property
    def is_signed(self) -> bool:
        return bool(self.signature_b64)


@dataclass(frozen=True, slots=True)
class ProjectionVerification:
    """Outcome of :func:`verify_projection`."""

    ok: bool
    errors: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def _root_entry_hash(events: Sequence[dict[str, Any]]) -> str:
    """Return the first event's hash, the trace-id anchor."""
    return str(events[0].get("event_hash", ""))


def project_spans(
    events: Sequence[dict[str, Any]],
    *,
    run_id: str,
    genai_stability: bool = True,
    keyid: str = "",
) -> SpanProjection:
    """Project journal ``events`` into an unsigned OTLP span set.

    Each event whose type maps to a GenAI operation layer becomes one span
    whose id is derived from its journal entry hash. The first mapped span
    of the run's dominant ``invoke_workflow`` layer is the trace root; other
    spans parent onto the most recent workflow root, or onto the most recent
    agent span for tool/chat layers, giving the six-layer tree the issue
    describes without an LLM in the loop.

    Args:
        events: Journal rows in append order (from
            :func:`bernstein.core.replay.journal.load_events`).
        run_id: The run identifier (journal run id).
        genai_stability: When ``True`` (the opt-in stability flag), each
            span carries the GenAI semantic-convention attributes
            (``gen_ai.operation.name`` etc.). When ``False``, only the
            journal-anchored attributes are emitted -- the ids and the
            entry-hash binding never depend on the convention (AC4).
        keyid: Optional signing-key id embedded in the envelope so a
            verifier can select the right public key.

    Returns:
        An unsigned :class:`SpanProjection`. Pass it to :func:`sign_projection`.

    Raises:
        ProjectionError: When ``events`` is empty. The span set is
            unproducible without the journal, not merely unsigned.
    """
    if not events:
        msg = "empty journal: OTel span set is unproducible without the event journal"
        raise ProjectionError(msg)

    root_hash = _root_entry_hash(events)
    trace_id = derive_trace_id(root_hash)

    spans: list[ProjectedSpan] = []
    current_workflow: str = ""
    current_agent: str = ""

    for index, row in enumerate(events):
        event_type = str(row.get("event", ""))
        operation = _EVENT_TO_OPERATION.get(event_type)
        if operation is None:
            continue
        entry_hash = str(row.get("event_hash", ""))
        span_id = derive_span_id(entry_hash)

        if operation == OP_INVOKE_WORKFLOW:
            parent = ""
            current_workflow = span_id
            current_agent = ""
        elif operation == OP_INVOKE_AGENT:
            parent = current_workflow
            current_agent = span_id
        else:  # execute_tool / chat parent onto the active agent, else workflow
            parent = current_agent or current_workflow

        attributes: dict[str, Any] = {
            ATTR_JOURNAL_ENTRY_HASH: entry_hash,
            ATTR_JOURNAL_INDEX: index,
        }
        if parent == "":
            attributes[ATTR_JOURNAL_RUN_HEAD] = root_hash
        if genai_stability:
            attributes[_ATTR_GEN_AI_OPERATION_NAME] = operation
            attributes[_ATTR_GEN_AI_SYSTEM] = _DEFAULT_SYSTEM

        spans.append(
            ProjectedSpan(
                name=f"{GENAI_ATTR_NAMESPACE}.{operation}",
                span_id=span_id,
                parent_span_id=parent,
                operation=operation,
                entry_hash=entry_hash,
                index=index,
                attributes=attributes,
            )
        )

    return SpanProjection(
        schema_version=PROJECTION_SCHEMA_VERSION,
        run_id=run_id,
        trace_id=trace_id,
        run_head=root_hash,
        genai_stability=genai_stability,
        spans=spans,
        keyid=keyid,
        signature_b64="",
    )


# ---------------------------------------------------------------------------
# Canonical serialisation
# ---------------------------------------------------------------------------


def _signing_payload_dict(projection: SpanProjection) -> dict[str, Any]:
    """Return the dict that gets signed (excludes ``signature_b64``)."""
    return {
        "schema_version": projection.schema_version,
        "run_id": projection.run_id,
        "trace_id": projection.trace_id,
        "run_head": projection.run_head,
        "genai_stability": projection.genai_stability,
        "keyid": projection.keyid,
        "spans": [s.to_dict() for s in projection.spans],
    }


def canonical_projection_bytes(projection: SpanProjection) -> bytes:
    """Return deterministic signing bytes: sorted keys, compact, UTF-8."""
    return json.dumps(
        _signing_payload_dict(projection),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def projection_to_dict(projection: SpanProjection) -> dict[str, Any]:
    """Return the canonical dict view of the projection (with signature)."""
    payload = _signing_payload_dict(projection)
    payload["signature_b64"] = projection.signature_b64
    return payload


def projection_from_dict(payload: dict[str, Any]) -> SpanProjection:
    """Rebuild a :class:`SpanProjection` from its canonical dict view."""
    spans_raw = payload.get("spans")
    if not isinstance(spans_raw, list):
        msg = "projection 'spans' must be a JSON array"
        raise ProjectionError(msg)
    spans: list[ProjectedSpan] = []
    for item in spans_raw:
        if not isinstance(item, dict):
            continue
        attrs_raw = item.get("attributes")
        attributes: dict[str, Any] = dict(attrs_raw) if isinstance(attrs_raw, dict) else {}
        spans.append(
            ProjectedSpan(
                name=str(item.get("name", "")),
                span_id=str(item.get("span_id", "")),
                parent_span_id=str(item.get("parent_span_id", "")),
                operation=str(item.get("operation", "")),
                entry_hash=str(item.get("entry_hash", "")),
                index=int(item.get("index", 0)),
                attributes=attributes,
            )
        )
    return SpanProjection(
        schema_version=str(payload.get("schema_version", "")),
        run_id=str(payload.get("run_id", "")),
        trace_id=str(payload.get("trace_id", "")),
        run_head=str(payload.get("run_head", "")),
        genai_stability=bool(payload.get("genai_stability", False)),
        spans=spans,
        keyid=str(payload.get("keyid", "")),
        signature_b64=str(payload.get("signature_b64", "")),
    )


# ---------------------------------------------------------------------------
# Signing + verification
# ---------------------------------------------------------------------------


def sign_projection(
    projection: SpanProjection,
    *,
    signing_key: Ed25519PrivateKey,
) -> SpanProjection:
    """Attach a detached Ed25519 signature over the canonical bytes.

    Ed25519 is deterministic (RFC 8032), so signing the same projection
    twice with the same key yields byte-identical signature bytes (AC1).
    The projection is signed *without* its own signature field, so a
    verifier recomputes exactly these bytes.
    """
    sig = signing_key.sign(canonical_projection_bytes(projection))
    return SpanProjection(
        schema_version=projection.schema_version,
        run_id=projection.run_id,
        trace_id=projection.trace_id,
        run_head=projection.run_head,
        genai_stability=projection.genai_stability,
        spans=list(projection.spans),
        keyid=projection.keyid,
        signature_b64=base64.b64encode(sig).decode("ascii"),
    )


def recompute_span_ids(
    projection: SpanProjection,
    events: Sequence[dict[str, Any]],
) -> list[str]:
    """Return the errors from rederiving span ids from the journal.

    For every span, look up the journal row whose ``event_hash`` equals the
    span's ``bernstein.journal.entry_hash`` and confirm the span id equals
    ``derive_span_id`` of that hash and the trace id equals
    ``derive_trace_id`` of the run head. A span whose id was altered, or
    whose entry hash is absent from the journal, is reported (AC2/AC3).
    """
    errors: list[str] = []
    by_hash: dict[str, dict[str, Any]] = {}
    for row in events:
        h = str(row.get("event_hash", ""))
        if h:
            by_hash[h] = row

    if events:
        expected_trace = derive_trace_id(_root_entry_hash(events))
        if projection.trace_id != expected_trace:
            errors.append(f"trace id mismatch (expected {expected_trace}, got {projection.trace_id})")

    for span in projection.spans:
        anchor = str(span.attributes.get(ATTR_JOURNAL_ENTRY_HASH, span.entry_hash))
        if anchor not in by_hash:
            errors.append(f"span {span.span_id}: journal entry_hash {anchor} not found in journal")
            continue
        expected_span = derive_span_id(anchor)
        if span.span_id != expected_span:
            errors.append(f"span id mismatch (expected {expected_span}, got {span.span_id})")
    return errors


def verify_projection(
    projection: SpanProjection,
    events: Sequence[dict[str, Any]],
    public_key: Ed25519PublicKey,
) -> ProjectionVerification:
    """Verify the projection against the journal and the install identity.

    Checks, in order:

    * every span id recomputes from its journal entry hash and the trace id
      recomputes from the run head (AC2/AC3),
    * the projection carries a signature,
    * the detached Ed25519 signature verifies against ``public_key`` -- the
      install identity. Any tampering with a span changes the canonical
      bytes and fails this check.
    """
    errors: list[str] = list(recompute_span_ids(projection, events))

    if not projection.signature_b64:
        errors.append("projection is unsigned")
        return ProjectionVerification(ok=not errors, errors=tuple(errors))

    try:
        sig = base64.b64decode(projection.signature_b64.encode("ascii"), validate=True)
    except (ValueError, TypeError):
        errors.append("signature is not valid base64")
        return ProjectionVerification(ok=False, errors=tuple(errors))

    unsigned = SpanProjection(
        schema_version=projection.schema_version,
        run_id=projection.run_id,
        trace_id=projection.trace_id,
        run_head=projection.run_head,
        genai_stability=projection.genai_stability,
        spans=list(projection.spans),
        keyid=projection.keyid,
        signature_b64="",
    )
    try:
        public_key.verify(sig, canonical_projection_bytes(unsigned))
    except InvalidSignature:
        errors.append("signature does not verify against install identity")

    return ProjectionVerification(ok=not errors, errors=tuple(errors))


# ---------------------------------------------------------------------------
# OTLP shape
# ---------------------------------------------------------------------------


def to_otlp_spans(projection: SpanProjection) -> list[dict[str, Any]]:
    """Render the projection as OTLP/JSON span objects.

    The shape follows the OTLP ``Span`` message (trace/span ids as hex,
    attributes as key/value pairs) so the local JSONL store and any
    OTLP forwarder consume the same records. Kept as a pure function so the
    exported bytes are deterministic and journal-anchored.
    """
    out: list[dict[str, Any]] = []
    for span in projection.spans:
        attributes = [{"key": k, "value": _otlp_any_value(v)} for k, v in sorted(span.attributes.items())]
        out.append(
            {
                "traceId": projection.trace_id,
                "spanId": span.span_id,
                "parentSpanId": span.parent_span_id,
                "name": span.name,
                "kind": _otlp_kind(span.operation),
                "attributes": attributes,
            }
        )
    return out


def _otlp_kind(operation: str) -> str:
    """Return the OTLP ``SpanKind`` string for an operation layer."""
    if operation in {OP_EXECUTE_TOOL, OP_CHAT}:
        return "SPAN_KIND_CLIENT"
    return "SPAN_KIND_INTERNAL"


def _otlp_any_value(value: Any) -> dict[str, Any]:
    """Wrap a scalar into an OTLP ``AnyValue``."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    return {"stringValue": str(value)}


def project_iterable(events: Iterable[dict[str, Any]], *, run_id: str) -> SpanProjection:
    """Convenience wrapper accepting any iterable of journal rows."""
    return project_spans(list(events), run_id=run_id)
