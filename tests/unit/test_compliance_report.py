"""Tests for bernstein.core.compliance_report (GH-321)."""

from __future__ import annotations

import hashlib

import pytest

from bernstein.core.compliance_report import (
    SOC2_CONTROLS,
    CompliancePackage,
    ControlMapping,
    EvidenceSummary,
    build_compliance_package,
    compute_merkle_root,
    format_compliance_report,
    map_events_to_controls,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_type: str = "task.complete",
    timestamp: str = "2026-01-15T10:00:00.000000Z",
    hmac: str = "aabbcc",
    actor: str = "agent-1",
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "event_type": event_type,
        "actor": actor,
        "resource_type": "task",
        "resource_id": "t-1",
        "details": {},
        "prev_hmac": "0" * 64,
        "hmac": hmac,
    }


# ---------------------------------------------------------------------------
# ControlMapping
# ---------------------------------------------------------------------------


class TestControlMapping:
    def test_frozen(self) -> None:
        ctrl = ControlMapping(
            control_id="CC6.1",
            title="Logical Access",
            description="desc",
            evidence_types=["auth."],
        )
        with pytest.raises(AttributeError):
            ctrl.control_id = "CC6.2"  # type: ignore[misc]

    def test_fields(self) -> None:
        ctrl = ControlMapping(
            control_id="CC6.1",
            title="Logical Access",
            description="Protects access",
            evidence_types=["auth.", "login"],
        )
        assert ctrl.control_id == "CC6.1"
        assert ctrl.title == "Logical Access"
        assert ctrl.evidence_types == ["auth.", "login"]

    def test_soc2_controls_populated(self) -> None:
        assert len(SOC2_CONTROLS) == 4
        ids = {c.control_id for c in SOC2_CONTROLS}
        assert ids == {"CC6.1", "CC6.2", "CC7.2", "CC8.1"}


# ---------------------------------------------------------------------------
# compute_merkle_root
# ---------------------------------------------------------------------------


class TestComputeMerkleRoot:
    def test_empty_events(self) -> None:
        root = compute_merkle_root([])
        assert root == hashlib.sha256(b"").hexdigest()

    def test_single_event(self) -> None:
        event = _make_event(hmac="deadbeef")
        root = compute_merkle_root([event])
        leaf = hashlib.sha256(b"deadbeef").hexdigest()
        # Single leaf: only one node in the tree, so root == leaf hash.
        assert root == leaf

    def test_two_events(self) -> None:
        e1 = _make_event(hmac="aaa")
        e2 = _make_event(hmac="bbb")
        root = compute_merkle_root([e1, e2])

        # HMACs sorted: ["aaa", "bbb"]
        leaf_a = hashlib.sha256(b"aaa").hexdigest()
        leaf_b = hashlib.sha256(b"bbb").hexdigest()
        expected = hashlib.sha256((leaf_a + leaf_b).encode()).hexdigest()
        assert root == expected

    def test_three_events(self) -> None:
        events = [
            _make_event(hmac="ccc"),
            _make_event(hmac="aaa"),
            _make_event(hmac="bbb"),
        ]
        root = compute_merkle_root(events)

        # Sorted HMACs: aaa, bbb, ccc
        leaves = [hashlib.sha256(h.encode()).hexdigest() for h in ["aaa", "bbb", "ccc"]]
        # Layer 1: combine(leaves[0], leaves[1]), combine(leaves[2], leaves[2])
        n01 = hashlib.sha256((leaves[0] + leaves[1]).encode()).hexdigest()
        n22 = hashlib.sha256((leaves[2] + leaves[2]).encode()).hexdigest()
        # Root
        expected = hashlib.sha256((n01 + n22).encode()).hexdigest()
        assert root == expected

    def test_deterministic_regardless_of_order(self) -> None:
        e1 = _make_event(hmac="xxx")
        e2 = _make_event(hmac="yyy")
        assert compute_merkle_root([e1, e2]) == compute_merkle_root([e2, e1])

    def test_multiple_events_power_of_two(self) -> None:
        events = [_make_event(hmac=f"h{i}") for i in range(4)]
        root = compute_merkle_root(events)
        assert isinstance(root, str)
        assert len(root) == 64  # SHA-256 hex length


# ---------------------------------------------------------------------------
# map_events_to_controls
# ---------------------------------------------------------------------------


class TestMapEventsToControls:
    def test_matching_events(self) -> None:
        controls = [
            ControlMapping(
                control_id="CC6.1",
                title="Access",
                description="d",
                evidence_types=["auth."],
            ),
        ]
        events = [
            _make_event(event_type="auth.login", timestamp="2026-01-01T00:00:00Z"),
            _make_event(event_type="auth.logout", timestamp="2026-01-02T00:00:00Z"),
            _make_event(event_type="task.complete", timestamp="2026-01-03T00:00:00Z"),
        ]
        summaries = map_events_to_controls(events, controls)
        assert len(summaries) == 1
        assert summaries[0].control_id == "CC6.1"
        assert summaries[0].event_count == 2
        assert summaries[0].first_event == "2026-01-01T00:00:00Z"
        assert summaries[0].last_event == "2026-01-02T00:00:00Z"

    def test_no_matching_events(self) -> None:
        controls = [
            ControlMapping(
                control_id="CC6.1",
                title="Access",
                description="d",
                evidence_types=["auth."],
            ),
        ]
        events = [_make_event(event_type="task.complete")]
        summaries = map_events_to_controls(events, controls)
        assert summaries == []

    def test_event_maps_to_multiple_controls(self) -> None:
        controls = [
            ControlMapping(
                control_id="C1",
                title="C1",
                description="d",
                evidence_types=["task."],
            ),
            ControlMapping(
                control_id="C2",
                title="C2",
                description="d",
                evidence_types=["task."],
            ),
        ]
        events = [_make_event(event_type="task.complete")]
        summaries = map_events_to_controls(events, controls)
        assert len(summaries) == 2
        assert {s.control_id for s in summaries} == {"C1", "C2"}

    def test_sample_events_capped_at_five(self) -> None:
        controls = [
            ControlMapping(
                control_id="C1",
                title="C1",
                description="d",
                evidence_types=["task."],
            ),
        ]
        events = [_make_event(event_type="task.complete", hmac=f"h{i}") for i in range(10)]
        summaries = map_events_to_controls(events, controls)
        assert summaries[0].event_count == 10
        assert len(summaries[0].sample_events) == 5

    def test_sorted_by_control_id(self) -> None:
        controls = [
            ControlMapping(control_id="CC99", title="Z", description="d", evidence_types=["z."]),
            ControlMapping(control_id="CC01", title="A", description="d", evidence_types=["a."]),
        ]
        events = [
            _make_event(event_type="z.thing"),
            _make_event(event_type="a.thing"),
        ]
        summaries = map_events_to_controls(events, controls)
        assert summaries[0].control_id == "CC01"
        assert summaries[1].control_id == "CC99"


