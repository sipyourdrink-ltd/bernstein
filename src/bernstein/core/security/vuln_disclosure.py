"""Coordinated vulnerability disclosure per the project's SECURITY.md policy.

Provides structured vulnerability report handling, triage workflows,
coordinated disclosure timelines, and RFC 9116 security.txt generation.

Recognition is non-monetary and uniform: every valid, first-reported issue
gets the same credit - name/handle/link in the release notes of the fixing
release, plus reporter credit on the published GitHub Security Advisory
(which may carry a CVE). There is no severity-based recognition ladder and
no bug bounty.

There is no fix deadline. Severity sets the order reports are worked on, not
a promised delivery date. The only date this module commits to is the first
substantive response, due within ``response_sla_hours`` of submission (up to
90 days per SECURITY.md), and the public-disclosure date, which the reporter
may exercise unilaterally regardless of fix status.

Usage::

    from bernstein.core.security.vuln_disclosure import (
        VulnReport,
        DisclosureScope,
        VulnerabilityDisclosureManager,
        generate_security_txt,
    )

    manager = VulnerabilityDisclosureManager()
    report = VulnReport(
        report_id="VR-001",
        severity="high",
        title="SQL Injection in login endpoint",
        description="User input is concatenated directly into SQL query...",
        reporter_email="researcher@example.com",
    )
    tracking_id = manager.submit_report(report)
    print(tracking_id)  # "VR-001"

    triaged = manager.triage_report("VR-001", assessed_severity="critical")
    timeline = manager.generate_disclosure_timeline("VR-001")
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class Severity(StrEnum):
    """Standard vulnerability severity levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ReportStatus(StrEnum):
    """Lifecycle status of a vulnerability report."""

    NEW = "new"
    TRIAGED = "triaged"
    FIXING = "fixing"
    RESOLVED = "resolved"
    DISCLOSED = "disclosed"
    REJECTED = "rejected"


class RecognitionTier(StrEnum):
    """Non-monetary recognition granted for a disclosure.

    Recognition does not vary by severity: every valid, first-reported issue
    receives the same credit (release notes plus GitHub Security Advisory
    credit). A report that is rejected, or that is not the first report of
    a duplicate, receives none.
    """

    NONE = "none"
    CREDITED = "credited"


@dataclass(frozen=True)
class VulnReport:
    """A vulnerability report submitted by a security researcher.

    Attributes:
        report_id: Unique tracking identifier for this report.
        severity: Assessed or self-reported severity level.
        title: Short human-readable summary of the vulnerability.
        description: Detailed technical description of the issue.
        reporter_email: Contact email for the researcher.
        submitted_at: Timestamp when the report was submitted.
        status: Current lifecycle status of the report.
        cve_id: Associated CVE identifier, if assigned.
        affected_components: List of components or endpoints affected.
    """

    report_id: str
    severity: str  # Severity value
    title: str
    description: str
    reporter_email: str
    submitted_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: str = ReportStatus.NEW.value
    cve_id: str | None = None
    affected_components: tuple[str, ...] = ()


@dataclass(frozen=True)
class DisclosureScope:
    """Scope definition for the coordinated disclosure program.

    Attributes:
        in_scope: Components or endpoints that are in scope.
        out_of_scope: Components explicitly out of scope.
        response_sla_hours: Maximum hours before the first substantive
            response. SECURITY.md states this as "up to 90 days"
            (``90 * 24 = 2160`` hours); this is a maintained-alone project's
            outer bound, not a target most reports will take that long.
    """

    in_scope: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    response_sla_hours: int = 2160


@dataclass(frozen=True)
class DisclosureTimeline:
    """Coordinated disclosure timeline milestones.

    There is no fix-deadline milestone: SECURITY.md promises a first
    response, not a fix date.

    Attributes:
        report_id: The associated vulnerability report.
        vendor_notified: When the vendor was first notified.
        first_response_due: Deadline for the first substantive response.
        public_disclosure: Date the reporter may disclose unilaterally,
            with or without a shipped fix.
        milestones: All milestone dates with labels.
    """

    report_id: str
    vendor_notified: datetime
    first_response_due: datetime
    public_disclosure: datetime
    milestones: dict[str, datetime] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# security.txt generation (RFC 9116)
# ---------------------------------------------------------------------------

