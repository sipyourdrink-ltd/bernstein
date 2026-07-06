"""Audit-chain mirror for typed activity results (issue #2311).

Each activity boundary crossing is anchored into the run event journal and,
when an audit chain is supplied, mirrored into the HMAC-chained audit log so an
operator can prove -- from the chain alone -- that a modality-agnostic activity
ran under the deterministic scheduler with a given evidence set, without the
record ever exposing the artifact body or the fetched pages. Only hashes, the
kind, the terminal state, and the reason code are recorded.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.orchestration.activity import (
    ActivityKind,
    Observation,
    TerminalState,
    dispatch_activity,
)
from bernstein.core.replay.journal import EventJournal
from bernstein.core.security.audit_chain import (
    EVENT_ACTIVITY_RESULT,
    AuditChainStore,
    record_activity_result,
)


def _journal(tmp_path: Path, run_id: str = "run-1") -> EventJournal:
    return EventJournal(run_id=run_id, sdd_dir=tmp_path / ".sdd")


def _result() -> object:
    from bernstein.core.orchestration.activity import ActivityResult

    return ActivityResult.build(
        kind=ActivityKind.RESEARCH,
        artifact={"summary": "s"},
        observations=(Observation.of(kind="page", ref="https://a", content=b"alpha"),),
        terminal_state=TerminalState.COMPLETED,
        reason_code="ok",
    )


def test_record_activity_result_appends_event(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    event = record_activity_result(
        chain=chain,
        run_id="run-1",
        stage_id="stage-0",
        kind="research",
        artifact_hash="sha256:aa",
        evidence_set_hash="sha256:bb",
        terminal_state="completed",
        reason_code="ok",
        journal_index=0,
        journal_event_hash="sha256:ee",
    )
    assert event.event_type == EVENT_ACTIVITY_RESULT
    rows = chain.query(event_type=EVENT_ACTIVITY_RESULT)
    assert len(rows) == 1
    details = rows[0].details
    assert details["kind"] == "research"
    assert details["artifact_hash"] == "sha256:aa"
    assert details["evidence_set_hash"] == "sha256:bb"
    assert details["terminal_state"] == "completed"
    assert details["reason_code"] == "ok"
    assert details["stage_id"] == "stage-0"
    assert details["journal_index"] == 0
    assert details["journal_event_hash"] == "sha256:ee"
    assert "prev_chain_digest" in details


def test_record_activity_result_never_carries_artifact_body(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_activity_result(
        chain=chain,
        run_id="run-1",
        stage_id="s0",
        kind="browser",
        artifact_hash="sha256:aa",
        evidence_set_hash="sha256:bb",
        terminal_state="completed",
        reason_code="ok",
        journal_index=0,
        journal_event_hash="sha256:ee",
    )
    rows = chain.query(event_type=EVENT_ACTIVITY_RESULT)
    blob = repr(rows[0].details)
    # The record carries only hashes, never a body / page content.
    assert "alpha" not in blob
    assert "summary" not in blob


def test_dispatch_mirrors_into_audit_chain(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    result = _result()
    dispatch_activity(result, stage_id="stage-0", journal=journal, chain=chain)

    rows = chain.query(event_type=EVENT_ACTIVITY_RESULT)
    assert len(rows) == 1
    assert rows[0].details["kind"] == "research"
    assert rows[0].details["stage_id"] == "stage-0"
    # The mirrored journal event hash matches the journal head.
    assert rows[0].details["journal_event_hash"] == journal.head()


def test_audit_chain_stays_verifiable_after_mirror(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    dispatch_activity(_result(), stage_id="s0", journal=journal, chain=chain)
    ok, errors = chain.verify()
    assert ok, errors
