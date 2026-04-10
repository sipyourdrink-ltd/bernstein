"""Tests for SEC-022: Security event correlation across multiple runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.security_correlation import (
    BUILTIN_PATTERNS,
    CorrelationMatch,
    CorrelationPattern,
    SecurityEvent,
    correlate_events,
    format_correlation_report,
    load_security_events,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_type: str = "secret_detected",
    agent_id: str = "agent-1",
    role: str = "backend",
    run_id: str = "run-1",
    timestamp: str = "2026-04-10T10:00:00+00:00",
    details: str = "found secret",
    severity: str = "high",
) -> SecurityEvent:
    return SecurityEvent(
        event_type=event_type,
        agent_id=agent_id,
        role=role,
        run_id=run_id,
        timestamp=timestamp,
        details=details,
        severity=severity,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# SecurityEvent dataclass
# ---------------------------------------------------------------------------


class TestSecurityEvent:
    def test_frozen(self) -> None:
        evt = _make_event()
        assert evt.event_type == "secret_detected"
        assert evt.severity == "high"

    def test_fields(self) -> None:
        evt = _make_event(agent_id="a-2", role="qa", run_id="r-5")
        assert evt.agent_id == "a-2"
        assert evt.role == "qa"
        assert evt.run_id == "r-5"


# ---------------------------------------------------------------------------
# BUILTIN_PATTERNS
# ---------------------------------------------------------------------------


class TestBuiltinPatterns:
    def test_patterns_exist(self) -> None:
        ids = {p.pattern_id for p in BUILTIN_PATTERNS}
        assert "repeated_secret_detection" in ids
        assert "permission_escalation_pattern" in ids
        assert "sandbox_escape_attempts" in ids

    def test_repeated_secret_detection_config(self) -> None:
        pat = next(p for p in BUILTIN_PATTERNS if p.pattern_id == "repeated_secret_detection")
        assert pat.min_occurrences == 3
        assert pat.time_window_hours == pytest.approx(24.0)
        assert pat.event_types == ["secret_detected"]

    def test_permission_escalation_config(self) -> None:
        pat = next(p for p in BUILTIN_PATTERNS if p.pattern_id == "permission_escalation_pattern")
        assert pat.min_occurrences == 5
        assert pat.time_window_hours == pytest.approx(1.0)

    def test_sandbox_escape_config(self) -> None:
        pat = next(p for p in BUILTIN_PATTERNS if p.pattern_id == "sandbox_escape_attempts")
        assert pat.min_occurrences == 2
        assert pat.time_window_hours == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# correlate_events
# ---------------------------------------------------------------------------


class TestCorrelateEvents:
    def test_match_repeated_secrets(self) -> None:
        events = [
            _make_event(timestamp="2026-04-10T10:00:00+00:00"),
            _make_event(timestamp="2026-04-10T11:00:00+00:00"),
            _make_event(timestamp="2026-04-10T12:00:00+00:00"),
        ]
        matches = correlate_events(events)
        assert len(matches) == 1
        assert matches[0].pattern.pattern_id == "repeated_secret_detection"
        assert matches[0].count == 3

    def test_no_match_below_threshold(self) -> None:
        events = [
            _make_event(timestamp="2026-04-10T10:00:00+00:00"),
            _make_event(timestamp="2026-04-10T11:00:00+00:00"),
        ]
        # Only 2 events, threshold is 3
        matches = correlate_events(events)
        assert len(matches) == 0

    def test_no_match_outside_time_window(self) -> None:
        events = [
            _make_event(timestamp="2026-04-10T01:00:00+00:00"),
            _make_event(timestamp="2026-04-11T10:00:00+00:00"),
            _make_event(timestamp="2026-04-12T20:00:00+00:00"),
        ]
        # Each pair is >24h apart
        matches = correlate_events(events)
        assert len(matches) == 0

    def test_no_match_wrong_event_type(self) -> None:
        events = [_make_event(event_type="info_message", timestamp=f"2026-04-10T1{i}:00:00+00:00") for i in range(5)]
        matches = correlate_events(events)
        assert len(matches) == 0

    def test_match_sandbox_escape(self) -> None:
        events = [
            _make_event(
                event_type="sandbox_violation",
                timestamp="2026-04-10T10:00:00+00:00",
                severity="critical",
            ),
            _make_event(
                event_type="sandbox_violation",
                agent_id="agent-2",
                timestamp="2026-04-10T10:30:00+00:00",
                severity="critical",
            ),
        ]
        matches = correlate_events(events)
        assert len(matches) == 1
        assert matches[0].pattern.pattern_id == "sandbox_escape_attempts"

    def test_match_permission_escalation(self) -> None:
        events = [
            _make_event(
                event_type="permission_denied",
                agent_id=f"agent-{i}",
                role="backend",
                timestamp=f"2026-04-10T10:{i:02d}:00+00:00",
            )
            for i in range(5)
        ]
        matches = correlate_events(events)
        assert len(matches) == 1
        assert matches[0].pattern.pattern_id == "permission_escalation_pattern"
        assert matches[0].count == 5

    def test_multiple_patterns_match(self) -> None:
        events = [
            # 3 secret detections → repeated_secret_detection
            _make_event(timestamp="2026-04-10T10:00:00+00:00"),
            _make_event(timestamp="2026-04-10T11:00:00+00:00"),
            _make_event(timestamp="2026-04-10T12:00:00+00:00"),
            # 2 sandbox violations → sandbox_escape_attempts
            _make_event(
                event_type="sandbox_violation",
                timestamp="2026-04-10T10:05:00+00:00",
                severity="critical",
            ),
            _make_event(
                event_type="sandbox_violation",
                timestamp="2026-04-10T10:10:00+00:00",
                severity="critical",
            ),
        ]
        matches = correlate_events(events)
        pattern_ids = {m.pattern.pattern_id for m in matches}
        assert "repeated_secret_detection" in pattern_ids
        assert "sandbox_escape_attempts" in pattern_ids

    def test_custom_pattern(self) -> None:
        custom = CorrelationPattern(
            pattern_id="custom_test",
            description="Test pattern",
            event_types=["test_event"],
            min_occurrences=2,
            time_window_hours=1.0,
            severity="medium",
        )
        events = [
            _make_event(event_type="test_event", timestamp="2026-04-10T10:00:00+00:00"),
            _make_event(event_type="test_event", timestamp="2026-04-10T10:30:00+00:00"),
        ]
        matches = correlate_events(events, patterns=[custom])
        assert len(matches) == 1
        assert matches[0].pattern.pattern_id == "custom_test"

    def test_groups_by_agent_for_secrets(self) -> None:
        events = [
            _make_event(agent_id="agent-1", timestamp="2026-04-10T10:00:00+00:00"),
            _make_event(agent_id="agent-1", timestamp="2026-04-10T11:00:00+00:00"),
            _make_event(agent_id="agent-2", timestamp="2026-04-10T10:00:00+00:00"),
            _make_event(agent_id="agent-2", timestamp="2026-04-10T11:00:00+00:00"),
        ]
        # Neither agent reaches 3 events
        matches = correlate_events(events)
        assert len(matches) == 0

    def test_empty_events(self) -> None:
        matches = correlate_events([])
        assert matches == []

    def test_match_fields(self) -> None:
        events = [
            _make_event(timestamp="2026-04-10T10:00:00+00:00"),
            _make_event(timestamp="2026-04-10T11:00:00+00:00"),
            _make_event(timestamp="2026-04-10T12:00:00+00:00"),
        ]
        matches = correlate_events(events)
        m = matches[0]
        assert m.first_seen == "2026-04-10T10:00:00+00:00"
        assert m.last_seen == "2026-04-10T12:00:00+00:00"
        assert m.count == 3
        assert isinstance(m.events, list)
        assert len(m.events) == 3


# ---------------------------------------------------------------------------
# load_security_events
# ---------------------------------------------------------------------------


class TestLoadSecurityEvents:
    def test_loads_from_jsonl(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        records = [
            {
                "event_type": "secret_detected",
                "actor": "agent-1",
                "timestamp": "2026-04-10T10:00:00+00:00",
                "details": {"role": "backend", "run_id": "run-1", "severity": "high", "description": "key leaked"},
            },
            {
                "event_type": "task.transition",
                "actor": "orchestrator",
                "timestamp": "2026-04-10T10:01:00+00:00",
                "details": {},
            },
        ]
        jsonl_path = audit_dir / "2026-04-10.jsonl"
        jsonl_path.write_text("\n".join(json.dumps(r) for r in records))

        events = load_security_events(audit_dir)
        assert len(events) == 1
        assert events[0].event_type == "secret_detected"
        assert events[0].agent_id == "agent-1"
        assert events[0].role == "backend"
        assert events[0].details == "key leaked"

    def test_filters_by_run_id(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        records = [
            {
                "event_type": "secret_detected",
                "actor": "a-1",
                "timestamp": "2026-04-10T10:00:00+00:00",
                "details": {"run_id": "run-1", "role": "qa"},
            },
            {
                "event_type": "secret_detected",
                "actor": "a-2",
                "timestamp": "2026-04-10T11:00:00+00:00",
                "details": {"run_id": "run-2", "role": "qa"},
            },
        ]
        (audit_dir / "2026-04-10.jsonl").write_text("\n".join(json.dumps(r) for r in records))

        events = load_security_events(audit_dir, run_ids=["run-1"])
        assert len(events) == 1
        assert events[0].run_id == "run-1"

    def test_missing_dir(self, tmp_path: Path) -> None:
        events = load_security_events(tmp_path / "nonexistent")
        assert events == []

    def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        content = "not json\n" + json.dumps(
            {
                "event_type": "sandbox_violation",
                "actor": "a-1",
                "timestamp": "2026-04-10T10:00:00+00:00",
                "details": {"role": "backend", "severity": "critical"},
            }
        )
        (audit_dir / "2026-04-10.jsonl").write_text(content)

        events = load_security_events(audit_dir)
        assert len(events) == 1
        assert events[0].event_type == "sandbox_violation"


# ---------------------------------------------------------------------------
# format_correlation_report
# ---------------------------------------------------------------------------


class TestFormatCorrelationReport:
    def test_empty_matches(self) -> None:
        report = format_correlation_report([])
        assert report == "No correlation matches found."

    def test_report_contains_pattern_info(self) -> None:
        events = [
            _make_event(timestamp="2026-04-10T10:00:00+00:00"),
            _make_event(timestamp="2026-04-10T11:00:00+00:00"),
            _make_event(timestamp="2026-04-10T12:00:00+00:00"),
        ]
        matches = correlate_events(events)
        report = format_correlation_report(matches)
        assert "repeated_secret_detection" in report
        assert "Security Correlation Report" in report
        assert "Match #1" in report
        assert "high" in report

    def test_report_contains_event_details(self) -> None:
        match = CorrelationMatch(
            pattern=CorrelationPattern(
                pattern_id="test_pat",
                description="Test",
                event_types=["secret_detected"],
                min_occurrences=1,
                time_window_hours=1.0,
                severity="medium",
            ),
            events=[_make_event(details="API key in env")],
            first_seen="2026-04-10T10:00:00+00:00",
            last_seen="2026-04-10T10:00:00+00:00",
            count=1,
        )
        report = format_correlation_report([match])
        assert "API key in env" in report
        assert "agent-1" in report
