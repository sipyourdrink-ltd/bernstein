"""Unit tests for the live OTLP bridge over the journal projection (#2526).

The bridge is a *transport* for the deterministic span projection built by
:mod:`bernstein.core.observability.otel_projection`: ids and attributes on
the wire are exactly the projection's, timestamps come from the journal
rows, and with no endpoint configured nothing is initialised and nothing
touches the network.

Gating invariant (maintainer-required): the incremental projector emits
exactly the spans the batch ``project_spans`` emits, so a live trace and a
replayed projection are literally the same spans.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from bernstein.core.observability.otel_bridge import (
    ATTR_AUDIT_ANCHOR,
    ATTR_RUN_ID,
    GENAI_STABILITY_ENV,
    IncrementalSpanProjector,
    JournalOTLPBridge,
    LiveJournalSpanStream,
    attach_live_export,
    build_bridge_from_env,
    projection_to_readable_spans,
)
from bernstein.core.observability.otel_projection import (
    ATTR_JOURNAL_ENTRY_HASH,
    derive_trace_id,
    project_spans,
)
from bernstein.core.observability.otlp_exporter import OTEL_ENDPOINT_ENV, OTLPExporterConfig
from bernstein.core.replay.journal import EventJournal, load_events

_RUN_ID = "run-1"


def _write_journal(sdd_dir, run_id: str = _RUN_ID) -> EventJournal:
    """Record a representative run: workflow, agent, tool, control-plane rows."""
    journal = EventJournal(run_id, sdd_dir)
    journal.record("run_started", goal="ship")
    journal.record("agent_spawned", agent_id="a1")
    journal.record("task_claimed", task_id="t1")
    journal.record("tick_start", tick=1)  # control-plane: no span
    journal.record("task_completed", task_id="t1")
    journal.record("agent_reaped", agent_id="a1")
    journal.record("run_completed", ok=True)
    return journal


def _events(tmp_path, run_id: str = _RUN_ID) -> list[dict]:
    return load_events(_write_journal(tmp_path / ".sdd", run_id).path)


def _bridge(exporter: InMemorySpanExporter) -> JournalOTLPBridge:
    return JournalOTLPBridge(
        OTLPExporterConfig(endpoint=None),
        span_exporter=exporter,
        batch=False,
    )


# ---------------------------------------------------------------------------
# Identity preservation: wire ids and attributes ARE the projection's
# ---------------------------------------------------------------------------


def test_wire_span_ids_are_the_projections(tmp_path) -> None:
    """Exported trace/span/parent ids equal the journal-derived projection ids."""
    events = _events(tmp_path)
    projection = project_spans(events, run_id=_RUN_ID)
    exporter = InMemorySpanExporter()
    _bridge(exporter).export_projection(projection, events)

    finished = exporter.get_finished_spans()
    assert len(finished) == len(projection.spans)
    for wire, projected in zip(finished, projection.spans, strict=True):
        assert format(wire.context.trace_id, "032x") == projection.trace_id
        assert format(wire.context.span_id, "016x") == projected.span_id
        if projected.parent_span_id:
            assert format(wire.parent.span_id, "016x") == projected.parent_span_id
        else:
            assert wire.parent is None


def test_wire_spans_carry_entry_hash_and_anchor(tmp_path) -> None:
    """Every exported span carries the journal binding and the audit anchor."""
    events = _events(tmp_path)
    projection = project_spans(events, run_id=_RUN_ID)
    exporter = InMemorySpanExporter()
    _bridge(exporter).export_projection(projection, events)

    for wire in exporter.get_finished_spans():
        assert wire.attributes[ATTR_JOURNAL_ENTRY_HASH]
        assert wire.attributes[ATTR_AUDIT_ANCHOR] == projection.run_head
        assert wire.attributes[ATTR_RUN_ID] == _RUN_ID


def test_anchor_recomputes_audit_event_trace_id(tmp_path, monkeypatch) -> None:
    """The first-entry wire anchor joins to the completed audit event by trace id."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    monkeypatch.setenv("BERNSTEIN_CREDENTIAL_SIGNING_KEY", str(tmp_path / "install.key"))
    exporter = InMemorySpanExporter()
    journal, stream = _live_stream(tmp_path, exporter)
    journal.record("run_started", goal="ship")
    journal.record("run_completed", ok=True)
    stream.finalize()

    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import EVENT_OTEL_PROJECTION, AuditChainStore

    events = load_events(journal.path)
    audit = AuditChainStore(tmp_path / ".sdd" / "audit", key=load_or_create_audit_key()).query(
        event_type=EVENT_OTEL_PROJECTION
    )[0]
    anchor = exporter.get_finished_spans()[0].attributes[ATTR_AUDIT_ANCHOR]
    assert anchor == events[0]["event_hash"]
    assert derive_trace_id(str(anchor)) == audit.details["trace_id"]
    assert audit.details["journal_head"] == events[-1]["event_hash"]


