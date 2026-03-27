"""Tests for pivot signal system."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.signals import (
    PivotSignal,
    TicketChange,
    VPDecision,
    file_pivot_signal,
    needs_vp_review,
    read_pivot_signals,
    read_unresolved_pivots,
    record_ticket_change,
    record_vp_decision,
)

if TYPE_CHECKING:
    from pathlib import Path


def _make_signal(
    *,
    severity: str = "high",
    task_id: str = "T-100",
    affected: list[str] | None = None,
) -> PivotSignal:
    return PivotSignal(
        timestamp="2026-03-28T12:00:00Z",
        agent_id="backend-abc",
        task_id=task_id,
        severity=severity,  # type: ignore[arg-type]
        summary="Port 8052 hardcoded everywhere",
        affected_tickets=affected or ["501", "503"],
        proposed_action="Create centralized config",
    )


class TestFileAndReadPivotSignals:
    def test_file_and_read_roundtrip(self, tmp_path: Path) -> None:
        signal = _make_signal()
        file_pivot_signal(signal, tmp_path)

        signals = read_pivot_signals(tmp_path)
        assert len(signals) == 1
        assert signals[0].agent_id == "backend-abc"
        assert signals[0].severity == "high"
        assert signals[0].affected_tickets == ["501", "503"]

    def test_multiple_signals_appended(self, tmp_path: Path) -> None:
        file_pivot_signal(_make_signal(task_id="T-1"), tmp_path)
        file_pivot_signal(_make_signal(task_id="T-2"), tmp_path)

        signals = read_pivot_signals(tmp_path)
        assert len(signals) == 2
        assert signals[0].task_id == "T-1"
        assert signals[1].task_id == "T-2"

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        signals = read_pivot_signals(tmp_path)
        assert signals == []

    def test_malformed_line_skipped(self, tmp_path: Path) -> None:
        signals_dir = tmp_path / ".sdd" / "signals"
        signals_dir.mkdir(parents=True)
        path = signals_dir / "pivots.jsonl"
        path.write_text("not valid json\n")

        signals = read_pivot_signals(tmp_path)
        assert signals == []

    def test_creates_sdd_signals_dir(self, tmp_path: Path) -> None:
        signal = _make_signal()
        file_pivot_signal(signal, tmp_path)

        assert (tmp_path / ".sdd" / "signals" / "pivots.jsonl").exists()


class TestRecordTicketChange:
    def test_writes_change_to_jsonl(self, tmp_path: Path) -> None:
        change = TicketChange(
            timestamp="2026-03-28T12:30:00Z",
            pivot_signal_task_id="T-100",
            changed_by="vp",
            ticket_id="501",
            field_name="priority",
            before="3",
            after="1",
        )
        record_ticket_change(change, tmp_path)

        path = tmp_path / ".sdd" / "signals" / "ticket_changes.jsonl"
        assert path.exists()
        data = json.loads(path.read_text().strip())
        assert data["ticket_id"] == "501"
        assert data["before"] == "3"
        assert data["after"] == "1"


class TestNeedsVPReview:
    def test_high_severity_needs_review(self) -> None:
        signal = _make_signal(severity="high", affected=["501"])
        assert needs_vp_review(signal) is True

    def test_low_severity_few_tickets_no_review(self) -> None:
        signal = _make_signal(severity="low", affected=["501"])
        assert needs_vp_review(signal) is False

    def test_medium_severity_few_tickets_no_review(self) -> None:
        signal = _make_signal(severity="medium", affected=["501", "502"])
        assert needs_vp_review(signal) is False

    def test_low_severity_many_tickets_needs_review(self) -> None:
        signal = _make_signal(severity="low", affected=["501", "502", "503"])
        assert needs_vp_review(signal) is True

    def test_medium_severity_three_tickets_needs_review(self) -> None:
        signal = _make_signal(severity="medium", affected=["501", "502", "503"])
        assert needs_vp_review(signal) is True


class TestReadUnresolvedPivots:
    def test_no_signals_returns_empty(self, tmp_path: Path) -> None:
        assert read_unresolved_pivots(tmp_path) == []

    def test_low_severity_not_returned(self, tmp_path: Path) -> None:
        file_pivot_signal(_make_signal(severity="low"), tmp_path)
        assert read_unresolved_pivots(tmp_path) == []

    def test_high_severity_returned_when_no_decision(self, tmp_path: Path) -> None:
        file_pivot_signal(_make_signal(severity="high", task_id="T-1"), tmp_path)
        unresolved = read_unresolved_pivots(tmp_path)
        assert len(unresolved) == 1
        assert unresolved[0].task_id == "T-1"

    def test_resolved_pivot_excluded(self, tmp_path: Path) -> None:
        file_pivot_signal(_make_signal(severity="high", task_id="T-1"), tmp_path)
        decision = VPDecision(
            pivot_task_id="T-1",
            decision="approve",
            rationale="Valid finding",
        )
        record_vp_decision(decision, tmp_path)

        assert read_unresolved_pivots(tmp_path) == []

    def test_mixed_resolved_and_unresolved(self, tmp_path: Path) -> None:
        file_pivot_signal(_make_signal(severity="high", task_id="T-1"), tmp_path)
        file_pivot_signal(_make_signal(severity="high", task_id="T-2"), tmp_path)

        decision = VPDecision(
            pivot_task_id="T-1",
            decision="reject",
            rationale="Not relevant",
        )
        record_vp_decision(decision, tmp_path)

        unresolved = read_unresolved_pivots(tmp_path)
        assert len(unresolved) == 1
        assert unresolved[0].task_id == "T-2"


class TestVPDecision:
    def test_record_and_read_decision(self, tmp_path: Path) -> None:
        decision = VPDecision(
            pivot_task_id="T-100",
            decision="escalate",
            rationale="Needs human input on budget",
            timestamp="2026-03-28T13:00:00Z",
        )
        record_vp_decision(decision, tmp_path)

        path = tmp_path / ".sdd" / "signals" / "vp_decisions.jsonl"
        assert path.exists()
        data = json.loads(path.read_text().strip())
        assert data["decision"] == "escalate"
        assert data["pivot_task_id"] == "T-100"
