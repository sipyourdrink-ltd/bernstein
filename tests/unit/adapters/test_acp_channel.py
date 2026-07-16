"""ACP as a first-class adapter event transport (#2522).

Adapters that declare ``EventChannel.ACP`` complete a spawn-to-terminal
lifecycle entirely over structured JSON-RPC frames: no ``BERNSTEIN:`` text
grammar, no per-adapter stdout parser. These tests pin the adapter-side
binding and the strategy-matrix declarations for the migrated adapters.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.adapters._contract import EventChannel, strategy_for
from bernstein.adapters.acp_channel import (
    adapter_speaks_acp,
    run_acp_channel,
)
from bernstein.core.protocols.acp.schema import ACPSchemaError
from bernstein.core.replay.journal import EventJournal, load_events

_SESSION: list[dict] = [
    {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-04-01"}},
    {"jsonrpc": "2.0", "method": "streamUpdate", "params": {"sessionId": "s", "delta": {"text": "hi"}}},
    {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}},
]


def _lines(frames: list[dict]) -> list[bytes]:
    return [(json.dumps(f) + "\n").encode("utf-8") for f in frames]


# ---------------------------------------------------------------------------
# Strategy-matrix declarations for the migrated adapters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["kilo", "goose"])
def test_migrated_adapters_declare_acp_channel(name: str) -> None:
    assert strategy_for(name).event_channel is EventChannel.ACP


@pytest.mark.parametrize("name", ["kilo", "goose"])
def test_adapter_speaks_acp_true_for_migrated(name: str) -> None:
    assert adapter_speaks_acp(name) is True


def test_adapter_speaks_acp_false_for_text_signal_adapter() -> None:
    assert adapter_speaks_acp("aider") is False


# ---------------------------------------------------------------------------
# Spawn-to-terminal lifecycle with zero stdout/stderr lifecycle parsing
# ---------------------------------------------------------------------------


def test_run_acp_channel_reaches_terminal_without_text_parsing(tmp_path: Path) -> None:
    journal = EventJournal("kilo-run", tmp_path)
    result = run_acp_channel(_lines(_SESSION), journal=journal, session_id="kilo-run")

    assert result.ok is True
    assert result.terminal is True
    assert result.stop_reason == "end_turn"
    assert result.event_count == len(_SESSION)
    assert result.journal_head == journal.head()
    assert journal.verify().ok

    # Lifecycle was driven purely from structured frames: no BERNSTEIN: text
    # signal appears anywhere in the recorded session.
    assert not any(b"BERNSTEIN:" in line for line in _lines(_SESSION))


def test_run_acp_channel_stops_at_first_terminal(tmp_path: Path) -> None:
    trailing = [*_SESSION, {"jsonrpc": "2.0", "method": "streamUpdate", "params": {"sessionId": "s", "delta": "late"}}]
    journal = EventJournal("kilo-run-2", tmp_path)
    result = run_acp_channel(_lines(trailing), journal=journal, session_id="kilo-run-2")
    # The terminal prompt response ends the turn; the trailing frame is not journaled.
    assert result.event_count == len(_SESSION)


def test_malformed_frame_mid_lifecycle_is_refused(tmp_path: Path) -> None:
    corrupt = [
        _SESSION[0],
    ]
    lines = [*_lines(corrupt), b"{ this is not valid json\n"]
    journal = EventJournal("kilo-bad", tmp_path)
    with pytest.raises(ACPSchemaError):
        run_acp_channel(lines, journal=journal, session_id="kilo-bad")
    # The valid prefix is journaled; the malformed frame wrote nothing.
    rows = [r for r in load_events(journal.path) if r.get("event") == "acp_event"]
    assert len(rows) == 1
    assert journal.verify().ok