def test_full_span_tree_on_wire_not_only_leaves(tmp_path) -> None:
    """The workflow -> agent -> tool tree exports intact (parents included)."""
    events = _events(tmp_path)
    projection = project_spans(events, run_id=_RUN_ID)
    exporter = InMemorySpanExporter()
    _bridge(exporter).export_projection(projection, events)

    by_op = {}
    for wire in exporter.get_finished_spans():
        by_op.setdefault(wire.attributes["gen_ai.operation.name"], []).append(wire)
    assert set(by_op) >= {"invoke_workflow", "invoke_agent", "execute_tool"}
    agent = by_op["invoke_agent"][0]
    tool = by_op["execute_tool"][0]
    workflow_root = by_op["invoke_workflow"][0]
    assert agent.parent.span_id == workflow_root.context.span_id
    assert tool.parent.span_id == agent.context.span_id


def test_timestamps_come_from_journal_rows(tmp_path) -> None:
    """Span times are the journal rows' recorded ts, not a fresh clock read."""
    events = _events(tmp_path)
    projection = project_spans(events, run_id=_RUN_ID)
    exporter = InMemorySpanExporter()
    _bridge(exporter).export_projection(projection, events)

    for wire, projected in zip(exporter.get_finished_spans(), projection.spans, strict=True):
        expected_ns = round(float(events[projected.index]["ts"]) * 1e9)
        assert wire.start_time == expected_ns
        assert wire.end_time == expected_ns


def test_reexport_same_journal_byte_identical(tmp_path) -> None:
    """AC: two exports over the same journal emit byte-identical span trees."""
    events = _events(tmp_path)
    projection = project_spans(events, run_id=_RUN_ID)

    def snapshot() -> list[tuple]:
        exporter = InMemorySpanExporter()
        _bridge(exporter).export_projection(projection, events)
        return [
            (
                s.name,
                s.context.trace_id,
                s.context.span_id,
                s.parent.span_id if s.parent else None,
                s.start_time,
                s.end_time,
                dict(s.attributes),
            )
            for s in exporter.get_finished_spans()
        ]

    assert snapshot() == snapshot()


# ---------------------------------------------------------------------------
# Gating invariant: incremental == batch project_spans
# ---------------------------------------------------------------------------


def test_incremental_equals_batch_projection(tmp_path) -> None:
    """The streaming projector emits exactly the batch projection's spans."""
    events = _events(tmp_path)
    batch = project_spans(events, run_id=_RUN_ID)

    projector = IncrementalSpanProjector(_RUN_ID)
    streamed = [span for row in events if (span := projector.observe(row)) is not None]

    assert projector.trace_id == batch.trace_id
    assert projector.run_head == batch.run_head
    assert streamed == batch.spans


def test_incremental_equals_batch_without_genai_stability(tmp_path) -> None:
    """Equality holds with the GenAI convention attributes flagged off."""
    events = _events(tmp_path)
    batch = project_spans(events, run_id=_RUN_ID, genai_stability=False)
    projector = IncrementalSpanProjector(_RUN_ID, genai_stability=False)
    streamed = [span for row in events if (span := projector.observe(row)) is not None]
    assert streamed == batch.spans


def test_incremental_equals_batch_multi_agent_interleaving(tmp_path) -> None:
    """Parent state (workflow/agent) tracks identically across agent churn."""
    journal = EventJournal("run-multi", tmp_path / ".sdd")
    journal.record("run_started", goal="ship")
    journal.record("agent_spawned", agent_id="a1")
    journal.record("task_claimed", task_id="t1")
    journal.record("agent_spawned", agent_id="a2")
    journal.record("task_claimed", task_id="t2")
    journal.record("task_retried", task_id="t2")
    journal.record("workflow_phase_advanced", phase="review")
    journal.record("agent_reaped", agent_id="a1")
    journal.record("run_completed", ok=True)
    events = load_events(journal.path)

    batch = project_spans(events, run_id="run-multi")
    projector = IncrementalSpanProjector("run-multi")
    streamed = [span for row in events if (span := projector.observe(row)) is not None]
    assert streamed == batch.spans


