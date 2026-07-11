"""MCP Tasks extension: verifiable long-running run handles (issue #2364).

The Tasks extension turns a long-running orchestration run into a handle a
stateless MCP client can poll. The handle is not free-standing state: its
status is a pure projection of the run journal, and it embeds the run's
audit-chain head so a client can later verify the task it watched
corresponds to the audited run. These tests isolate state with ``tmp_path``
and never depend on wall-clock ordering.
"""

from __future__ import annotations

from bernstein.core.protocols.mcp.tasks_extension import (
    SPEC_REVISION,
    TASK_CANCELLED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_INPUT_REQUIRED,
    TASK_WORKING,
    RunHandle,
    TraceContext,
    decode_poll_token,
    ingest_trace_context,
    poll_task_handle,
    project_task_status,
    record_trace_context_into_lineage,
    verify_handle,
    verify_handle_chain_head,
)
from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.security.audit_chain import AuditChainStore


def _events(*event_types: str) -> list[dict[str, object]]:
    return [{"event": e} for e in event_types]


class TestStatusProjection:
    def test_default_status_is_working(self) -> None:
        assert project_task_status([]) == TASK_WORKING

    def test_lifecycle_events_project_to_status(self) -> None:
        assert project_task_status(_events("run_started", "task_claimed")) == TASK_WORKING
        assert project_task_status(_events("run_started", "input_required")) == TASK_INPUT_REQUIRED
        assert project_task_status(_events("run_started", "run_completed")) == TASK_COMPLETED
        assert project_task_status(_events("run_started", "run_failed")) == TASK_FAILED
        assert project_task_status(_events("run_started", "run_cancelled")) == TASK_CANCELLED

    def test_terminal_status_is_sticky(self) -> None:
        # A stray progress event after completion must not downgrade the
        # projected status: the fold is monotone at the terminal boundary.
        events = _events("run_started", "run_completed", "task_progress")
        assert project_task_status(events) == TASK_COMPLETED

    def test_projection_is_deterministic(self) -> None:
        events = _events("run_started", "task_claimed", "input_required", "run_completed")
        assert project_task_status(events) == project_task_status(list(events))


class TestRunHandleReceipt:
    def test_receipt_hash_is_deterministic(self) -> None:
        h1 = RunHandle(
            task_id="t1",
            run_id="run-2364",
            status=TASK_WORKING,
            journal_head="a" * 64,
            chain_head="b" * 64,
        )
        h2 = RunHandle(
            task_id="t1",
            run_id="run-2364",
            status=TASK_WORKING,
            journal_head="a" * 64,
            chain_head="b" * 64,
        )
        assert h1.receipt_hash == h2.receipt_hash
        assert h1.spec_revision == SPEC_REVISION

    def test_receipt_hash_changes_with_status(self) -> None:
        base = {
            "task_id": "t1",
            "run_id": "run-2364",
            "journal_head": "a" * 64,
            "chain_head": "b" * 64,
        }
        working = RunHandle(status=TASK_WORKING, **base)
        done = RunHandle(status=TASK_COMPLETED, **base)
        assert working.receipt_hash != done.receipt_hash

    def test_wire_roundtrip_carries_chain_head_and_token(self) -> None:
        handle = RunHandle(
            task_id="t1",
            run_id="run-2364",
            status=TASK_COMPLETED,
            journal_head="a" * 64,
            chain_head="b" * 64,
            trace_id="c" * 32,
        )
        wire = handle.to_wire()
        assert wire["chainHead"] == "b" * 64
        assert wire["specRevision"] == SPEC_REVISION
        assert wire["receiptHash"] == handle.receipt_hash
        token = wire["pollToken"]
        decoded = decode_poll_token(token)
        assert decoded["run_id"] == "run-2364"
        assert decoded["task_id"] == "t1"


class TestHandleFromJournal:
    def test_handle_projects_status_and_head_from_journal(self, tmp_path) -> None:
        journal = EventJournal("run-2364", tmp_path)
        journal.record("run_started", goal="do a thing")
        journal.record("task_claimed", role="backend")
        handle = RunHandle.from_journal(
            task_id="t1",
            run_id="run-2364",
            events=load_events(journal.path),
            chain_head="b" * 64,
        )
        assert handle.status == TASK_WORKING
        assert handle.journal_head == journal.head()

    def test_verify_handle_detects_forged_status(self, tmp_path) -> None:
        journal = EventJournal("run-2364", tmp_path)
        journal.record("run_started")
        events = load_events(journal.path)
        good = RunHandle.from_journal(task_id="t1", run_id="run-2364", events=events, chain_head="b" * 64)
        ok, reason = verify_handle(good, events)
        assert ok, reason
        # A client claiming completion while the journal shows only "working"
        # must fail verification: the progress claim is not a faithful
        # projection of the journal.
        forged = RunHandle(
            task_id="t1",
            run_id="run-2364",
            status=TASK_COMPLETED,
            journal_head=good.journal_head,
            chain_head="b" * 64,
        )
        ok2, reason2 = verify_handle(forged, events)
        assert not ok2
        assert reason2


