"""Client-side ACP transport: content-addressed event journaling (#2522).

Bernstein historically only *served* ACP to IDEs. These tests pin the
inverse direction: Bernstein consuming ACP from an upstream CLI as a
first-class adapter event transport. Every inbound frame is validated at
the schema boundary and journaled content-addressed, so agent output is
replay-stable across upstream CLI output-format changes.

The killer property under test: the content-addressed event journal makes
output replay-stable. Strip the journal and this is just a parser swap -
so the divergence-by-content-hash tests below are the load-bearing ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.protocols.acp.client import (
    ACP_EVENT_TYPE,
    ACPEventJournalSink,
    canonical_frame_bytes,
    compare_acp_journals,
    drive_acp_lifecycle,
    frame_content_hash,
    is_terminal_frame,
    parse_inbound_frame,
    replay_acp_content_hashes,
)
from bernstein.core.protocols.acp.schema import ACPSchemaError
from bernstein.core.replay.journal import EventJournal, load_events

# ---------------------------------------------------------------------------
# A recorded upstream ACP session (agent -> client direction).
# ---------------------------------------------------------------------------

_INITIALIZE_RESPONSE = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {"protocolVersion": "2025-04-01", "agentCapabilities": {}},
}
_STREAM_UPDATE_1 = {
    "jsonrpc": "2.0",
    "method": "streamUpdate",
    "params": {"sessionId": "s-1", "delta": {"text": "reading files"}},
}
_STREAM_UPDATE_2 = {
    "jsonrpc": "2.0",
    "method": "streamUpdate",
    "params": {"sessionId": "s-1", "delta": {"text": "writing patch"}},
}
_PROMPT_RESPONSE_TERMINAL = {
    "jsonrpc": "2.0",
    "id": 2,
    "result": {"stopReason": "end_turn"},
}

_RECORDED_SESSION: list[dict] = [
    _INITIALIZE_RESPONSE,
    _STREAM_UPDATE_1,
    _STREAM_UPDATE_2,
    _PROMPT_RESPONSE_TERMINAL,
]


def _lines(frames: list[dict]) -> list[bytes]:
    return [(json.dumps(f) + "\n").encode("utf-8") for f in frames]


# ---------------------------------------------------------------------------
# Content addressing
# ---------------------------------------------------------------------------


def test_content_hash_is_canonical_and_key_order_independent() -> None:
    a = {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}}
    b = {"result": {"stopReason": "end_turn"}, "id": 2, "jsonrpc": "2.0"}
    assert frame_content_hash(a) == frame_content_hash(b)
    # Canonical bytes are sorted + compact, so byte identity holds too.
    assert canonical_frame_bytes(a) == canonical_frame_bytes(b)


def test_content_hash_changes_when_payload_changes() -> None:
    a = {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}}
    b = {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "max_tokens"}}
    assert frame_content_hash(a) != frame_content_hash(b)


# ---------------------------------------------------------------------------
# Schema-boundary validation (reuses validate_request / validate_response)
# ---------------------------------------------------------------------------


def test_parse_inbound_streamupdate_is_non_terminal_event() -> None:
    event = parse_inbound_frame(json.dumps(_STREAM_UPDATE_1), seq=0)
    assert event.method == "streamUpdate"
    assert event.terminal is False
    assert event.content_hash == frame_content_hash(_STREAM_UPDATE_1)


def test_parse_inbound_prompt_response_is_terminal() -> None:
    event = parse_inbound_frame(json.dumps(_PROMPT_RESPONSE_TERMINAL), seq=3)
    assert event.terminal is True
    assert event.stop_reason == "end_turn"


def test_parse_inbound_error_response_is_terminal() -> None:
    frame = {"jsonrpc": "2.0", "id": 2, "error": {"code": -32001, "message": "boom"}}
    event = parse_inbound_frame(json.dumps(frame), seq=1)
    assert event.terminal is True
    assert event.stop_reason == "error"


def test_is_terminal_frame_helper() -> None:
    assert is_terminal_frame(_PROMPT_RESPONSE_TERMINAL) == (True, "end_turn")
    assert is_terminal_frame(_STREAM_UPDATE_1) == (False, "")


@pytest.mark.parametrize(
    "raw",
    [
        b"not json at all\n",
        json.dumps({"jsonrpc": "1.0", "method": "streamUpdate", "params": {}}).encode(),
        json.dumps({"jsonrpc": "2.0", "method": "streamUpdate", "params": {"delta": "x"}}).encode(),
        json.dumps({"jsonrpc": "2.0", "id": 5}).encode(),  # neither request nor response
    ],
)
def test_malformed_frame_refused_at_schema_boundary(raw: bytes) -> None:
    with pytest.raises(ACPSchemaError):
        parse_inbound_frame(raw, seq=0)


# ---------------------------------------------------------------------------
# Journaling: content-addressed, no partial state on malformed frames
# ---------------------------------------------------------------------------


def test_malformed_frame_produces_no_journal_row(tmp_path: Path) -> None:
    journal = EventJournal("run-malformed", tmp_path)
    sink = ACPEventJournalSink(journal)
    # One valid frame lands, then a malformed frame is refused.
    sink.record(parse_inbound_frame(json.dumps(_STREAM_UPDATE_1), seq=0))
    with pytest.raises(ACPSchemaError):
        sink.record(parse_inbound_frame(b"{bad json", seq=1))
    rows = [r for r in load_events(journal.path) if r.get("event") == ACP_EVENT_TYPE]
    # Exactly the single valid event; the malformed frame wrote nothing.
    assert len(rows) == 1
    assert journal.verify().ok


def test_drive_lifecycle_journals_every_event_content_addressed(tmp_path: Path) -> None:
    journal = EventJournal("run-lifecycle", tmp_path)
    sink = ACPEventJournalSink(journal)
    result = drive_acp_lifecycle(_lines(_RECORDED_SESSION), sink)

    assert result.ok is True
    assert result.terminal is True
    assert result.stop_reason == "end_turn"
    assert result.event_count == len(_RECORDED_SESSION)
    assert result.journal_head == journal.head()
    assert journal.verify().ok

    rows = [r for r in load_events(journal.path) if r.get("event") == ACP_EVENT_TYPE]
    assert len(rows) == len(_RECORDED_SESSION)
    for row, frame in zip(rows, _RECORDED_SESSION, strict=True):
        assert row["content_hash"] == frame_content_hash(frame)


# ---------------------------------------------------------------------------
# Determinism: byte-identical replay + divergence naming the step
# ---------------------------------------------------------------------------


def test_replay_yields_byte_identical_journal_hashes(tmp_path: Path) -> None:
    j1 = EventJournal("run-a", tmp_path)
    drive_acp_lifecycle(_lines(_RECORDED_SESSION), ACPEventJournalSink(j1))

    j2 = EventJournal("run-b", tmp_path)
    drive_acp_lifecycle(_lines(_RECORDED_SESSION), ACPEventJournalSink(j2))

    # Same recorded session -> same content hashes per step -> same Merkle head.
    assert replay_acp_content_hashes(j1.path) == replay_acp_content_hashes(j2.path)
    assert j1.head() == j2.head()
    # No divergence between the two faithful replays.
    assert compare_acp_journals(j1.path, j2.path) is None


def test_mutated_recorded_event_reported_as_divergence_naming_step(tmp_path: Path) -> None:
    recorded = EventJournal("run-recorded", tmp_path)
    drive_acp_lifecycle(_lines(_RECORDED_SESSION), ACPEventJournalSink(recorded))

    # Upstream mutates one event's payload (index 2: second streamUpdate).
    mutated_session = [dict(f) for f in _RECORDED_SESSION]
    mutated_session[2] = {
        "jsonrpc": "2.0",
        "method": "streamUpdate",
        "params": {"sessionId": "s-1", "delta": {"text": "TAMPERED"}},
    }
    replayed = EventJournal("run-replayed", tmp_path)
    drive_acp_lifecycle(_lines(mutated_session), ACPEventJournalSink(replayed))

    divergence = compare_acp_journals(recorded.path, replayed.path)
    assert divergence is not None
    assert divergence.seq == 2
    assert divergence.method == "streamUpdate"
    assert divergence.expected_hash != divergence.actual_hash
    # The content hash is what caught it - re-parsing text could not.
    assert divergence.expected_hash == frame_content_hash(_RECORDED_SESSION[2])