# ---------------------------------------------------------------------------
# Live stream over the journal observer
# ---------------------------------------------------------------------------


def _live_stream(
    tmp_path, exporter: InMemorySpanExporter, run_id: str = _RUN_ID
) -> tuple[EventJournal, LiveJournalSpanStream]:
    journal = EventJournal(run_id, tmp_path / ".sdd")
    stream = LiveJournalSpanStream(
        bridge=_bridge(exporter),
        run_id=run_id,
        workdir=tmp_path,
        journal_path=journal.path,
    )
    journal.set_observer(stream.on_journal_append)
    return journal, stream


def test_live_stream_exports_same_spans_as_backfill(tmp_path) -> None:
    """AC: the live path and the offline projection are the same spans."""
    live_exporter = InMemorySpanExporter()
    journal, stream = _live_stream(tmp_path, live_exporter)
    journal.record("run_started", goal="ship")
    journal.record("agent_spawned", agent_id="a1")
    journal.record("task_claimed", task_id="t1")
    journal.record("tick_start", tick=1)
    journal.record("task_completed", task_id="t1")
    journal.record("agent_reaped", agent_id="a1")
    journal.record("run_completed", ok=True)

    events = load_events(journal.path)
    projection = project_spans(events, run_id=_RUN_ID)
    backfill_exporter = InMemorySpanExporter()
    _bridge(backfill_exporter).export_projection(projection, events)

    def key(s):
        return (
            s.name,
            s.context.trace_id,
            s.context.span_id,
            s.parent.span_id if s.parent else None,
            s.start_time,
            s.end_time,
            dict(s.attributes),
        )

    live = [key(s) for s in live_exporter.get_finished_spans()]
    offline = [key(s) for s in backfill_exporter.get_finished_spans()]
    assert live == offline
    assert stream.span_count == len(projection.spans)


def test_observer_failure_never_breaks_journal_append(tmp_path) -> None:
    """A crashing observer is swallowed; the chain keeps appending intact."""
    journal = EventJournal(_RUN_ID, tmp_path / ".sdd")

    def boom(_row: dict) -> None:
        raise RuntimeError("observer exploded")

    journal.set_observer(boom)
    journal.record("run_started", goal="ship")
    journal.record("task_claimed", task_id="t1")
    assert journal.event_count() == 2
    assert journal.verify().ok


def test_observer_delivery_preserves_concurrent_append_order(tmp_path: Path) -> None:
    """Concurrent writers still deliver observer rows in journal-index order."""
    journal = EventJournal(_RUN_ID, tmp_path / ".sdd")
    first_entered = threading.Event()
    release_first = threading.Event()
    seen: list[int] = []

    def observer(row: dict) -> None:
        if row["index"] == 0:
            first_entered.set()
            assert release_first.wait(timeout=2)
        seen.append(row["index"])

    journal.set_observer(observer)
    first = threading.Thread(target=lambda: journal.record("run_started"))
    second = threading.Thread(target=lambda: journal.record("agent_spawned"))
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert seen == [0, 1]
    assert journal.verify().ok


def test_live_stream_finalize_records_audit_event(tmp_path, monkeypatch) -> None:
    """finalize() anchors the live trace via the otel.projection audit event."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    monkeypatch.setenv("BERNSTEIN_CREDENTIAL_SIGNING_KEY", str(tmp_path / "install.key"))

    exporter = InMemorySpanExporter()
    journal, stream = _live_stream(tmp_path, exporter)
    journal.record("run_started", goal="ship")
    journal.record("run_completed", ok=True)
    stream.finalize()

    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import EVENT_OTEL_PROJECTION, AuditChainStore

    chain = AuditChainStore(tmp_path / ".sdd" / "audit", key=load_or_create_audit_key())
    otel_events = [e for e in chain.query() if e.event_type == EVENT_OTEL_PROJECTION]
    assert len(otel_events) == 1
    details = otel_events[0].details
    events = load_events(journal.path)
    projection = project_spans(events, run_id=_RUN_ID)
    assert details["trace_id"] == projection.trace_id
    assert details["run_id"] == _RUN_ID
    assert details["journal_head"] == events[-1]["event_hash"]
    assert details["span_count"] == len(projection.spans)


def test_finalize_is_idempotent(tmp_path, monkeypatch) -> None:
    """Double finalize records exactly one audit event."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    monkeypatch.setenv("BERNSTEIN_CREDENTIAL_SIGNING_KEY", str(tmp_path / "install.key"))
    exporter = InMemorySpanExporter()
    journal, stream = _live_stream(tmp_path, exporter)
    journal.record("run_started", goal="ship")
    stream.finalize()
    stream.finalize()

    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import EVENT_OTEL_PROJECTION, AuditChainStore

    chain = AuditChainStore(tmp_path / ".sdd" / "audit", key=load_or_create_audit_key())
    assert sum(1 for e in chain.query() if e.event_type == EVENT_OTEL_PROJECTION) == 1