_SECURITY_TXT_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def generate_security_txt(
    contact: str,
    policy_url: str,
    *,
    expires: datetime | None = None,
    encryption: str | None = None,
    acknowledgments: str | None = None,
    preferred_languages: str | None = None,
    hiring: str | None = None,
) -> str:
    """Generate ``security.txt`` content conforming to RFC 9116.

    Args:
        contact: Contact URI (e.g. ``mailto:security@example.com`` or
                 ``https://github.com/example/repo/security/advisories``).
        policy_url: URL to the full security/vulnerability policy.
        expires: Date when this file expires (ISO 8601). Defaults to 1 year.
        encryption: URL pointing to a PGP public key for encrypted reports.
        acknowledgments: URL to a page acknowledging security researchers.
        preferred_languages: Comma-separated list of accepted languages.
        hiring: URL to security-related job listings.

    Returns:
        The complete ``security.txt`` file content as a string.

    Raises:
        ValueError: If *contact* or *policy_url* are empty.
    """
    if not contact:
        raise ValueError("contact is required")
    if not policy_url:
        raise ValueError("policy_url is required")

    if expires is None:
        expires = datetime.now(UTC) + timedelta(days=365)

    lines: list[str] = [
        f"Contact: {contact}",
        f"Policy: {policy_url}",
        f"Expires: {expires.strftime('%Y-%m-%dT%H:%M:%S.000Z')}",
    ]

    if encryption:
        lines.append(f"Encryption: {encryption}")
    if acknowledgments:
        lines.append(f"Acknowledgments: {acknowledgments}")
    if preferred_languages:
        lines.append(f"Preferred-Languages: {preferred_languages}")
    if hiring:
        lines.append(f"Hiring: {hiring}")

    lines.append("")  # trailing newline
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Vulnerability Disclosure Manager
# ---------------------------------------------------------------------------


