"""Live OTLP transport for the journal-anchored OTel span projection (#2526).

Two halves of OpenTelemetry support existed disconnected:
:mod:`bernstein.core.observability.otel_projection` builds a deterministic,
signed span set from the run journal but is offline-only, while
:mod:`bernstein.core.observability.otlp_exporter` emits live spans whose
random SDK-generated ids carry no binding to the journal or the audit chain.

This bridge makes the OTLP wire path a *transport* for the existing
projection instead of a second source of truth:

* Spans are never created through ``Tracer.start_span`` (the SDK owns id
  generation there -- exactly why live spans were random). The bridge
  builds SDK ``ReadableSpan`` objects with an explicit ``SpanContext``
  from the journal-derived ``trace_id`` / ``span_id`` / parent produced by
  :func:`otel_projection.project_spans`, and hands them straight to the
  configured OTLP ``SpanExporter``. Nothing about span identity is
  re-derived on the wire path.
* Span start/end times come from the journal rows' recorded ``ts`` (which
  is already excluded from the Merkle hash chain), so no wall clock is
  read at export time and re-exporting the same journal is byte-identical.
* Every exported span carries ``bernstein.journal.entry_hash`` (from the
  projection) plus two bridge-added, equally journal-derived attributes:
  ``bernstein.audit.anchor`` (the first-entry projection anchor from which
  the trace id derives) and ``bernstein.run.id``. The completed audit event
  joins through that trace id and records the final journal head. Attributes
  stay deterministic across replays -- flush-time chain state is never
  stamped onto a span.
* Default off: :func:`build_bridge_from_env` returns ``None`` when
  ``BERNSTEIN_OTEL_ENDPOINT`` is unset, so no exporter is initialised and
  no network attempt is ever made.

:class:`IncrementalSpanProjector` is the streaming twin of
``project_spans``: it consumes journal rows one at a time as they append
and emits the *same* spans the batch projection would. That equality is
the gating invariant -- a live trace and a replayed projection are
literally the same spans -- and is enforced by a property test.

The offline projection artifact (``projection.otel.json``) and its
``otel.projection`` audit event remain byte-compatible: this module only
consumes ``project_spans`` output and never touches the signing payload.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.observability.otel_projection import (
    ATTR_GEN_AI_OPERATION_NAME,
    ATTR_GEN_AI_SYSTEM,
    ATTR_JOURNAL_ENTRY_HASH,
    ATTR_JOURNAL_INDEX,
    ATTR_JOURNAL_RUN_HEAD,
    DEFAULT_GEN_AI_SYSTEM,
    EVENT_TO_OPERATION,
    GENAI_ATTR_NAMESPACE,
    OP_INVOKE_AGENT,
    OP_INVOKE_WORKFLOW,
    ProjectedSpan,
    SpanProjection,
    derive_span_id,
    derive_trace_id,
    to_otlp_spans,
)
from bernstein.core.observability.otlp_exporter import (
    DEFAULT_SERVICE_NAME,
    OTLPExporterConfig,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from bernstein.core.replay.journal import EventJournal

logger = logging.getLogger(__name__)

__all__ = [
    "ATTR_AUDIT_ANCHOR",
    "ATTR_RUN_ID",
    "GENAI_STABILITY_ENV",
    "IncrementalSpanProjector",
    "JournalOTLPBridge",
    "LiveJournalSpanStream",
    "OTLPExportError",
    "ParsedExportedSpan",
    "SpanParseError",
    "SpanVerification",
    "attach_live_export",
    "build_bridge_from_env",
    "parse_exported_span",
    "projection_to_otlp_json_spans",
    "projection_to_readable_spans",
    "record_projection_audit_event",
    "verify_exported_span",
]


class OTLPExportError(RuntimeError):
    """Raised when the OTLP exporter reports a failed batch export.

    The OTLP/gRPC ``SpanExporter`` signals an unreachable or rejecting
    collector by *returning* ``SpanExportResult.FAILURE`` -- it does not
    raise. A caller that ignores the return value would report a successful
    export (and record the ``otel.projection`` audit event) for spans that
    never reached the collector. :meth:`JournalOTLPBridge.export_batch`
    turns that ``FAILURE`` into this exception so the synchronous backfill
    path fails loudly instead of falsely reporting delivery.
    """


#: Stable projection anchor carried on the wire: the first journal entry hash
#: from which the trace id is derived. The completed ``otel.projection`` audit
#: event records that same trace id plus the journal's final head. A verifier
#: therefore checks ``derive_trace_id(anchor) == audit.trace_id`` and then the
#: final journal head, without stamping flush-time audit-chain state on spans.
ATTR_AUDIT_ANCHOR = "bernstein.audit.anchor"

#: Run identifier stamped on every exported span so a verifier can locate
#: the journal (``.sdd/runs/<run_id>/journal.jsonl``) without out-of-band
#: context.
ATTR_RUN_ID = "bernstein.run.id"

#: Environment switch for Development-stage GenAI semantic-convention
#: attributes on the live path. Identity and ordering never depend on it.
GENAI_STABILITY_ENV = "BERNSTEIN_OTEL_GENAI_STABILITY"

#: Instrumentation scope name for bridge-exported spans. Fixed string so
#: exports stay deterministic.
_SCOPE_NAME = "bernstein.otel_bridge"


def _ts_to_ns(ts: Any) -> int:
    """Convert a journal row's ``ts`` (float epoch seconds) to integer ns."""
    try:
        return round(float(ts) * 1e9)
    except (TypeError, ValueError):
        return 0