# ---------------------------------------------------------------------------
# Default off: zero initialisation, zero network
# ---------------------------------------------------------------------------


def test_no_endpoint_builds_nothing(monkeypatch) -> None:
    """AC: with no endpoint configured the factory returns None pre-init."""
    monkeypatch.delenv(OTEL_ENDPOINT_ENV, raising=False)
    assert build_bridge_from_env({}) is None
    assert build_bridge_from_env() is None


def test_no_endpoint_attach_leaves_journal_untouched(tmp_path, monkeypatch) -> None:
    """attach_live_export is a no-op with no endpoint: no observer installed."""
    monkeypatch.delenv(OTEL_ENDPOINT_ENV, raising=False)
    journal = EventJournal(_RUN_ID, tmp_path / ".sdd")
    assert attach_live_export(journal, workdir=tmp_path) is None
    assert journal._observer is None


def test_no_endpoint_zero_exporter_init_and_zero_network(tmp_path, monkeypatch) -> None:
    """AC: default off means no exporter class construction and no sockets."""
    monkeypatch.delenv(OTEL_ENDPOINT_ENV, raising=False)

    def refuse_socket(*args, **kwargs):
        raise AssertionError("network attempt during default-off OTel wiring")

    def refuse_exporter_import(name, *args, **kwargs):
        raise AssertionError("OTLP exporter imported during default-off wiring")

    monkeypatch.setattr(socket, "socket", refuse_socket)
    monkeypatch.setattr(socket, "create_connection", refuse_socket)
    monkeypatch.setattr(
        JournalOTLPBridge,
        "_build_otlp_exporter",
        refuse_exporter_import,
    )

    journal = EventJournal(_RUN_ID, tmp_path / ".sdd")
    stream = attach_live_export(journal, workdir=tmp_path)
    assert stream is None
    journal.record("run_started", goal="ship")
    journal.record("run_completed", ok=True)


def test_live_env_can_disable_genai_stability(tmp_path, monkeypatch) -> None:
    """The live path honors the same GenAI stability boundary as backfill."""
    from bernstein.core.observability import otel_bridge

    exporter = InMemorySpanExporter()
    monkeypatch.setattr(otel_bridge, "build_bridge_from_env", lambda _env: _bridge(exporter))
    journal = EventJournal(_RUN_ID, tmp_path / ".sdd")
    stream = attach_live_export(
        journal,
        workdir=tmp_path,
        env={GENAI_STABILITY_ENV: "false"},
    )
    assert stream is not None
    journal.record("run_started", goal="ship")

    attributes = exporter.get_finished_spans()[0].attributes
    assert "gen_ai.operation.name" not in attributes
    assert "gen_ai.system" not in attributes
    assert attributes[ATTR_JOURNAL_ENTRY_HASH]


def test_real_otlp_exporter_serializes_to_in_memory_receiver(tmp_path) -> None:
    """The real OTLP exporter preserves explicit ids in its protobuf request."""
    trace_exporter = pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")
    trace_service = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")

    class Receiver:
        def __init__(self) -> None:
            self.requests = []

        def Export(self, request, **_kwargs):
            self.requests.append(request)
            return trace_service.ExportTraceServiceResponse()

    receiver = Receiver()
    exporter = trace_exporter.OTLPSpanExporter(endpoint="localhost:4317", insecure=True)
    exporter._client = receiver
    events = _events(tmp_path)
    projection = project_spans(events, run_id=_RUN_ID)
    bridge = JournalOTLPBridge(span_exporter=exporter, batch=False)
    assert bridge.export_projection(projection, events) == len(projection.spans)
    bridge.shutdown()

    spans = [
        span
        for request in receiver.requests
        for resource_spans in request.resource_spans
        for scope_spans in resource_spans.scope_spans
        for span in scope_spans.spans
    ]
    assert len(spans) == len(projection.spans)
    assert spans[0].trace_id.hex() == projection.trace_id
    assert spans[0].span_id.hex() == projection.spans[0].span_id
    assert {attribute.key for attribute in spans[0].attributes} >= {
        ATTR_JOURNAL_ENTRY_HASH,
        ATTR_AUDIT_ANCHOR,
        ATTR_RUN_ID,
    }


