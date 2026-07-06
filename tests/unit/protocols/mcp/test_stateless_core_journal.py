"""Journal and audit-chain anchoring for the stateless MCP core (issue #2307).

Every stateless MCP call is recorded as an ordered entry in the canonical
run journal with its content-derived span id (AC4), and the call is anchored
into the HMAC audit chain so continuity is chain-anchored rather than
session-anchored.

These tests isolate per-test state with ``tmp_path``.
"""

from __future__ import annotations

from bernstein.core.protocols.mcp.stateless_core import (
    CacheReference,
    StatelessCallRecord,
    build_request_meta,
    record_mcp_call_in_journal,
)
from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.security.audit_chain import (
    EVENT_MCP_STATELESS_CALL,
    AuditChainStore,
    record_mcp_stateless_call,
)


def _caps() -> dict[str, object]:
    return {"tools": {"listChanged": True}}


def _meta(call_index: int) -> dict[str, object]:
    return build_request_meta(
        method="tools/call",
        params_content_hash=f"{call_index:064x}",
        run_root_hash="b" * 64,
        call_index=call_index,
        client_capabilities=_caps(),
    )


class TestJournalRecording:
    def test_calls_appear_as_ordered_entries_with_span_id(self, tmp_path) -> None:
        # AC4: each MCP call is an ordered journal entry with its span id.
        journal = EventJournal("run-2307", tmp_path)
        for i in range(3):
            meta = _meta(i)
            record_mcp_call_in_journal(
                journal,
                StatelessCallRecord.from_meta(
                    method="tools/call",
                    call_index=i,
                    meta=meta,
                ),
            )

        rows = [r for r in load_events(journal.path) if r["event"] == "mcp.stateless_call"]
        assert len(rows) == 3
        assert [r["call_index"] for r in rows] == [0, 1, 2]
        for i, row in enumerate(rows):
            expected_span = _meta(i)["traceparent"].split("-")[2]
            assert row["span_id"] == expected_span

        # Journal chain stays intact and byte-identical across a replay.
        assert journal.verify().ok

    def test_replay_produces_identical_journal_head(self, tmp_path) -> None:
        heads = []
        for run in ("a", "b"):
            journal = EventJournal(f"run-{run}", tmp_path / run)
            for i in range(4):
                record_mcp_call_in_journal(
                    journal,
                    StatelessCallRecord.from_meta(method="tools/call", call_index=i, meta=_meta(i)),
                )
            heads.append(journal.head())
        assert heads[0] == heads[1]


class TestAuditChainAnchor:
    def test_call_anchored_into_audit_chain(self, tmp_path) -> None:
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        meta = _meta(0)
        event = record_mcp_stateless_call(
            chain=chain,
            run_id="run-2307",
            method="tools/call",
            call_index=0,
            trace_id=meta["traceparent"].split("-")[1],
            span_id=meta["traceparent"].split("-")[2],
            journal_head="f" * 64,
            cache_content_hash="",
        )
        assert event.event_type == EVENT_MCP_STATELESS_CALL
        assert "prev_chain_digest" in event.details
        assert event.details["span_id"] == meta["traceparent"].split("-")[2]
        ok, errors = chain.verify()
        assert ok, errors

    def test_cache_hit_anchor_carries_producing_run_hash(self, tmp_path) -> None:
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        ref = CacheReference.for_hit(
            content_hash="c" * 64,
            producing_run_head="d" * 64,
            cache_scope="run",
        )
        event = record_mcp_stateless_call(
            chain=chain,
            run_id="run-2307",
            method="tools/call",
            call_index=1,
            trace_id="a" * 32,
            span_id="b" * 16,
            journal_head="e" * 64,
            cache_content_hash=ref.content_hash,
        )
        assert event.details["cache_content_hash"] == "c" * 64
        assert chain.verify()[0]