def _is_export_failure(result: Any) -> bool:
    """Return ``True`` only when ``result`` is an explicit ``FAILURE``.

    ``SpanExporter.export`` returns a ``SpanExportResult`` enum
    (``SUCCESS`` / ``FAILURE``); some thin exporters return ``None`` to mean
    success. Only a concrete ``SpanExportResult.FAILURE`` is treated as a
    failure so success and legacy ``None`` returns both pass through.
    """
    try:
        from opentelemetry.sdk.trace.export import SpanExportResult
    except ImportError:  # pragma: no cover - otel extra present wherever export runs
        return False
    return result is SpanExportResult.FAILURE


def _genai_stability_from_env(env: Mapping[str, str] | None = None) -> bool:
    """Resolve the live GenAI stability switch (enabled by default)."""
    import os

    source = env if env is not None else os.environ
    raw = source.get(GENAI_STABILITY_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _wire_attributes(span: ProjectedSpan, *, run_head: str, run_id: str) -> dict[str, Any]:
    """Return the exact attribute map transported on the wire."""
    attributes: dict[str, Any] = dict(span.attributes)
    attributes[ATTR_AUDIT_ANCHOR] = run_head
    attributes[ATTR_RUN_ID] = run_id
    return attributes


def _span_kind(operation: str) -> Any:
    """Mirror ``otel_projection._otlp_kind``: tool/chat CLIENT, else INTERNAL."""
    from opentelemetry.trace import SpanKind

    if operation in {OP_INVOKE_WORKFLOW, OP_INVOKE_AGENT}:
        return SpanKind.INTERNAL
    return SpanKind.CLIENT


def _readable_span(
    span: ProjectedSpan,
    *,
    trace_id: str,
    run_head: str,
    run_id: str,
    start_ns: int,
    end_ns: int,
    resource: Any,
    scope: Any,
) -> Any:
    """Build one SDK ``ReadableSpan`` carrying the projection's exact identity.

    The ``SpanContext`` is constructed from the journal-derived ids -- the
    SDK id generator is never consulted -- and the attribute map is the
    projected span's attributes plus the two journal-derived bridge
    attributes (:data:`ATTR_AUDIT_ANCHOR`, :data:`ATTR_RUN_ID`).
    """
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import SpanContext, TraceFlags
    from opentelemetry.trace.status import Status, StatusCode

    trace_id_int = int(trace_id, 16)
    context = SpanContext(
        trace_id=trace_id_int,
        span_id=int(span.span_id, 16),
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    parent = None
    if span.parent_span_id:
        parent = SpanContext(
            trace_id=trace_id_int,
            span_id=int(span.parent_span_id, 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )

    attributes = _wire_attributes(span, run_head=run_head, run_id=run_id)

    return ReadableSpan(
        name=span.name,
        context=context,
        parent=parent,
        resource=resource,
        attributes=attributes,
        events=(),
        links=(),
        kind=_span_kind(span.operation),
        status=Status(StatusCode.UNSET),
        start_time=start_ns,
        end_time=end_ns,
        instrumentation_scope=scope,
    )


def _build_resource_and_scope(service_name: str) -> tuple[Any, Any]:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope

    resource = Resource.create({"service.name": service_name})
    return resource, InstrumentationScope(_SCOPE_NAME)


def projection_to_readable_spans(
    projection: SpanProjection,
    events: Sequence[dict[str, Any]],
    *,
    service_name: str = DEFAULT_SERVICE_NAME,
) -> list[Any]:
    """Render a batch projection as SDK ``ReadableSpan`` objects.

    ``events`` supplies the journal rows the projection was built from;
    each span's start/end time is its row's recorded ``ts``. Every journal
    entry projects one instantaneous span (start == end), matching the
    offline projection's one-span-per-entry shape.
    """
    resource, scope = _build_resource_and_scope(service_name)
    out: list[Any] = []
    for span in projection.spans:
        ts_ns = _ts_to_ns(events[span.index].get("ts")) if span.index < len(events) else 0
        out.append(
            _readable_span(
                span,
                trace_id=projection.trace_id,
                run_head=projection.run_head,
                run_id=projection.run_id,
                start_ns=ts_ns,
                end_ns=ts_ns,
                resource=resource,
                scope=scope,
            )
        )
    return out


def projection_to_otlp_json_spans(
    projection: SpanProjection,
    events: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Render the actual bridge span payload as deterministic OTLP/JSON.

    This is the network-free preview used by ``telemetry export-otel
    --dry-run``. Unlike the offline projection shape, it includes the two
    bridge attributes and the journal-recorded wire timestamps.
    """
    rendered = to_otlp_spans(projection)
    for item, span in zip(rendered, projection.spans, strict=True):
        attributes = _wire_attributes(span, run_head=projection.run_head, run_id=projection.run_id)
        item["attributes"] = [
            {"key": key, "value": _otlp_json_value(value)} for key, value in sorted(attributes.items())
        ]
        ts_ns = _ts_to_ns(events[span.index].get("ts")) if span.index < len(events) else 0
        item["startTimeUnixNano"] = str(ts_ns)
        item["endTimeUnixNano"] = str(ts_ns)
    return rendered


def _otlp_json_value(value: Any) -> dict[str, Any]:
    """Encode one scalar as an OTLP/JSON ``AnyValue``."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    return {"stringValue": str(value)}


# ---------------------------------------------------------------------------
# Wire bridge
# ---------------------------------------------------------------------------


class JournalOTLPBridge:
    """Owns the OTLP span exporter and pushes journal-projected spans to it.

    Args:
        config: Exporter configuration (endpoint, service name). Ignored
            when ``span_exporter`` is supplied.
        span_exporter: Pre-built SDK ``SpanExporter`` (tests inject an
            ``InMemorySpanExporter`` here so nothing touches the network).
        batch: When ``True`` (live path), spans submitted via
            :meth:`submit` go through a ``BatchSpanProcessor`` worker
            thread so journal appends never block on gRPC. When ``False``
            (backfill / tests), every export is synchronous.
    """

    def __init__(
        self,
        config: OTLPExporterConfig | None = None,
        *,
        span_exporter: Any | None = None,
        batch: bool = True,
    ) -> None:
        self._config = config or OTLPExporterConfig.from_env()
        self._exporter: Any | None = span_exporter
        self._processor: Any | None = None
        if self._exporter is None:
            self._exporter = self._build_otlp_exporter()
        if self._exporter is not None and batch:
            self._processor = self._build_processor(self._exporter)
            if self._processor is None:
                # Live journal observers execute in append order under the
                # journal lock. Never fall back to synchronous network I/O on
                # that path; disable export and leave backfill (batch=False)
                # as the explicit synchronous surface.
                try:
                    self._exporter.shutdown()
                except Exception as exc:
                    logger.warning("OTel bridge exporter cleanup failed: %s", exc)
                self._exporter = None

    @property
    def enabled(self) -> bool:
        """Whether the bridge has an active span exporter."""
        return self._exporter is not None

    @property
    def service_name(self) -> str:
        return self._config.service_name

    def submit(self, readable_span: Any) -> None:
        """Queue one span for export (non-blocking on the live path)."""
        if self._exporter is None:
            return
        if self._processor is not None:
            self._processor.on_end(readable_span)
            return
        self._exporter.export((readable_span,))

    def export_batch(self, readable_spans: Sequence[Any]) -> int:
        """Synchronously export ``readable_spans``; returns the count.

        Raises:
            OTLPExportError: when the exporter returns
                ``SpanExportResult.FAILURE`` (an unreachable or rejecting
                collector is reported by return value, not by raising). The
                span count is only returned on a delivered batch, so callers
                never report success for spans the collector refused.
        """
        if self._exporter is None or not readable_spans:
            return 0
        spans = tuple(readable_spans)
        result = self._exporter.export(spans)
        if _is_export_failure(result):
            raise OTLPExportError(
                f"OTLP exporter reported {getattr(result, 'name', result)} exporting {len(spans)} span(s); "
                "no spans were delivered to the collector",
            )
        return len(spans)

    def export_projection(
        self,
        projection: SpanProjection,
        events: Sequence[dict[str, Any]],
    ) -> int:
        """Export a full batch projection (the backfill path)."""
        spans = projection_to_readable_spans(projection, events, service_name=self._config.service_name)
        return self.export_batch(spans)

    def shutdown(self) -> None:
        """Flush any queued spans and tear the exporter down."""
        try:
            if self._processor is not None:
                self._processor.shutdown()
            elif self._exporter is not None:
                self._exporter.shutdown()
        except Exception as exc:
            logger.warning("OTel bridge shutdown failed: %s", exc)

    # ----------------------------------------------------------- Internals

    def _build_otlp_exporter(self) -> Any | None:
        """Build the OTLP/gRPC span exporter, or ``None`` when unavailable.

        Mirrors :class:`otlp_exporter.GenAIOTLPExporter`: no endpoint means
        no construction at all; a missing optional gRPC extra logs one
        warning and leaves the bridge disabled-but-callable.
        """
        if self._config.endpoint is None:
            return None
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError:
            logger.warning(
                "OTel bridge disabled - install 'bernstein[otel]' for opentelemetry-exporter-otlp-proto-grpc",
            )
            return None
        try:
            return OTLPSpanExporter(
                endpoint=self._config.endpoint,
                insecure=self._config.insecure,
                headers=tuple(self._config.headers.items()) or None,
            )
        except Exception as exc:
            logger.warning("OTel bridge exporter init failed: %s", exc)
            return None

    @staticmethod
    def _build_processor(exporter: Any) -> Any | None:
        try:
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            return BatchSpanProcessor(exporter)
        except Exception as exc:
            logger.warning("OTel bridge batch processor init failed; live export disabled: %s", exc)
            return None


def build_bridge_from_env(env: Mapping[str, str] | None = None) -> JournalOTLPBridge | None:
    """Return a live bridge, or ``None`` when export is not configured.

    Default off: with ``BERNSTEIN_OTEL_ENDPOINT`` unset this returns
    ``None`` *before* any exporter class is imported or constructed, so a
    default install performs zero exporter initialisation and zero network
    attempts.
    """
    config = OTLPExporterConfig.from_env(env)
    if config.endpoint is None:
        return None
    bridge = JournalOTLPBridge(config)
    return bridge if bridge.enabled else None


# ---------------------------------------------------------------------------
# Incremental projection
# ---------------------------------------------------------------------------


class IncrementalSpanProjector:
    """Streaming twin of :func:`otel_projection.project_spans`.

    Consumes journal rows one at a time (in append order) and returns the
    span each row projects, maintaining the same parent state machine as
    the batch projection. Gating invariant, enforced by a property test:
    the spans emitted across :meth:`observe` calls are exactly
    ``project_spans(all_rows).spans`` -- same ids, same parents, same
    attributes -- so a live trace and a replayed projection are literally
    the same spans.
    """

    def __init__(self, run_id: str, *, genai_stability: bool = True) -> None:
        self._run_id = run_id
        self._genai_stability = genai_stability
        self._index = 0
        self._root_hash = ""
        self._trace_id = ""
        self._current_workflow = ""
        self._current_agent = ""

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def trace_id(self) -> str:
        """Trace id derived from the run's first observed row ('' until then)."""
        return self._trace_id

    @property
    def run_head(self) -> str:
        """The root entry hash anchoring the trace ('' until the first row)."""
        return self._root_hash

    def observe(self, row: dict[str, Any]) -> ProjectedSpan | None:
        """Project one appended journal row; ``None`` for control-plane rows.

        Must be called with every journal row in append order: unmapped
        rows still advance the journal index and the first row (mapped or
        not) anchors the trace id, exactly as the batch projection does.
        """
        index = self._index
        self._index += 1
        if index == 0:
            self._root_hash = str(row.get("event_hash", ""))
            self._trace_id = derive_trace_id(self._root_hash)

        event_type = str(row.get("event", ""))
        operation = EVENT_TO_OPERATION.get(event_type)
        if operation is None:
            return None
        entry_hash = str(row.get("event_hash", ""))
        span_id = derive_span_id(entry_hash)

        if operation == OP_INVOKE_WORKFLOW:
            parent = ""
            self._current_workflow = span_id
            self._current_agent = ""
        elif operation == OP_INVOKE_AGENT:
            parent = self._current_workflow
            self._current_agent = span_id
        else:  # execute_tool / chat parent onto the active agent, else workflow
            parent = self._current_agent or self._current_workflow

        attributes: dict[str, Any] = {
            ATTR_JOURNAL_ENTRY_HASH: entry_hash,
            ATTR_JOURNAL_INDEX: index,
        }
        if parent == "":
            attributes[ATTR_JOURNAL_RUN_HEAD] = self._root_hash
        if self._genai_stability:
            attributes[ATTR_GEN_AI_OPERATION_NAME] = operation
            attributes[ATTR_GEN_AI_SYSTEM] = DEFAULT_GEN_AI_SYSTEM

        return ProjectedSpan(
            name=f"{GENAI_ATTR_NAMESPACE}.{operation}",
            span_id=span_id,
            parent_span_id=parent,
            operation=operation,
            entry_hash=entry_hash,
            index=index,
            attributes=attributes,
        )


# ---------------------------------------------------------------------------
# Live journal stream
# ---------------------------------------------------------------------------


class LiveJournalSpanStream:
    """Journal-append observer that streams projected spans to the bridge.

    Attach via :func:`attach_live_export`. Each appended row runs through
    the incremental projector; mapped rows become ``ReadableSpan`` objects
    (timestamped from the row's own ``ts``) and are queued on the bridge's
    batch processor, so the orchestrator tick loop never blocks on gRPC.

    :meth:`finalize` flushes the wire and records the run's
    ``otel.projection`` audit event so any live-exported span can be
    matched back to the chain.
    """

    def __init__(
        self,
        *,
        bridge: JournalOTLPBridge,
        run_id: str,
        workdir: Path,
        journal_path: Path,
        genai_stability: bool = True,
    ) -> None:
        self._bridge = bridge
        self._workdir = workdir
        self._journal_path = journal_path
        self._genai_stability = genai_stability
        self._projector = IncrementalSpanProjector(run_id, genai_stability=genai_stability)
        self._resource, self._scope = _build_resource_and_scope(bridge.service_name)
        self._span_count = 0
        self._finalized = False

    @property
    def span_count(self) -> int:
        """Number of spans handed to the bridge so far."""
        return self._span_count

    def on_journal_append(self, row: dict[str, Any]) -> None:
        """Observer hook for :meth:`EventJournal.record`.

        Never raises: telemetry must not be able to wedge the journal.
        """
        try:
            span = self._projector.observe(row)
            if span is None:
                return
            ts_ns = _ts_to_ns(row.get("ts"))
            readable = _readable_span(
                span,
                trace_id=self._projector.trace_id,
                run_head=self._projector.run_head,
                run_id=self._projector.run_id,
                start_ns=ts_ns,
                end_ns=ts_ns,
                resource=self._resource,
                scope=self._scope,
            )
            self._bridge.submit(readable)
            self._span_count += 1
        except Exception as exc:
            logger.warning("live OTel span export failed: %s", exc)

    def finalize(self) -> None:
        """Flush the wire and anchor the run's export into the audit chain.

        Loads the finished journal, rebuilds and signs the canonical
        projection (identical bytes to ``bernstein trace project``), and
        records the ``otel.projection`` audit event. Best-effort
        throughout: a completed run is never failed by telemetry.
        """
        if self._finalized:
            return
        self._finalized = True
        try:
            self._bridge.shutdown()
        except Exception as exc:
            logger.warning("OTel bridge flush at run end failed: %s", exc)
        try:
            record_projection_audit_event(
                workdir=self._workdir,
                journal_path=self._journal_path,
                run_id=self._projector.run_id,
                genai_stability=self._genai_stability,
            )
        except Exception as exc:
            logger.warning("otel projection audit record failed: %s", exc)


def attach_live_export(
    journal: EventJournal,
    *,
    workdir: Path,
    env: Mapping[str, str] | None = None,
    genai_stability: bool | None = None,
) -> LiveJournalSpanStream | None:
    """Wire live OTLP export onto ``journal``, or no-op when unconfigured.

    Returns the attached stream (call ``finalize()`` at run end), or
    ``None`` when ``BERNSTEIN_OTEL_ENDPOINT`` is unset -- in which case
    nothing is constructed and the journal is left untouched. Never
    raises: observability wiring must not be able to fail a run.
    """
    try:
        bridge = build_bridge_from_env(env)
        if bridge is None:
            return None
        stream = LiveJournalSpanStream(
            bridge=bridge,
            run_id=journal.run_id,
            workdir=workdir,
            journal_path=journal.path,
            genai_stability=(_genai_stability_from_env(env) if genai_stability is None else genai_stability),
        )
        journal.set_observer(stream.on_journal_append)
    except Exception as exc:
        logger.warning("live OTel export wiring failed: %s", exc)
        return None
    return stream


# ---------------------------------------------------------------------------
# Audit anchoring
# ---------------------------------------------------------------------------


def record_projection_audit_event(
    *,
    workdir: Path,
    journal_path: Path,
    run_id: str,
    genai_stability: bool = True,
) -> None:
    """Sign the run's canonical projection and record ``otel.projection``.

    Rebuilds the projection from the journal, signs it with the install
    identity (the same key and canonical bytes ``bernstein trace project``
    uses), and appends the audit event binding the exported trace to the
    chain. The projection is deterministic, so the event carries the same
    ``trace_id`` / ``projection_sha256`` an offline projection of the same
    journal would. Wire spans carry the projection's first-entry anchor;
    its derived trace id is the stable join key to this event, while this
    event's ``journal_head`` records the completed chain's final hash.
    """
    from bernstein.core.observability.otel_projection import (
        canonical_projection_bytes,
        project_spans,
        sign_projection,
    )
    from bernstein.core.replay.journal import load_events
    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import AuditChainStore, record_otel_projection
    from bernstein.core.security.audit_dsse import keyid_from_public_key
    from bernstein.core.security.install_key import load_or_create_install_key, signing_key_path

    events = load_events(journal_path)
    if not events:
        return
    key = load_or_create_install_key(signing_key_path(workdir))
    projection = project_spans(
        events,
        run_id=run_id,
        genai_stability=genai_stability,
        keyid=keyid_from_public_key(key.public_key()),
    )
    signed = sign_projection(projection, signing_key=key)

    chain = AuditChainStore(workdir / ".sdd" / "audit", key=load_or_create_audit_key())
    record_otel_projection(
        chain=chain,
        run_id=run_id,
        journal_head=str(events[-1].get("event_hash", "")),
        trace_id=signed.trace_id,
        span_count=len(signed.spans),
        projection_sha256=hashlib.sha256(canonical_projection_bytes(signed)).hexdigest(),
    )


# ---------------------------------------------------------------------------
# Span verification (#2526 Phase 3)
# ---------------------------------------------------------------------------


class SpanParseError(ValueError):
    """Raised when pasted span JSON is not a recognisable OTLP span."""


@dataclass(frozen=True, slots=True)
class ParsedExportedSpan:
    """The identity-bearing fields lifted from an exported OTLP span.

    Attributes:
        span_id: Lower-case hex span id (``spanId``).
        trace_id: Lower-case hex trace id, or ``""`` when absent.
        parent_span_id: Lower-case hex parent span id, or ``""``.
        entry_hash: The ``bernstein.journal.entry_hash`` attribute.
        anchor: The ``bernstein.audit.anchor`` attribute.
        run_id: The ``bernstein.run.id`` attribute, or ``""``.
        index: The ``bernstein.journal.index`` attribute, or ``-1``.
        attributes: The flattened attribute map.
    """

    span_id: str
    trace_id: str
    parent_span_id: str
    entry_hash: str
    anchor: str
    run_id: str
    index: int
    attributes: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SpanVerification:
    """Outcome of :func:`verify_exported_span`.

    ``ok`` is the accept/reject verdict. When ``ok`` is ``False`` and
    ``unverifiable`` is set the span could not be proven either way (no
    journal, or the run was never anchored into the audit chain); otherwise
    the span is a positive forgery. Both non-ok outcomes are nonzero exits on
    the CLI -- a real rejection is never softened into a pass.
    """

    ok: bool
    reason: str
    unverifiable: bool = False
    span_id: str = ""
    entry_hash: str = ""
    anchor: str = ""
    trace_id: str = ""
    index: int = -1
    chain_trace_id: str = ""


def _otlp_scalar(value: Any) -> Any:
    """Unwrap an OTLP ``AnyValue`` dict to its scalar, or pass a plain value through."""
    if isinstance(value, dict):
        for key in ("stringValue", "intValue", "boolValue", "doubleValue"):
            if key in value:
                return value[key]
        return ""
    return value


def _flatten_attributes(raw: Any) -> dict[str, Any]:
    """Return a flat ``{name: scalar}`` map from either attribute shape.

    Accepts both the OTLP/JSON list form (``[{"key": k, "value": {...}}]`` -- what a
    stock collector emits) and a plain object (``{k: v}`` -- what an operator may
    hand-simplify), so a span copied from any pipeline parses.
    """
    out: dict[str, Any] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and "key" in item:
                out[str(item["key"])] = _otlp_scalar(item.get("value"))
    elif isinstance(raw, dict):
        for key, value in raw.items():
            out[str(key)] = _otlp_scalar(value)
    return out


def _hex(value: Any) -> str:
    """Normalise a hex id/hash for comparison (hex is case-insensitive)."""
    return str(value if value is not None else "").strip().lower()


def parse_exported_span(payload: Any) -> ParsedExportedSpan:
    """Lift the identity-bearing fields from a pasted OTLP span.

    Args:
        payload: A single decoded OTLP span object (camelCase ``spanId`` or
            snake ``span_id``; attributes in either OTLP/JSON list form or a
            flat object).

    Returns:
        The parsed span.

    Raises:
        SpanParseError: When ``payload`` is not an object or carries no span id.
    """
    if not isinstance(payload, dict):
        raise SpanParseError("exported span must be a JSON object")
    span_id = _hex(payload.get("spanId") or payload.get("span_id"))
    if not span_id:
        raise SpanParseError("exported span carries no spanId")
    attributes = _flatten_attributes(payload.get("attributes"))
    index_raw = attributes.get(ATTR_JOURNAL_INDEX)
    try:
        index = int(str(index_raw)) if index_raw is not None else -1
    except (TypeError, ValueError):
        index = -1
    return ParsedExportedSpan(
        span_id=span_id,
        trace_id=_hex(payload.get("traceId") or payload.get("trace_id")),
        parent_span_id=_hex(payload.get("parentSpanId") or payload.get("parent_span_id")),
        entry_hash=_hex(attributes.get(ATTR_JOURNAL_ENTRY_HASH)),
        anchor=_hex(attributes.get(ATTR_AUDIT_ANCHOR)),
        run_id=str(attributes.get(ATTR_RUN_ID, "")).strip(),
        index=index,
        attributes=attributes,
    )


def _forged(reason: str, span: ParsedExportedSpan) -> SpanVerification:
    return SpanVerification(
        ok=False,
        reason=reason,
        unverifiable=False,
        span_id=span.span_id,
        entry_hash=span.entry_hash,
        anchor=span.anchor,
    )


def _unverifiable(reason: str, span: ParsedExportedSpan) -> SpanVerification:
    return SpanVerification(
        ok=False,
        reason=reason,
        unverifiable=True,
        span_id=span.span_id,
        entry_hash=span.entry_hash,
        anchor=span.anchor,
    )


def verify_exported_span(
    span: ParsedExportedSpan,
    events: Sequence[dict[str, Any]],
    projections: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
) -> SpanVerification:
    """Prove one exported span against the journal and the audit chain.

    Re-derives the span id from the ``bernstein.journal.entry_hash`` the span
    carries with :func:`otel_projection.derive_span_id` -- the *same* function
    the export bridge used, so the verify path can never drift from the
    producing path -- confirms that entry exists in ``run_id``'s journal, and
    checks the ``bernstein.audit.anchor`` resolves through
    :func:`otel_projection.derive_trace_id` to the run's ``otel.projection``
    audit event.

    Args:
        span: The parsed exported span (see :func:`parse_exported_span`).
        events: The run's journal rows in append order.
        projections: The ``details`` payloads of the run's ``otel.projection``
            audit events (each carrying ``run_id`` / ``trace_id`` /
            ``journal_head``).
        run_id: The run whose journal and chain the span is checked against.

    Returns:
        The verdict. ``ok`` is the accept/reject decision; a rejection with
        ``unverifiable`` set means the span could not be proven either way (no
        journal, or the export was never anchored), never that it passed.
    """
    if span.run_id and span.run_id != run_id:
        return _forged(
            f"span carries {ATTR_RUN_ID}={span.run_id!r} but was checked against run {run_id!r}",
            span,
        )
    if not events:
        return _unverifiable(
            f"no event journal for run {run_id!r}; span identity cannot be recomputed",
            span,
        )

    index_by_hash = {str(row.get("event_hash", "")): i for i, row in enumerate(events) if row.get("event_hash")}
    root_hash = str(events[0].get("event_hash", ""))

    if not span.entry_hash:
        return _forged(f"span carries no {ATTR_JOURNAL_ENTRY_HASH}; it is unbindable to the journal", span)
    if span.entry_hash not in index_by_hash:
        return _forged(
            f"{ATTR_JOURNAL_ENTRY_HASH} {span.entry_hash} is absent from run {run_id!r}'s journal",
            span,
        )

    expected_span_id = derive_span_id(span.entry_hash)
    if span.span_id != expected_span_id:
        return _forged(
            f"span id {span.span_id} does not recompute from entry_hash {span.entry_hash} "
            f"(expected {expected_span_id})",
            span,
        )

    if not span.anchor:
        return _forged(f"span carries no {ATTR_AUDIT_ANCHOR}; its trace is unanchored", span)
    if span.anchor != root_hash:
        return _forged(
            f"{ATTR_AUDIT_ANCHOR} {span.anchor} does not match run {run_id!r}'s journal head {root_hash}",
            span,
        )

    expected_trace = derive_trace_id(span.anchor)
    if span.trace_id and span.trace_id != expected_trace:
        return _forged(
            f"traceId {span.trace_id} does not derive from the anchor (expected {expected_trace})",
            span,
        )

    run_projections = [p for p in projections if str(p.get("run_id", "")) == run_id]
    if not run_projections:
        return _unverifiable(
            f"no otel.projection audit event for run {run_id!r}; the export was never anchored into the chain",
            span,
        )
    matching = [p for p in run_projections if _hex(p.get("trace_id")) == expected_trace]
    if not matching:
        return _forged(
            f"anchor-derived trace {expected_trace} matches no otel.projection audit event for run {run_id!r}",
            span,
        )
    journal_head = str(events[-1].get("event_hash", ""))
    if not any(str(p.get("journal_head", "")) == journal_head for p in matching):
        return _forged(
            f"otel.projection audit event for trace {expected_trace} anchors a different journal head",
            span,
        )

    return SpanVerification(
        ok=True,
        reason="span id recomputes from the journal entry and its anchor matches the audit chain",
        span_id=span.span_id,
        entry_hash=span.entry_hash,
        anchor=span.anchor,
        trace_id=expected_trace,
        index=index_by_hash[span.entry_hash],
        chain_trace_id=expected_trace,
    )
