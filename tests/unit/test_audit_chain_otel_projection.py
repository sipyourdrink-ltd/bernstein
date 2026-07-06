"""Tests for the additive ``otel.projection`` audit-chain helper (issue #2300).

Each signed OTel span set projected from a run's event journal is mirrored
into the HMAC-chained audit log so an operator can confirm, from the chain
alone, that a trace was projected from a named journal head. The event
records only hashes and identifiers -- never the span attribute payloads --
and embeds the previous chain digest so the record is chained.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_OTEL_PROJECTION,
    AuditChainStore,
    record_otel_projection,
)


def _store(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def test_record_otel_projection_appends_chained_event(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    event = record_otel_projection(
        chain=chain,
        run_id="run-1",
        journal_head="ab" * 32,
        trace_id="cd" * 16,
        span_count=6,
        projection_sha256="ef" * 32,
    )
    assert event.event_type == EVENT_OTEL_PROJECTION
    assert event.resource_id == "cd" * 16
    assert "prev_chain_digest" in event.details
    assert event.details["run_id"] == "run-1"
    assert event.details["journal_head"] == "ab" * 32
    assert event.details["span_count"] == 6
    assert event.details["projection_sha256"] == "ef" * 32
    ok, errors = chain.verify()
    assert ok, errors
