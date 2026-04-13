"""Tests for remote bridge lineage — T549, T550, T551."""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.session import (
    BridgeRebuildReason,
    BridgeTransportEvent,
    load_bridge_lineage,
    record_bridge_event,
)


class TestBridgeTransportEvent:
    def test_to_dict_roundtrip(self) -> None:
        evt = BridgeTransportEvent(
            session_id="abc123",
            event_type="rebuild",
            reason=BridgeRebuildReason.CREDENTIAL_REFRESH,
            remote_url="https://remote.example.com",
            credential_expiry=9999999.0,
            gap_seconds=2.5,
        )
        d = evt.to_dict()
        assert d["session_id"] == "abc123"
        assert d["event_type"] == "rebuild"
        assert d["reason"] == "credential_refresh"
        assert d["credential_expiry"] == pytest.approx(9999999.0)
        assert d["gap_seconds"] == pytest.approx(2.5)


class TestRecordAndLoadBridgeLineage:
    def test_record_creates_file(self, tmp_path: Path) -> None:
        evt = BridgeTransportEvent(session_id="s1", event_type="connect")
        record_bridge_event(tmp_path, evt)
        lineage_path = tmp_path / ".sdd" / "runtime" / "bridge_lineage.jsonl"
        assert lineage_path.exists()

    def test_load_returns_empty_when_no_file(self, tmp_path: Path) -> None:
        events = load_bridge_lineage(tmp_path)
        assert events == []

    def test_load_returns_recorded_events(self, tmp_path: Path) -> None:
        evt1 = BridgeTransportEvent(session_id="s1", event_type="connect")
        evt2 = BridgeTransportEvent(
            session_id="s1",
            event_type="rebuild",
            reason=BridgeRebuildReason.TIMEOUT,
            gap_seconds=5.0,
        )
        record_bridge_event(tmp_path, evt1)
        record_bridge_event(tmp_path, evt2)
        events = load_bridge_lineage(tmp_path)
        assert len(events) == 2
        assert events[1].reason == "timeout"
        assert events[1].gap_seconds == pytest.approx(5.0)

    def test_filter_by_session_id(self, tmp_path: Path) -> None:
        record_bridge_event(tmp_path, BridgeTransportEvent(session_id="s1", event_type="connect"))
        record_bridge_event(tmp_path, BridgeTransportEvent(session_id="s2", event_type="connect"))
        events = load_bridge_lineage(tmp_path, session_id="s1")
        assert len(events) == 1
        assert events[0].session_id == "s1"

    def test_credential_refresh_event(self, tmp_path: Path) -> None:
        evt = BridgeTransportEvent(
            session_id="s1",
            event_type="credential_refresh",
            reason=BridgeRebuildReason.CREDENTIAL_REFRESH,
            credential_expiry=1234567890.0,
        )
        record_bridge_event(tmp_path, evt)
        events = load_bridge_lineage(tmp_path)
        assert events[0].credential_expiry == pytest.approx(1234567890.0)
        assert events[0].event_type == "credential_refresh"
