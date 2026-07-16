"""ACP lifecycle conformance + the parser-drift regression (#2522).

For adapters that speak ACP, lifecycle conformance is an event-schema
fixture (a recorded JSON-RPC frame sequence) rather than a pinned stdout
golden transcript. The regression test below is the whole point of the
feature: an upstream output-format change breaks a text-signals fixture
but leaves the ACP-channel path green, because the ACP path reasons about
structured frames and content hashes, never fragile text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.adapters.conformance import (
    check_terminal_signal,
    load_acp_event_fixture,
    replay_acp_event_fixture,
)
from bernstein.core.protocols.acp.client import (
    ACPEventJournalSink,
    compare_acp_journals,
    drive_acp_lifecycle,
)
from bernstein.core.replay.journal import EventJournal

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "acp" / "lifecycle"


@pytest.mark.integration
@pytest.mark.parametrize(
    "fixture",
    sorted(FIXTURE_DIR.glob("*.jsonl")),
    ids=lambda p: p.stem,
)
def test_acp_lifecycle_fixture_validates_and_reaches_terminal(fixture: Path, tmp_path: Path) -> None:
    """Every shipped ACP lifecycle fixture validates and ends in a terminal."""
    result = replay_acp_event_fixture(fixture, sdd_dir=tmp_path)
    assert result.terminal is True, f"{fixture.name} never reached a terminal ACP event"
    assert result.ok is True
    assert result.event_count > 0
    assert result.journal_head


# ---------------------------------------------------------------------------
# The parser-drift regression: text breaks, ACP stays green
# ---------------------------------------------------------------------------


def _acp_frames(*, completion_text: str) -> list[bytes]:
    """A structured ACP lifecycle whose human text differs but stopReason does not."""
    frames = [
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-04-01"}},
        {"jsonrpc": "2.0", "method": "streamUpdate", "params": {"sessionId": "s", "delta": {"text": completion_text}}},
        {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}},
    ]
    return [(json.dumps(f) + "\n").encode("utf-8") for f in frames]


def test_upstream_format_change_breaks_text_but_not_acp(tmp_path: Path) -> None:
    # --- Text-signals path -------------------------------------------------
    # v1: the CLI emits the canonical terminal signal -> text path is green.
    text_v1 = ["thinking...", 'BERNSTEIN:COMPLETED {"ok": true}']
    assert check_terminal_signal(text_v1, run_id="text-v1") is None

    # v2: an upstream release changes the completion line format. The bespoke
    # text parser drifts: no terminal signal is found -> the fixture breaks.
    text_v2 = ["thinking...", ">>> task complete <<<"]
    assert check_terminal_signal(text_v2, run_id="text-v2") is not None

    # --- ACP-channel path --------------------------------------------------
    # The same upstream release changed only the human-readable delta text;
    # the structured stopReason is unchanged, so both versions reach terminal.
    j1 = EventJournal("acp-v1", tmp_path)
    r1 = drive_acp_lifecycle(_acp_frames(completion_text="done"), ACPEventJournalSink(j1))
    j2 = EventJournal("acp-v2", tmp_path)
    r2 = drive_acp_lifecycle(_acp_frames(completion_text="all finished, patch applied"), ACPEventJournalSink(j2))

    assert r1.terminal is True and r1.stop_reason == "end_turn"
    assert r2.terminal is True and r2.stop_reason == "end_turn"


def test_divergence_detection_across_replays(tmp_path: Path) -> None:
    frames = _acp_frames(completion_text="done")

    recorded = EventJournal("rec", tmp_path)
    drive_acp_lifecycle(frames, ACPEventJournalSink(recorded))

    # A faithful replay diverges nowhere.
    faithful = EventJournal("faithful", tmp_path)
    drive_acp_lifecycle(frames, ACPEventJournalSink(faithful))
    assert compare_acp_journals(recorded.path, faithful.path) is None

    # A tampered replay is named at the exact step.
    tampered_frames = _acp_frames(completion_text="INJECTED")
    tampered = EventJournal("tampered", tmp_path)
    drive_acp_lifecycle(tampered_frames, ACPEventJournalSink(tampered))
    divergence = compare_acp_journals(recorded.path, tampered.path)
    assert divergence is not None
    assert divergence.seq == 1
    assert divergence.method == "streamUpdate"


def test_load_acp_event_fixture_reads_frames() -> None:
    fixture = FIXTURE_DIR / "simple_task.jsonl"
    lines = load_acp_event_fixture(fixture)
    assert lines
    assert all(isinstance(line, bytes) for line in lines)