class VulnerabilityDisclosureManager:
    """Manages the full lifecycle of vulnerability disclosure.

    Handles report submission, triage, fix tracking, and coordinated
    disclosure timelines. Recognition is a single uniform credit path -
    there is no severity-based tier to classify into.

    Usage::

        mgr = VulnerabilityDisclosureManager(
            scope=DisclosureScope(
                in_scope=("/api/v1/*", "/api/v2/*"),
            ),
        )
        report_id = mgr.submit_report(report)
        mgr.triage_report(report_id, assessed_severity="high")
    """

    def __init__(
        self,
        scope: DisclosureScope | None = None,
        *,
        disclosure_days: int = 90,
    ) -> None:
        self._scope = scope or DisclosureScope()
        self._reports: dict[str, VulnReport] = {}
        self._disclosure_days = disclosure_days

    @property
    def scope(self) -> DisclosureScope:
        """Return the current disclosure scope configuration."""
        return self._scope

    @property
    def reports(self) -> dict[str, VulnReport]:
        """Return a shallow copy of all tracked reports."""
        return self._reports.copy()

    # -- Report submission ---------------------------------------------------

    def submit_report(self, report: VulnReport) -> str:
        """Register a vulnerability report and return its tracking ID.

        If the report already has a ``report_id`` it is used directly;
        otherwise a new one is generated using a short UUID.

        Args:
            report: The vulnerability report to register.

        Returns:
            The tracking ID for the submitted report.
        """
        report_id = report.report_id
        if not report_id:
            report_id = f"VR-{uuid.uuid4().hex[:8].upper()}"

        # Create a new frozen report with ensured status=new
        updated = VulnReport(
            report_id=report_id,
            severity=report.severity,
            title=report.title,
            description=report.description,
            reporter_email=report.reporter_email,
            submitted_at=report.submitted_at,
            status=ReportStatus.NEW.value,
            cve_id=report.cve_id,
            affected_components=report.affected_components,
        )
        self._reports[report_id] = updated
        return report_id

    # -- Triage ---------------------------------------------------------------

    def triage_report(
        self,
        report_id: str,
        *,
        assessed_severity: str | None = None,
        cve_id: str | None = None,
    ) -> VulnReport:
        """Triaged a vulnerability report with an assessed severity.

        Args:
            report_id: Tracking ID of the report to triage.
            assessed_severity: Override the severity assessment.
            cve_id: Assign a CVE identifier.

        Returns:
            The updated :class:`VulnReport`.

        Raises:
            KeyError: If *report_id* does not exist.
        """
        report = self._reports.get(report_id)
        if report is None:
            raise KeyError(f"Report {report_id!r} not found")

        severity = assessed_severity or report.severity

        # Validate severity
        try:
            Severity(severity)
        except ValueError:
            valid = [s.value for s in Severity]
            raise ValueError(f"Invalid severity {severity!r}; must be one of {valid}") from None

        updated = VulnReport(
            report_id=report.report_id,
            severity=severity,
            title=report.title,
            description=report.description,
            reporter_email=report.reporter_email,
            submitted_at=report.submitted_at,
            status=ReportStatus.TRIAGED.value,
            cve_id=cve_id or report.cve_id,
            affected_components=report.affected_components,
        )
        self._reports[report_id] = updated
        return updated

    # -- Fix tracking ---------------------------------------------------------

    def mark_fixing(self, report_id: str) -> VulnReport:
        """Mark a report as currently being fixed.

        Args:
            report_id: Tracking ID of the report.

        Returns:
            The updated report.

        Raises:
            KeyError: If the report does not exist.
        """
        report = self._reports.get(report_id)
        if report is None:
            raise KeyError(f"Report {report_id!r} not found")

        updated = VulnReport(
            report_id=report.report_id,
            severity=report.severity,
            title=report.title,
            description=report.description,
            reporter_email=report.reporter_email,
            submitted_at=report.submitted_at,
            status=ReportStatus.FIXING.value,
            cve_id=report.cve_id,
            affected_components=report.affected_components,
        )
        self._reports[report_id] = updated
        return updated

    def mark_resolved(self, report_id: str) -> VulnReport:
        """Mark a report as resolved (fix deployed).

        Args:
            report_id: Tracking ID of the report.

        Returns:
            The updated report.

        Raises:
            KeyError: If the report does not exist.
        """
        report = self._reports.get(report_id)
        if report is None:
            raise KeyError(f"Report {report_id!r} not found")

        updated = VulnReport(
            report_id=report.report_id,
            severity=report.severity,
            title=report.title,
            description=report.description,
            reporter_email=report.reporter_email,
            submitted_at=report.submitted_at,
            status=ReportStatus.RESOLVED.value,
            cve_id=report.cve_id,
            affected_components=report.affected_components,
        )
        self._reports[report_id] = updated
        return updated

    # -- Disclosure timeline --------------------------------------------------

    def generate_disclosure_timeline(self, report_id: str) -> DisclosureTimeline:
        """Generate a coordinated disclosure timeline for a report.

        Computes milestones directly from the submission date. There is no
        fix-deadline milestone: SECURITY.md promises a first response, not a
        fix date.

        1. **Vendor notified** - at submission time.
        2. **First response due** - within ``response_sla_hours`` of
           submission.
        3. **Public disclosure** - ``disclosure_days`` after submission,
           available to the reporter regardless of fix status.

        Args:
            report_id: Tracking ID of the report.

        Returns:
            A :class:`DisclosureTimeline` with all milestone dates.

        Raises:
            KeyError: If the report does not exist.
        """
        report = self._reports.get(report_id)
        if report is None:
            raise KeyError(f"Report {report_id!r} not found")

        submitted = report.submitted_at
        first_response_due = submitted + timedelta(hours=self._scope.response_sla_hours)
        public_disclosure = submitted + timedelta(days=self._disclosure_days)

        milestones = {
            "submitted": submitted,
            "vendor_notified": submitted,
            "first_response_due": first_response_due,
            "public_disclosure": public_disclosure,
        }

        return DisclosureTimeline(
            report_id=report_id,
            vendor_notified=submitted,
            first_response_due=first_response_due,
            public_disclosure=public_disclosure,
            milestones=milestones,
        )

    # -- Recognition classification -------------------------------------------

    def classify_recognition(self, report_id: str) -> str:
        """Classify a report into its (uniform) recognition state.

        Recognition does not depend on severity: every report that has not
        been rejected is credited the same way (release notes plus GitHub
        Security Advisory credit). A rejected report gets none.

        Args:
            report_id: Tracking ID of the report.

        Returns:
            The recognition tier value (see :class:`RecognitionTier`).

        Raises:
            KeyError: If the report does not exist.
        """
        report = self._reports.get(report_id)
        if report is None:
            raise KeyError(f"Report {report_id!r} not found")

        if report.status == ReportStatus.REJECTED.value:
            return RecognitionTier.NONE.value
        return RecognitionTier.CREDITED.value

    # -- SLA compliance -------------------------------------------------------

    def check_sla_compliance(self, report_id: str) -> dict[str, bool]:
        """Check whether the first-response SLA has been met.

        There is no fix deadline to check against - only the first
        substantive response is a commitment SECURITY.md makes.

        Args:
            report_id: Tracking ID of the report.

        Returns:
            Dictionary with key ``response_within_sla``: ``True`` if the
            report has moved past ``new`` (i.e. a response has occurred) or
            the response SLA window has not yet elapsed.

        Raises:
            KeyError: If the report does not exist.
        """
        report = self._reports.get(report_id)
        if report is None:
            raise KeyError(f"Report {report_id!r} not found")

        now = datetime.now(UTC)
        response_deadline = report.submitted_at + timedelta(hours=self._scope.response_sla_hours)
        responded = report.status != ReportStatus.NEW.value

        return {
            "response_within_sla": responded or now <= response_deadline,
        }