class TestChainHeadVerification:
    def test_embedded_chain_head_verifies_against_audit_chain(self, tmp_path) -> None:
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        chain.log(event_type="run.step", actor="x", resource_type="r", resource_id="1", details={})
        head_at_completion = chain.prev_chain_digest
        handle = RunHandle(
            task_id="t1",
            run_id="run-2364",
            status=TASK_COMPLETED,
            journal_head="a" * 64,
            chain_head=head_at_completion,
        )
        ok, reason = verify_handle_chain_head(handle, chain)
        assert ok, reason

    def test_tampered_chain_head_fails(self, tmp_path) -> None:
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        chain.log(event_type="run.step", actor="x", resource_type="r", resource_id="1", details={})
        handle = RunHandle(
            task_id="t1",
            run_id="run-2364",
            status=TASK_COMPLETED,
            journal_head="a" * 64,
            chain_head="f" * 64,
        )
        ok, reason = verify_handle_chain_head(handle, chain)
        assert not ok
        assert reason


class TestTraceContextIngestion:
    def test_ingest_parses_w3c_traceparent(self) -> None:
        meta = {
            "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
            "tracestate": "vendor=1",
            "baggage": "k=v",
        }
        trace = ingest_trace_context(meta)
        assert trace is not None
        assert trace.trace_id == "a" * 32
        assert trace.parent_id == "b" * 16
        assert trace.tracestate == "vendor=1"

    def test_ingest_rejects_malformed_and_absent(self) -> None:
        assert ingest_trace_context({}) is None
        assert ingest_trace_context({"traceparent": "garbage"}) is None
        assert ingest_trace_context({"traceparent": "00-" + "0" * 32 + "-" + "b" * 16 + "-01"}) is None

    def test_trace_context_appears_in_artefact_lineage(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("BERNSTEIN_LINEAGE_ENABLED", "1")
        lineage_root = tmp_path / "lineage"
        trace = TraceContext(
            trace_id="a" * 32,
            parent_id="b" * 16,
            trace_flags="01",
            tracestate="vendor=1",
            baggage="k=v",
        )
        entry_hash = record_trace_context_into_lineage(
            trace=trace,
            artifact_path="src/out.py",
            content=b"print('hi')\n",
            actor="mcp_task_handle",
            run_id="run-2364",
            lineage_root=lineage_root,
            hmac_key=b"k" * 32,
            timestamp=1,
        )
        assert entry_hash
        from bernstein.core.lineage.spine import LineageSpine

        spine = LineageSpine(lineage_root, run_id="run-2364", hmac_key=b"k" * 32)
        rows = [e for e in spine.iter_entries() if e.artifact_path == "src/out.py"]
        assert rows
        # The calling host's traceparent must be discoverable in the lineage
        # of the artefact the run produced (AC3).
        assert trace.traceparent in rows[0].step_id
        assert spine.verify().ok


class TestPollingFallbackInterop:
    def test_reference_client_polls_without_session(self, tmp_path) -> None:
        # AC1/AC4: a minimal reference client that holds no session drives a
        # run purely from the poll token and the on-disk journal. It re-reads
        # the journal each poll and reprojects the handle; a different server
        # instance would answer identically because the projection is pure.
        journal = EventJournal("run-2364", tmp_path)
        journal.record("run_started", goal="g")
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)

        first = RunHandle.from_journal(
            task_id="t1",
            run_id="run-2364",
            events=load_events(journal.path),
            chain_head=chain.prev_chain_digest,
        )
        token = first.to_wire()["pollToken"]

        # Client polls: still working.
        polled = poll_task_handle(token, events=load_events(journal.path), chain_head=chain.prev_chain_digest)
        assert polled.status == TASK_WORKING
        assert polled.run_id == "run-2364"

        # Run advances to completion; the client polls again from the token.
        journal.record("run_completed", result="done")
        chain.log(event_type="run.complete", actor="x", resource_type="r", resource_id="1", details={})
        done = poll_task_handle(token, events=load_events(journal.path), chain_head=chain.prev_chain_digest)
        assert done.status == TASK_COMPLETED
        # The completed handle embeds the final chain head and verifies.
        ok, reason = verify_handle_chain_head(done, chain)
        assert ok, reason
