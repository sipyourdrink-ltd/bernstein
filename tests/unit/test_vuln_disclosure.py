"""Tests for bernstein.core.security.vuln_disclosure.

Covers vulnerability report lifecycle, triage, disclosure timelines,
uniform (non-severity-based) recognition, SLA compliance, and
security.txt generation. The defaults here track SECURITY.md: a 90-day
first-response window, no fix deadline, and one uniform credit path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bernstein.core.security.vuln_disclosure import (
    DisclosureScope,
    DisclosureTimeline,
    RecognitionTier,
    ReportStatus,
    VulnerabilityDisclosureManager,
    VulnReport,
    generate_security_txt,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_report() -> VulnReport:
    """Create a sample vulnerability report for testing."""
    return VulnReport(
        report_id="VR-001",
        severity="high",
        title="SQL Injection in login endpoint",
        description="User input concatenated directly into SQL query in /api/v1/login.",
        reporter_email="researcher@example.com",
        submitted_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        affected_components=("/api/v1/login", "AuthService"),
    )


@pytest.fixture
def sample_scope() -> DisclosureScope:
    """Create a sample disclosure scope."""
    return DisclosureScope(
        in_scope=("/api/v1/*", "/api/v2/*"),
        out_of_scope=("/static/*", "/docs/*"),
    )


@pytest.fixture
def manager(sample_scope: DisclosureScope) -> VulnerabilityDisclosureManager:
    """Create a disclosure manager with the sample scope."""
    return VulnerabilityDisclosureManager(scope=sample_scope, disclosure_days=90)


# ---------------------------------------------------------------------------
# generate_security_txt
# ---------------------------------------------------------------------------


class TestGenerateSecurityTxt:
    """Tests for security.txt generation (RFC 9116)."""

    def test_basic_generation(self) -> None:
        result = generate_security_txt(
            contact="mailto:security@example.com",
            policy_url="https://example.com/security-policy",
        )
        assert "Contact: mailto:security@example.com" in result
        assert "Policy: https://example.com/security-policy" in result
        assert "Expires:" in result

    def test_with_all_optional_fields(self) -> None:
        expires = datetime(2027, 1, 1, tzinfo=UTC)
        result = generate_security_txt(
            contact="mailto:security@example.com",
            policy_url="https://example.com/security-policy",
            expires=expires,
            encryption="https://example.com/pgp-key.asc",
            acknowledgments="https://example.com/hall-of-fame",
            preferred_languages="en, fr",
            hiring="https://example.com/security-jobs",
        )
        assert "Encryption: https://example.com/pgp-key.asc" in result
        assert "Acknowledgments: https://example.com/hall-of-fame" in result
        assert "Preferred-Languages: en, fr" in result
        assert "Hiring: https://example.com/security-jobs" in result
        assert "Expires: 2027-01-01T00:00:00.000Z" in result

    def test_missing_contact_raises(self) -> None:
        with pytest.raises(ValueError, match="contact"):
            generate_security_txt(
                contact="",
                policy_url="https://example.com/policy",
            )

    def test_missing_policy_raises(self) -> None:
        with pytest.raises(ValueError, match="policy_url"):
            generate_security_txt(
                contact="mailto:security@example.com",
                policy_url="",
            )

    def test_default_expiry_is_one_year(self) -> None:
        result = generate_security_txt(
            contact="mailto:security@example.com",
            policy_url="https://example.com/policy",
        )
        # Should contain an Expires line
        assert "Expires: " in result


# ---------------------------------------------------------------------------
# VulnerabilityDisclosureManager - submit
# ---------------------------------------------------------------------------


class TestSubmitReport:
    """Tests for report submission."""

    def test_submit_returns_report_id(self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport) -> None:
        report_id = manager.submit_report(sample_report)
        assert report_id == "VR-001"

    def test_submit_generates_id_if_missing(self, manager: VulnerabilityDisclosureManager) -> None:
        report = VulnReport(
            report_id="",
            severity="medium",
            title="XSS in comments",
            description="Reflected XSS in comment section",
            reporter_email="bob@example.com",
        )
        report_id = manager.submit_report(report)
        assert report_id.startswith("VR-")
        assert len(report_id) > 4

    def test_submit_stores_report(self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport) -> None:
        manager.submit_report(sample_report)
        stored = manager.reports["VR-001"]
        assert stored.title == "SQL Injection in login endpoint"
        assert stored.status == ReportStatus.NEW.value

    def test_submit_overwrites_on_duplicate_id(self, manager: VulnerabilityDisclosureManager) -> None:
        r1 = VulnReport(
            report_id="VR-099",
            severity="low",
            title="First",
            description="desc",
            reporter_email="a@b.com",
        )
        r2 = VulnReport(
            report_id="VR-099",
            severity="critical",
            title="Second",
            description="desc2",
            reporter_email="c@d.com",
        )
        manager.submit_report(r1)
        manager.submit_report(r2)
        assert manager.reports["VR-099"].title == "Second"


# ---------------------------------------------------------------------------
# VulnerabilityDisclosureManager - triage
# ---------------------------------------------------------------------------


class TestTriageReport:
    """Tests for report triage."""

    def test_triage_updates_status(self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport) -> None:
        manager.submit_report(sample_report)
        triaged = manager.triage_report("VR-001")
        assert triaged.status == ReportStatus.TRIAGED.value

    def test_triage_updates_severity(self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport) -> None:
        manager.submit_report(sample_report)
        triaged = manager.triage_report("VR-001", assessed_severity="critical")
        assert triaged.severity == "critical"

    def test_triage_assigns_cve(self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport) -> None:
        manager.submit_report(sample_report)
        triaged = manager.triage_report("VR-001", cve_id="CVE-2026-0001")
        assert triaged.cve_id == "CVE-2026-0001"

    def test_triage_invalid_severity_raises(
        self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport
    ) -> None:
        manager.submit_report(sample_report)
        with pytest.raises(ValueError, match="Invalid severity"):
            manager.triage_report("VR-001", assessed_severity="catastrophic")

    def test_triage_nonexistent_raises(self, manager: VulnerabilityDisclosureManager) -> None:
        with pytest.raises(KeyError, match="VR-999"):
            manager.triage_report("VR-999")

    def test_all_severity_values_accepted(self, manager: VulnerabilityDisclosureManager) -> None:
        report = VulnReport(
            report_id="VR-SEV",
            severity="low",
            title="Test",
            description="desc",
            reporter_email="a@b.com",
        )
        manager.submit_report(report)
        for sev in ("low", "medium", "high", "critical"):
            triaged = manager.triage_report("VR-SEV", assessed_severity=sev)
            assert triaged.severity == sev


# ---------------------------------------------------------------------------
# VulnerabilityDisclosureManager - fix tracking
# ---------------------------------------------------------------------------


class TestFixTracking:
    """Tests for fix status transitions."""

    def test_mark_fixing(self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport) -> None:
        manager.submit_report(sample_report)
        manager.triage_report("VR-001")
        fixing = manager.mark_fixing("VR-001")
        assert fixing.status == ReportStatus.FIXING.value

    def test_mark_resolved(self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport) -> None:
        manager.submit_report(sample_report)
        manager.triage_report("VR-001")
        manager.mark_fixing("VR-001")
        resolved = manager.mark_resolved("VR-001")
        assert resolved.status == ReportStatus.RESOLVED.value

    def test_mark_resolved_nonexistent_raises(self, manager: VulnerabilityDisclosureManager) -> None:
        with pytest.raises(KeyError):
            manager.mark_resolved("VR-MISSING")


# ---------------------------------------------------------------------------
# VulnerabilityDisclosureManager - disclosure timeline
# ---------------------------------------------------------------------------


class TestDisclosureTimeline:
    """Tests for coordinated disclosure timeline generation.

    SECURITY.md promises a first response, not a fix date, so the timeline
    must carry no fix-deadline milestone.
    """

    def test_timeline_has_no_fix_deadline_field(self) -> None:
        """DisclosureTimeline no longer models a fix deadline at all."""
        assert not hasattr(DisclosureTimeline, "__dataclass_fields__") or (
            "fix_deadline" not in DisclosureTimeline.__dataclass_fields__
        )

    def test_timeline_milestones_have_no_fix_deadline_key(
        self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport
    ) -> None:
        manager.submit_report(sample_report)
        timeline = manager.generate_disclosure_timeline("VR-001")
        assert "fix_deadline" not in timeline.milestones

    def test_timeline_has_response_and_disclosure_milestones(
        self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport
    ) -> None:
        manager.submit_report(sample_report)
        timeline = manager.generate_disclosure_timeline("VR-001")
        assert isinstance(timeline, DisclosureTimeline)
        assert timeline.report_id == "VR-001"
        assert "submitted" in timeline.milestones
        assert "vendor_notified" in timeline.milestones
        assert "first_response_due" in timeline.milestones
        assert "public_disclosure" in timeline.milestones

    def test_first_response_due_respects_scope_sla(
        self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport
    ) -> None:
        manager.submit_report(sample_report)
        timeline = manager.generate_disclosure_timeline("VR-001")
        expected = sample_report.submitted_at + timedelta(hours=manager.scope.response_sla_hours)
        assert timeline.first_response_due == expected

    def test_public_disclosure_is_ninety_days_after_submission_by_default(
        self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport
    ) -> None:
        """Matches SECURITY.md: reporter may disclose 90 days after report,
        independent of fix status."""
        manager.submit_report(sample_report)
        timeline = manager.generate_disclosure_timeline("VR-001")
        expected_disclosure = sample_report.submitted_at + timedelta(days=90)
        assert timeline.public_disclosure == expected_disclosure

    def test_public_disclosure_respects_custom_disclosure_days(self, sample_report: VulnReport) -> None:
        mgr = VulnerabilityDisclosureManager(disclosure_days=45)
        mgr.submit_report(sample_report)
        timeline = mgr.generate_disclosure_timeline("VR-001")
        expected = sample_report.submitted_at + timedelta(days=45)
        assert timeline.public_disclosure == expected

    def test_timeline_nonexistent_raises(self, manager: VulnerabilityDisclosureManager) -> None:
        with pytest.raises(KeyError):
            manager.generate_disclosure_timeline("VR-MISSING")


# ---------------------------------------------------------------------------
# VulnerabilityDisclosureManager - recognition classification
# ---------------------------------------------------------------------------


class TestRecognitionClassification:
    """Tests for uniform (non-severity-based) recognition classification."""

    def test_recognition_tier_has_no_severity_ladder(self) -> None:
        """RecognitionTier carries exactly the uniform states, no per-severity
        tiers and no hall-of-fame (removed with the page in #3689)."""
        values = {t.value for t in RecognitionTier}
        assert values == {"none", "credited"}
        assert not hasattr(RecognitionTier, "HALL_OF_FAME")
        assert not hasattr(RecognitionTier, "PUBLIC_CREDIT")
        assert not hasattr(RecognitionTier, "CVE_CREDIT")
        assert not hasattr(RecognitionTier, "ACKNOWLEDGMENT")

    def test_high_severity_is_credited(
        self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport
    ) -> None:
        manager.submit_report(sample_report)
        manager.triage_report("VR-001")
        assert manager.classify_recognition("VR-001") == RecognitionTier.CREDITED.value

    def test_low_severity_is_credited_the_same_as_critical(self, manager: VulnerabilityDisclosureManager) -> None:
        """Credit is uniform - a low-severity report is credited identically
        to a critical one, since SECURITY.md states one credit path."""
        low = VulnReport(
            report_id="VR-LOW",
            severity="low",
            title="Info leak",
            description="Information disclosure",
            reporter_email="a@b.com",
        )
        critical = VulnReport(
            report_id="VR-CRIT",
            severity="critical",
            title="RCE",
            description="Remote code execution",
            reporter_email="c@d.com",
        )
        manager.submit_report(low)
        manager.submit_report(critical)
        manager.triage_report("VR-LOW")
        manager.triage_report("VR-CRIT")
        assert manager.classify_recognition("VR-LOW") == manager.classify_recognition("VR-CRIT") == "credited"

    def test_rejected_report_gets_no_recognition(
        self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport
    ) -> None:
        manager.submit_report(sample_report)
        # No status transition to "rejected" is exposed on the manager yet;
        # exercise the classification directly against a rejected VulnReport
        # stored via triage's returned dataclass semantics.
        report = manager.triage_report("VR-001")
        rejected = VulnReport(
            report_id=report.report_id,
            severity=report.severity,
            title=report.title,
            description=report.description,
            reporter_email=report.reporter_email,
            submitted_at=report.submitted_at,
            status=ReportStatus.REJECTED.value,
            cve_id=report.cve_id,
            affected_components=report.affected_components,
        )
        manager._reports[report.report_id] = rejected  # exercising internal state directly
        assert manager.classify_recognition("VR-001") == RecognitionTier.NONE.value

    def test_classify_nonexistent_raises(self, manager: VulnerabilityDisclosureManager) -> None:
        with pytest.raises(KeyError):
            manager.classify_recognition("VR-MISSING")


# ---------------------------------------------------------------------------
# VulnerabilityDisclosureManager - SLA compliance
# ---------------------------------------------------------------------------


class TestSLACompliance:
    """Tests for first-response SLA compliance checking.

    There is no fix deadline, so ``check_sla_compliance`` reports only the
    first-response SLA - no ``fix_within_sla`` or ``disclosure_within_sla``
    keys.
    """

    def _submit_fresh_report(self, manager: VulnerabilityDisclosureManager, report_id: str) -> None:
        """Submit a report timestamped now, so the 90-day SLA window has not
        elapsed regardless of when the test suite runs."""
        report = VulnReport(
            report_id=report_id,
            severity="high",
            title="Fresh report",
            description="desc",
            reporter_email="a@b.com",
            submitted_at=datetime.now(UTC),
        )
        manager.submit_report(report)

    def test_new_report_response_sla_ok(self, manager: VulnerabilityDisclosureManager) -> None:
        self._submit_fresh_report(manager, "VR-FRESH-1")
        compliance = manager.check_sla_compliance("VR-FRESH-1")
        assert compliance["response_within_sla"] is True

    def test_compliance_dict_has_no_fix_or_disclosure_keys(self, manager: VulnerabilityDisclosureManager) -> None:
        self._submit_fresh_report(manager, "VR-FRESH-2")
        compliance = manager.check_sla_compliance("VR-FRESH-2")
        assert set(compliance.keys()) == {"response_within_sla"}

    def test_triaged_report_response_sla_ok(self, manager: VulnerabilityDisclosureManager) -> None:
        self._submit_fresh_report(manager, "VR-FRESH-3")
        manager.triage_report("VR-FRESH-3")
        compliance = manager.check_sla_compliance("VR-FRESH-3")
        assert compliance["response_within_sla"] is True

    def test_old_new_report_past_sla_window_is_out_of_compliance(
        self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport
    ) -> None:
        """A report still at status=new, submitted more than 90 days ago
        (``sample_report`` is dated 2026-01-15), has missed the first
        response SLA."""
        manager.submit_report(sample_report)
        compliance = manager.check_sla_compliance("VR-001")
        assert compliance["response_within_sla"] is False

    def test_nonexistent_raises(self, manager: VulnerabilityDisclosureManager) -> None:
        with pytest.raises(KeyError):
            manager.check_sla_compliance("VR-MISSING")


# ---------------------------------------------------------------------------
# VulnReport data model
# ---------------------------------------------------------------------------


class TestVulnReport:
    """Tests for the VulnReport dataclass."""

    def test_frozen_report_cannot_mutate(self) -> None:
        report = VulnReport(
            report_id="VR-F",
            severity="low",
            title="Test",
            description="desc",
            reporter_email="a@b.com",
        )
        with pytest.raises(AttributeError):
            report.severity = "critical"  # type: ignore[misc]

    def test_default_values(self) -> None:
        report = VulnReport(
            report_id="VR-D",
            severity="medium",
            title="Defaults",
            description="desc",
            reporter_email="a@b.com",
        )
        assert report.status == ReportStatus.NEW.value
        assert report.cve_id is None
        assert report.affected_components == ()
        assert isinstance(report.submitted_at, datetime)

    def test_all_fields_set(self) -> None:
        report = VulnReport(
            report_id="VR-A",
            severity="critical",
            title="Full",
            description="Full desc",
            reporter_email="a@b.com",
            submitted_at=datetime(2026, 6, 1, tzinfo=UTC),
            status=ReportStatus.FIXING.value,
            cve_id="CVE-2026-1234",
            affected_components=("/api/v1/login", "AuthService"),
        )
        assert report.cve_id == "CVE-2026-1234"
        assert len(report.affected_components) == 2


# ---------------------------------------------------------------------------
# DisclosureScope data model
# ---------------------------------------------------------------------------


class TestDisclosureScope:
    """Tests for the DisclosureScope dataclass."""

    def test_default_response_sla_matches_ninety_day_policy(self) -> None:
        """SECURITY.md states up to 90 days for a first substantive
        response; the default must be that value in hours, not the
        retired 48-hour triage SLA."""
        scope = DisclosureScope()
        assert scope.response_sla_hours == 90 * 24

    def test_custom_response_sla(self) -> None:
        scope = DisclosureScope(in_scope=("/api/*",), response_sla_hours=24)
        assert scope.response_sla_hours == 24

    def test_no_recognition_field(self) -> None:
        """The scope no longer carries a severity-keyed recognition table -
        credit is uniform and lives in classify_recognition, not scope
        configuration."""
        scope = DisclosureScope(in_scope=("/api/*",))
        assert not hasattr(scope, "recognition")

    def test_no_monetary_fields(self) -> None:
        """The scope carries no dollar-denominated reward fields."""
        scope = DisclosureScope(in_scope=("/api/*",))
        assert not hasattr(scope, "rewards")
        assert not hasattr(scope, "max_reward")

    def test_default_scope_needs_no_targets(self) -> None:
        """A scope is constructible with no in-scope targets.

        The manager falls back to ``DisclosureScope()`` when no scope is
        supplied, so an empty in-scope default must be valid.
        """
        scope = DisclosureScope()
        assert scope.in_scope == ()

    def test_manager_default_scope_is_constructible(self) -> None:
        """VulnerabilityDisclosureManager() works with no scope argument."""
        mgr = VulnerabilityDisclosureManager()
        assert mgr.scope.in_scope == ()

    def test_frozen_scope(self) -> None:
        scope = DisclosureScope(in_scope=("/api/*",))
        with pytest.raises(AttributeError):
            scope.response_sla_hours = 999  # type: ignore[misc]

    def test_manager_has_no_fix_deadline_days_param(self) -> None:
        """fix_deadline_days is gone from the constructor - passing it must
        raise, not silently no-op."""
        with pytest.raises(TypeError):
            VulnerabilityDisclosureManager(fix_deadline_days=90)  # type: ignore[call-arg]

    def test_manager_has_no_triage_sla_hours_param(self) -> None:
        """triage_sla_hours duplicated scope.response_sla_hours with a
        different (stale) default; it is gone from the constructor."""
        with pytest.raises(TypeError):
            VulnerabilityDisclosureManager(triage_sla_hours=48)  # type: ignore[call-arg]
