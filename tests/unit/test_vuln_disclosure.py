"""Tests for bernstein.core.security.vuln_disclosure.

Covers vulnerability report lifecycle, triage, disclosure timelines,
reward calculation, SLA compliance, and security.txt generation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bernstein.core.security.vuln_disclosure import (
    BountyScope,
    DisclosureTimeline,
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
def sample_scope() -> BountyScope:
    """Create a sample bounty scope."""
    return BountyScope(
        in_scope=("/api/v1/*", "/api/v2/*"),
        out_of_scope=("/static/*", "/docs/*"),
        rewards={"low": 100.0, "medium": 500.0, "high": 1500.0, "critical": 5000.0},
        response_sla_hours=48,
        max_reward=10000.0,
    )


@pytest.fixture
def manager(sample_scope: BountyScope) -> VulnerabilityDisclosureManager:
    """Create a disclosure manager with the sample scope."""
    return VulnerabilityDisclosureManager(
        scope=sample_scope,
        triage_sla_hours=48,
        fix_deadline_days=90,
        disclosure_delay_days=30,
    )


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
# VulnerabilityDisclosureManager — submit
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
# VulnerabilityDisclosureManager — triage
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
# VulnerabilityDisclosureManager — fix tracking
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
# VulnerabilityDisclosureManager — disclosure timeline
# ---------------------------------------------------------------------------


class TestDisclosureTimeline:
    """Tests for coordinated disclosure timeline generation."""

    def test_timeline_has_all_milestones(
        self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport
    ) -> None:
        manager.submit_report(sample_report)
        timeline = manager.generate_disclosure_timeline("VR-001")
        assert isinstance(timeline, DisclosureTimeline)
        assert timeline.report_id == "VR-001"
        assert "submitted" in timeline.milestones
        assert "triage" in timeline.milestones
        assert "fix_deadline" in timeline.milestones
        assert "public_disclosure" in timeline.milestones

    def test_triage_deadline_respects_sla(
        self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport
    ) -> None:
        manager.submit_report(sample_report)
        timeline = manager.generate_disclosure_timeline("VR-001")
        expected_triage = sample_report.submitted_at + timedelta(hours=48)
        assert timeline.triage_date == expected_triage

    def test_fix_deadline_respects_config(
        self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport
    ) -> None:
        manager.submit_report(sample_report)
        timeline = manager.generate_disclosure_timeline("VR-001")
        expected_fix = sample_report.submitted_at + timedelta(days=90)
        assert timeline.fix_deadline == expected_fix

    def test_public_disclosure_after_fix_deadline(
        self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport
    ) -> None:
        manager.submit_report(sample_report)
        timeline = manager.generate_disclosure_timeline("VR-001")
        assert timeline.public_disclosure > timeline.fix_deadline
        expected_disclosure = sample_report.submitted_at + timedelta(days=120)
        assert timeline.public_disclosure == expected_disclosure

    def test_timeline_nonexistent_raises(self, manager: VulnerabilityDisclosureManager) -> None:
        with pytest.raises(KeyError):
            manager.generate_disclosure_timeline("VR-MISSING")


# ---------------------------------------------------------------------------
# VulnerabilityDisclosureManager — reward calculation
# ---------------------------------------------------------------------------


class TestRewardCalculation:
    """Tests for bounty reward calculation."""

    def test_high_severity_reward(self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport) -> None:
        manager.submit_report(sample_report)
        manager.triage_report("VR-001")
        reward = manager.calculate_reward("VR-001")
        assert reward == pytest.approx(1500.0)

    def test_critical_severity_reward(self, manager: VulnerabilityDisclosureManager) -> None:
        report = VulnReport(
            report_id="VR-CRIT",
            severity="critical",
            title="RCE",
            description="Remote code execution",
            reporter_email="a@b.com",
        )
        manager.submit_report(report)
        manager.triage_report("VR-CRIT")
        reward = manager.calculate_reward("VR-CRIT")
        assert reward == pytest.approx(5000.0)

    def test_unknown_severity_zero_reward(self, manager: VulnerabilityDisclosureManager) -> None:
        """Severity not in the reward table should return 0."""
        # Use a scope with no "info" tier
        scope = BountyScope(
            in_scope=("/api/*",),
            rewards={"high": 1000, "critical": 5000},
        )
        mgr = VulnerabilityDisclosureManager(scope=scope)
        report = VulnReport(
            report_id="VR-LOW",
            severity="low",
            title="Info leak",
            description="Information disclosure",
            reporter_email="a@b.com",
        )
        mgr.submit_report(report)
        mgr.triage_report("VR-LOW")
        assert mgr.calculate_reward("VR-LOW") == pytest.approx(0.0)

    def test_reward_capped_at_max(self, manager: VulnerabilityDisclosureManager) -> None:
        scope = BountyScope(
            in_scope=("/api/*",),
            rewards={"critical": 50000.0},
            max_reward=10000.0,
        )
        mgr = VulnerabilityDisclosureManager(scope=scope)
        report = VulnReport(
            report_id="VR-CAP",
            severity="critical",
            title="RCE",
            description="Remote code execution",
            reporter_email="a@b.com",
        )
        mgr.submit_report(report)
        mgr.triage_report("VR-CAP")
        assert mgr.calculate_reward("VR-CAP") == pytest.approx(10000.0)


# ---------------------------------------------------------------------------
# VulnerabilityDisclosureManager — SLA compliance
# ---------------------------------------------------------------------------


class TestSLACompliance:
    """Tests for SLA compliance checking."""

    def test_new_report_triage_sla_ok(self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport) -> None:
        manager.submit_report(sample_report)
        compliance = manager.check_sla_compliance("VR-001")
        assert compliance["triage_within_sla"] is True

    def test_resolved_report_all_ok(self, manager: VulnerabilityDisclosureManager, sample_report: VulnReport) -> None:
        manager.submit_report(sample_report)
        manager.triage_report("VR-001")
        manager.mark_fixing("VR-001")
        manager.mark_resolved("VR-001")
        compliance = manager.check_sla_compliance("VR-001")
        assert compliance["fix_within_sla"] is True

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
# BountyScope data model
# ---------------------------------------------------------------------------


class TestBountyScope:
    """Tests for the BountyScope dataclass."""

    def test_default_rewards(self) -> None:
        scope = BountyScope(in_scope=("/api/*",))
        assert scope.rewards["low"] == pytest.approx(100.0)
        assert scope.rewards["critical"] == pytest.approx(5000.0)

    def test_custom_rewards(self) -> None:
        scope = BountyScope(
            in_scope=("/api/*",),
            rewards={"low": 50.0, "high": 3000.0},
        )
        assert scope.rewards.get("low") == pytest.approx(50.0)
        assert scope.rewards.get("high") == pytest.approx(3000.0)

    def test_frozen_scope(self) -> None:
        scope = BountyScope(in_scope=("/api/*",))
        with pytest.raises(AttributeError):
            scope.max_reward = 999999  # type: ignore[misc]