def test_missing_grpc_extra_disables_bridge(monkeypatch) -> None:
    """An endpoint with no grpc extra logs a warning and disables cleanly."""
    import builtins

    real_import = builtins.__import__

    def fail_grpc(name, *args, **kwargs):
        if "exporter.otlp" in name:
            raise ImportError("simulated missing extra")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_grpc)
    bridge = JournalOTLPBridge(OTLPExporterConfig(endpoint="http://collector:4317"), batch=False)
    assert not bridge.enabled
    assert bridge.export_batch([object()]) == 0


def test_live_processor_failure_never_falls_back_to_sync_export(monkeypatch) -> None:
    """Live wiring disables itself rather than doing gRPC on the append path."""
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(JournalOTLPBridge, "_build_processor", lambda _self, _exporter: None)
    bridge = JournalOTLPBridge(span_exporter=exporter, batch=True)
    assert not bridge.enabled


def test_export_batch_surfaces_exporter_failure_result(tmp_path) -> None:
    """A FAILURE result raises instead of being reported as a successful export.

    The OTLP exporter returns ``SpanExportResult.FAILURE`` (it does not
    raise) when the collector is unreachable, so a bridge that ignored the
    return value would count undelivered spans as exported.
    """
    from opentelemetry.sdk.trace.export import SpanExportResult

    from bernstein.core.observability.otel_bridge import OTLPExportError

    class _FailingExporter:
        def __init__(self) -> None:
            self.calls = 0

        def export(self, spans):
            self.calls += 1
            return SpanExportResult.FAILURE

        def shutdown(self) -> None:
            pass

    events = _events(tmp_path)
    projection = project_spans(events, run_id=_RUN_ID)
    exporter = _FailingExporter()
    bridge = JournalOTLPBridge(span_exporter=exporter, batch=False)

    with pytest.raises(OTLPExportError):
        bridge.export_projection(projection, events)
    assert exporter.calls == 1


def test_export_batch_returns_count_on_success_result(tmp_path) -> None:
    """A SUCCESS result returns the delivered span count as before."""
    from opentelemetry.sdk.trace.export import SpanExportResult

    class _OkExporter:
        def export(self, spans):
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            pass

    events = _events(tmp_path)
    projection = project_spans(events, run_id=_RUN_ID)
    bridge = JournalOTLPBridge(span_exporter=_OkExporter(), batch=False)
    assert bridge.export_projection(projection, events) == len(projection.spans)


# ---------------------------------------------------------------------------
# Determinism guards
# ---------------------------------------------------------------------------


def test_projected_ids_never_from_wall_clock(tmp_path) -> None:
    """Span ids depend only on entry hashes: identical payloads at different
    times yield identical ids (ts is outside the hash chain)."""
    events = _events(tmp_path, "run-a")
    shifted = [dict(row, ts=row["ts"] + 3600.0, elapsed_s=0.0) for row in events]
    original = project_spans(events, run_id="x")
    moved = project_spans(shifted, run_id="x")
    assert [s.span_id for s in original.spans] == [s.span_id for s in moved.spans]
    assert original.trace_id == moved.trace_id


def test_readable_spans_have_sampled_flag_and_kind(tmp_path) -> None:
    """Exported contexts are sampled (so processors forward them) and keep
    the projection's CLIENT/INTERNAL kind split."""
    from opentelemetry.trace import SpanKind

    events = _events(tmp_path)
    projection = project_spans(events, run_id=_RUN_ID)
    spans = projection_to_readable_spans(projection, events)
    for readable, projected in zip(spans, projection.spans, strict=True):
        assert readable.context.trace_flags.sampled
        expected = SpanKind.CLIENT if projected.operation in {"execute_tool", "chat"} else SpanKind.INTERNAL
        assert readable.kind is expected


@pytest.mark.parametrize("bad_ts", [None, "not-a-number"])
def test_missing_ts_degrades_to_zero_not_crash(tmp_path, bad_ts) -> None:
    """A malformed ts never crashes the wire path; it degrades to epoch 0."""
    events = _events(tmp_path)
    events[0]["ts"] = bad_ts
    projection = project_spans(events, run_id=_RUN_ID)
    exporter = InMemorySpanExporter()
    _bridge(exporter).export_projection(projection, events)
    assert exporter.get_finished_spans()[0].start_time == 0