# ---------------------------------------------------------------------------
# build_compliance_package
# ---------------------------------------------------------------------------


class TestBuildCompliancePackage:
    def test_returns_frozen_package(self) -> None:
        pkg = build_compliance_package([], period="2026-Q1")
        assert isinstance(pkg, CompliancePackage)
        with pytest.raises(AttributeError):
            pkg.period = "2026-Q2"  # type: ignore[misc]

    def test_uses_default_controls(self) -> None:
        pkg = build_compliance_package([], period="2026-Q1")
        assert pkg.controls is SOC2_CONTROLS

    def test_custom_controls(self) -> None:
        custom = [
            ControlMapping(
                control_id="CUSTOM",
                title="Custom",
                description="d",
                evidence_types=["x."],
            ),
        ]
        pkg = build_compliance_package([], period="2026-Q1", controls=custom)
        assert pkg.controls is custom

    def test_total_events(self) -> None:
        events = [_make_event() for _ in range(7)]
        pkg = build_compliance_package(events, period="2026-Q1")
        assert pkg.total_events == 7

    def test_merkle_root_present(self) -> None:
        events = [_make_event(hmac="aaa"), _make_event(hmac="bbb")]
        pkg = build_compliance_package(events, period="2026-Q1")
        assert pkg.merkle_root == compute_merkle_root(events)
        assert len(pkg.merkle_root) == 64

    def test_generated_at_is_iso(self) -> None:
        pkg = build_compliance_package([], period="2026-Q1")
        # Should parse without error; format: YYYY-MM-DDTHH:MM:SS.ffffffZ
        assert "T" in pkg.generated_at
        assert pkg.generated_at.endswith("Z")

    def test_evidence_populated(self) -> None:
        events = [
            _make_event(event_type="task.complete"),
            _make_event(event_type="auth.login"),
        ]
        pkg = build_compliance_package(events, period="2026-Q1")
        control_ids_with_evidence = {e.control_id for e in pkg.evidence}
        # "task." maps to CC8.1, "auth." maps to CC6.1
        assert "CC8.1" in control_ids_with_evidence
        assert "CC6.1" in control_ids_with_evidence


# ---------------------------------------------------------------------------
# format_compliance_report
# ---------------------------------------------------------------------------


class TestFormatComplianceReport:
    def test_contains_header(self) -> None:
        pkg = build_compliance_package([], period="2026-Q1")
        report = format_compliance_report(pkg)
        assert "SOC 2 Type II Compliance Report" in report

    def test_contains_period(self) -> None:
        pkg = build_compliance_package([], period="2026-Q1")
        report = format_compliance_report(pkg)
        assert "2026-Q1" in report

    def test_contains_merkle_root(self) -> None:
        events = [_make_event(hmac="aaa")]
        pkg = build_compliance_package(events, period="2026-Q1")
        report = format_compliance_report(pkg)
        assert pkg.merkle_root in report

    def test_contains_control_ids(self) -> None:
        pkg = build_compliance_package([], period="2026-Q1")
        report = format_compliance_report(pkg)
        for ctrl in pkg.controls:
            assert ctrl.control_id in report

    def test_contains_evidence_counts(self) -> None:
        events = [_make_event(event_type="task.complete") for _ in range(3)]
        pkg = build_compliance_package(events, period="2026-Q1")
        report = format_compliance_report(pkg)
        assert "Events:  3" in report

    def test_no_evidence_message(self) -> None:
        custom = [
            ControlMapping(
                control_id="NONE",
                title="No Match",
                description="d",
                evidence_types=["zzz."],
            ),
        ]
        pkg = build_compliance_package([], period="2026-Q1", controls=custom)
        report = format_compliance_report(pkg)
        assert "No matching events found." in report

    def test_returns_string(self) -> None:
        pkg = build_compliance_package([], period="2026-Q1")
        report = format_compliance_report(pkg)
        assert isinstance(report, str)
        assert len(report) > 100


# ---------------------------------------------------------------------------
# EvidenceSummary frozen
# ---------------------------------------------------------------------------


class TestEvidenceSummary:
    def test_frozen(self) -> None:
        es = EvidenceSummary(
            control_id="CC6.1",
            event_count=5,
            first_event="2026-01-01T00:00:00Z",
            last_event="2026-01-31T00:00:00Z",
        )
        with pytest.raises(AttributeError):
            es.event_count = 10  # type: ignore[misc]
