"""ENT-004: SOC 2 compliance reporting.

Transforms the raw audit export into a structured compliance package with:
- Control mappings (SOC 2 Type II trust service criteria)
- Evidence summaries per control
- Merkle root attestation
- JSON-serializable report for auditors
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

_CC9_2 = "CC9.2"

_CC9_1 = "CC9.1"

_CC7_1 = "CC7.1"

logger = logging.getLogger(__name__)

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


# ---------------------------------------------------------------------------
# SOC 2 Trust Service Criteria (TSC) control definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SOC2Control:
    """A SOC 2 Trust Service Criteria control.

    Attributes:
        control_id: TSC identifier (e.g. ``CC6.1``).
        category: Trust service category (Security, Availability, etc.).
        title: Short title of the control.
        description: Full description of the control requirement.
        evidence_types: Types of evidence that satisfy this control.
    """

    control_id: str
    category: str
    title: str
    description: str
    evidence_types: tuple[str, ...] = ()


# Standard SOC 2 Type II controls relevant to Bernstein.
SOC2_CONTROLS: tuple[SOC2Control, ...] = (
    SOC2Control(
        control_id="CC6.1",
        category="Security",
        title="Logical Access Controls",
        description=(
            "The entity implements logical access security measures to protect "
            "against unauthorized access to information assets."
        ),
        evidence_types=("audit_log", "auth_config"),
    ),
    SOC2Control(
        control_id="CC6.2",
        category="Security",
        title="Authentication Mechanisms",
        description=(
            "Prior to issuing system credentials and granting system access, "
            "the entity registers and authorizes new users."
        ),
        evidence_types=("auth_config", "cluster_auth"),
    ),
    SOC2Control(
        control_id="CC6.3",
        category="Security",
        title="Authorization Controls",
        description=(
            "The entity authorizes, modifies, or removes access to data, "
            "software, functions, and other protected information assets."
        ),
        evidence_types=("audit_log", "permission_config"),
    ),
    SOC2Control(
        control_id=_CC7_1,
        category="Security",
        title="Change Management",
        description=(
            "The entity uses a defined change management process for "
            "modifications to infrastructure, data, software, and procedures."
        ),
        evidence_types=("wal", "audit_log"),
    ),
    SOC2Control(
        control_id="CC7.2",
        category="Security",
        title="System Monitoring",
        description=("The entity monitors system components and the operation of those components for anomalies."),
        evidence_types=("metrics", "sla_monitoring"),
    ),
    SOC2Control(
        control_id="CC8.1",
        category="Availability",
        title="Capacity Management",
        description=("The entity maintains, monitors, and evaluates current processing capacity to manage demand."),
        evidence_types=("metrics", "sla_monitoring"),
    ),
    SOC2Control(
        control_id=_CC9_1,
        category="Processing Integrity",
        title="Processing Accuracy",
        description=("The entity implements quality assurance procedures to verify processing integrity."),
        evidence_types=("merkle_seal", "hmac_verification"),
    ),
    SOC2Control(
        control_id=_CC9_2,
        category="Processing Integrity",
        title="Data Integrity",
        description=(
            "The entity implements procedures to ensure completeness, "
            "accuracy, timeliness, and authorization of system processing."
        ),
        evidence_types=("merkle_seal", "wal", "hmac_verification"),
    ),
)


@dataclass(frozen=True)
class EvidenceSummary:
    """Summary of evidence collected for a specific control.

    Attributes:
        control_id: TSC control identifier.
        evidence_type: Type of evidence (e.g. ``audit_log``, ``wal``).
        description: Human-readable description of the evidence.
        file_count: Number of evidence files collected.
        entry_count: Number of entries/records in the evidence.
        integrity_verified: Whether integrity was verified (HMAC/Merkle).
        details: Additional structured details.
    """

    control_id: str
    evidence_type: str
    description: str
    file_count: int = 0
    entry_count: int = 0
    integrity_verified: bool = False
    details: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class MerkleAttestation:
    """Merkle root attestation for the compliance package.

    Attributes:
        root_hash: Merkle tree root hash.
        leaf_count: Number of leaves (files) in the tree.
        algorithm: Hash algorithm used.
        attested_at: ISO 8601 timestamp.
        seal_path: Path to the seal file, if available.
    """

    root_hash: str
    leaf_count: int
    algorithm: str = "sha256"
    attested_at: str = ""
    seal_path: str = ""


@dataclass
class SOC2ComplianceReport:
    """Structured SOC 2 compliance report.

    Attributes:
        period: Reporting period (e.g. ``Q1-2026``).
        period_start: ISO date for period start.
        period_end: ISO date for period end.
        generated_at: ISO 8601 timestamp of report generation.
        controls: List of applicable controls.
        evidence: List of evidence summaries.
        merkle_attestation: Optional Merkle root attestation.
        hmac_chain_valid: Whether the HMAC chain was verified successfully.
        overall_status: One of ``compliant``, ``partial``, ``non_compliant``.
        package_hash: SHA-256 hash of the serialized report content.
    """

    period: str
    period_start: str
    period_end: str
    generated_at: str = ""
    controls: list[SOC2Control] = field(default_factory=list[SOC2Control])
    evidence: list[EvidenceSummary] = field(default_factory=list[EvidenceSummary])
    merkle_attestation: MerkleAttestation | None = None
    hmac_chain_valid: bool | None = None
    overall_status: str = "partial"
    package_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "report_type": "soc2_compliance",
            "period": self.period,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "generated_at": self.generated_at,
            "overall_status": self.overall_status,
            "controls": [
                {
                    "control_id": c.control_id,
                    "category": c.category,
                    "title": c.title,
                    "description": c.description,
                    "evidence_types": list(c.evidence_types),
                }
                for c in self.controls
            ],
            "evidence": [
                {
                    "control_id": e.control_id,
                    "evidence_type": e.evidence_type,
                    "description": e.description,
                    "file_count": e.file_count,
                    "entry_count": e.entry_count,
                    "integrity_verified": e.integrity_verified,
                    "details": e.details,
                }
                for e in self.evidence
            ],
            "merkle_attestation": (
                {
                    "root_hash": self.merkle_attestation.root_hash,
                    "leaf_count": self.merkle_attestation.leaf_count,
                    "algorithm": self.merkle_attestation.algorithm,
                    "attested_at": self.merkle_attestation.attested_at,
                    "seal_path": self.merkle_attestation.seal_path,
                }
                if self.merkle_attestation
                else None
            ),
            "hmac_chain_valid": self.hmac_chain_valid,
            "package_hash": self.package_hash,
        }


def _count_jsonl_entries(directory: Path, date_start: str, date_end: str) -> tuple[int, int]:
    """Count JSONL files and entries within a date range.

    Args:
        directory: Directory containing ``YYYY-MM-DD.jsonl`` files.
        date_start: ISO date lower bound (inclusive).
        date_end: ISO date upper bound (inclusive).

    Returns:
        Tuple of (file_count, entry_count).
    """
    file_count = 0
    entry_count = 0
    for log_path in sorted(directory.glob("*.jsonl")):
        file_date = log_path.stem
        if date_start <= file_date <= date_end:
            file_count += 1
            for line in log_path.read_text().splitlines():
                if line.strip():
                    entry_count += 1
    return file_count, entry_count


def _collect_audit_evidence(
    audit_dir: Path, period_start: str, period_end: str, evidence: list[EvidenceSummary]
) -> None:
    """Collect audit log evidence entries."""
    if not audit_dir.is_dir():
        return
    file_count, entry_count = _count_jsonl_entries(audit_dir, period_start, period_end)
    if file_count <= 0:
        return
    for control_id, desc in [
        ("CC6.1", "HMAC-chained audit event log entries"),
        ("CC6.3", "Authorization change audit trail"),
        (_CC7_1, "Change management audit trail"),
    ]:
        evidence.append(
            EvidenceSummary(
                control_id=control_id,
                evidence_type="audit_log",
                description=desc,
                file_count=file_count,
                entry_count=entry_count,
            )
        )


def _collect_hmac_evidence(audit_dir: Path, evidence: list[EvidenceSummary]) -> bool | None:
    """Verify HMAC chain and append evidence. Returns validity or None."""
    if not audit_dir.is_dir():
        return None
    try:
        from bernstein.core.security.audit import AuditLog

        audit_log = AuditLog(audit_dir)
        valid, audit_errors = audit_log.verify()
        evidence.append(
            EvidenceSummary(
                control_id=_CC9_1,
                evidence_type="hmac_verification",
                description="HMAC chain integrity verification",
                integrity_verified=valid,
                details={"errors": audit_errors} if audit_errors else {},
            )
        )
        return valid
    except Exception as exc:
        evidence.append(
            EvidenceSummary(
                control_id=_CC9_1,
                evidence_type="hmac_verification",
                description="HMAC chain verification failed",
                integrity_verified=False,
                details={"error": str(exc)},
            )
        )
        return False


def _collect_merkle_evidence(merkle_dir: Path, evidence: list[EvidenceSummary]) -> MerkleAttestation | None:
    """Collect Merkle seal evidence and return attestation if available."""
    if not merkle_dir.is_dir():
        return None
    from bernstein.core.merkle import load_latest_seal

    loaded = load_latest_seal(merkle_dir)
    if loaded is None:
        return None
    seal, seal_path = loaded
    seal_root = str(seal.get("root_hash", ""))
    seal_leaves = int(str(seal.get("leaf_count", 0)))
    evidence.append(
        EvidenceSummary(
            control_id=_CC9_2,
            evidence_type="merkle_seal",
            description="Merkle tree integrity attestation",
            integrity_verified=True,
            details={"root_hash": seal_root, "leaf_count": seal_leaves},
        )
    )
    return MerkleAttestation(
        root_hash=seal_root,
        leaf_count=seal_leaves,
        algorithm="sha256",
        attested_at=str(seal.get("sealed_at_iso", "")),
        seal_path=str(seal_path),
    )


def _collect_wal_evidence(wal_dir: Path, evidence: list[EvidenceSummary]) -> None:
    """Collect WAL evidence entries."""
    if not wal_dir.is_dir():
        return
    wal_files = list(wal_dir.glob("*.wal.jsonl"))
    if not wal_files:
        return
    wal_entries = sum(1 for wf in wal_files for line in wf.read_text().splitlines() if line.strip())
    for control_id, desc in [
        (_CC7_1, "Write-ahead log decision records"),
        (_CC9_2, "Decision integrity via hash-chained WAL"),
    ]:
        evidence.append(
            EvidenceSummary(
                control_id=control_id,
                evidence_type="wal",
                description=desc,
                file_count=len(wal_files),
                entry_count=wal_entries,
            )
        )


def _collect_metrics_evidence(metrics_dir: Path, evidence: list[EvidenceSummary]) -> None:
    """Collect metrics evidence entries."""
    if not metrics_dir.is_dir():
        return
    metrics_files = list(metrics_dir.glob("*"))
    if not metrics_files:
        return
    for control_id, desc in [
        ("CC7.2", "System monitoring and metrics data"),
        ("CC8.1", "Capacity monitoring data"),
    ]:
        evidence.append(
            EvidenceSummary(
                control_id=control_id,
                evidence_type="metrics",
                description=desc,
                file_count=len(metrics_files),
            )
        )


def generate_soc2_report(
    sdd_dir: Path,
    period: str,
    period_start: str,
    period_end: str,
) -> SOC2ComplianceReport:
    """Generate a SOC 2 compliance report for the given period.

    Collects evidence from audit logs, WAL, metrics, and Merkle seals.
    Maps evidence to SOC 2 controls and computes overall compliance status.

    Args:
        sdd_dir: Path to the ``.sdd`` directory.
        period: Period label (e.g. ``Q1-2026``).
        period_start: ISO date for period start.
        period_end: ISO date for period end.

    Returns:
        Populated SOC2ComplianceReport.
    """
    report = SOC2ComplianceReport(
        period=period,
        period_start=period_start,
        period_end=period_end,
        generated_at=time.strftime(_ISO_FMT, time.gmtime()),
        controls=list(SOC2_CONTROLS),
    )

    evidence: list[EvidenceSummary] = []
    audit_dir = sdd_dir / "audit"

    _collect_audit_evidence(audit_dir, period_start, period_end, evidence)
    hmac_valid = _collect_hmac_evidence(audit_dir, evidence)
    report.hmac_chain_valid = hmac_valid

    merkle_dir = audit_dir / "merkle" if audit_dir.is_dir() else sdd_dir / "audit" / "merkle"
    report.merkle_attestation = _collect_merkle_evidence(merkle_dir, evidence)

    _collect_wal_evidence(sdd_dir / "runtime" / "wal", evidence)
    _collect_metrics_evidence(sdd_dir / "metrics", evidence)

    report.evidence = evidence

    # --- Determine overall compliance status ---
    controls_with_evidence: set[str] = {e.control_id for e in evidence}
    all_control_ids = {c.control_id for c in SOC2_CONTROLS}

    if controls_with_evidence >= all_control_ids and hmac_valid is True:
        report.overall_status = "compliant"
    elif controls_with_evidence:
        report.overall_status = "partial"
    else:
        report.overall_status = "non_compliant"

    content = json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":"))
    report.package_hash = hashlib.sha256(content.encode()).hexdigest()

    return report


def save_soc2_report(report: SOC2ComplianceReport, output_dir: Path) -> Path:
    """Write the SOC 2 report to disk as JSON.

    Args:
        report: The compliance report.
        output_dir: Directory to write the report into.

    Returns:
        Path to the written report file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"soc2-report-{report.period}.json"
    path = output_dir / filename
    path.write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    logger.info("SOC 2 report saved: %s (status=%s)", path, report.overall_status)
    return path
