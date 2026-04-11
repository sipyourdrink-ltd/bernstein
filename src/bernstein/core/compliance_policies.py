"""Compliance-as-code policy library with pre-built rules.

Provides 50+ OPA/Rego-style compliance policies covering SOC 2, ISO 27001,
PCI DSS, and NIST 800-53 controls.  Policies are evaluated against a
:class:`PolicyInput` snapshot that describes the current Bernstein runtime
configuration.

Usage::

    from bernstein.core.compliance_policies import (
        ComplianceFramework,
        CompliancePolicyLibrary,
        PolicyInput,
        evaluate_framework,
    )

    # Build a snapshot of the running configuration
    inp = PolicyInput(
        audit_logging=True,
        audit_hmac_chain=True,
        sandbox_enabled=True,
        seccomp_enabled=True,
        tls_enforced=True,
        secrets_rotation_days=90,
    )

    # Evaluate all SOC 2 policies
    results = evaluate_framework(ComplianceFramework.SOC2, inp)
    for r in results:
        print(r.policy_id, r.passed, r.finding)

Activating a framework persists a YAML file under ``.sdd/compliance/`` so
subsequent runs can load and re-evaluate it::

    lib = CompliancePolicyLibrary()
    lib.enable(ComplianceFramework.SOC2, config_dir=Path(".sdd/compliance"))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ComplianceFramework(StrEnum):
    """Supported compliance frameworks."""

    SOC2 = "soc2"
    ISO27001 = "iso27001"
    PCI_DSS = "pci_dss"
    NIST_800_53 = "nist_800_53"


class PolicySeverity(StrEnum):
    """Severity levels for policy findings."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFORMATIONAL = "informational"


# ---------------------------------------------------------------------------
# Policy input (runtime snapshot)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyInput:
    """Snapshot of the Bernstein runtime configuration evaluated by policies.

    All fields default to their *least-secure* value so that omitting a field
    results in a policy finding rather than a false-negative pass.

    Attributes:
        audit_logging: Append-only JSONL audit log is enabled.
        audit_hmac_chain: HMAC-chained tamper-evident audit log is active.
        audit_retention_days: How long audit logs are kept (days).
        sandbox_enabled: Agents run inside containers.
        seccomp_enabled: Seccomp-BPF profile is applied to agent containers.
        network_isolation: Agent containers have network_mode=none or bridge.
        read_only_rootfs: Container root filesystem is read-only.
        tls_enforced: All external connections use TLS 1.2+ only.
        secrets_rotation_days: Maximum age of secrets before required rotation.
        mfa_enabled: Multi-factor authentication is enforced for operators.
        rbac_enabled: Role-based access control is configured.
        least_privilege_caps: Container capabilities are minimally scoped.
        vulnerability_scanning: Dependency and container image scanning runs.
        sbom_enabled: Software Bill of Materials is generated per task.
        change_approval_gates: Human approval required before task execution.
        incident_response_plan: Incident response runbook exists.
        data_classification: Sensitive data classification is implemented.
        encrypt_at_rest: State files are encrypted at rest.
        encrypt_in_transit: Data in transit is encrypted end-to-end.
        log_integrity: Log files are protected against tampering.
        access_review_days: Frequency of access rights reviews (days).
        password_min_length: Minimum credential length enforced.
        session_timeout_minutes: Operator session timeout in minutes.
        agent_token_expiry_hours: Agent JWT / token expiry in hours.
        rate_limiting_enabled: API rate limiting is active.
        waf_enabled: Web application firewall is deployed.
        backup_enabled: Backups are configured and tested.
        backup_encryption: Backups are encrypted.
        dr_rto_hours: Recovery Time Objective in hours.
        code_signing: Artifact signing is enforced in CI/CD.
        dependency_pinning: Dependencies are pinned to exact versions.
        sast_in_ci: Static analysis (SAST) runs in CI pipeline.
        phi_detection: PHI / PII detection is active (HIPAA context).
        data_residency_enforced: Data is restricted to a specific region.
        custom: Additional key-value pairs for framework-specific checks.
    """

    audit_logging: bool = False
    audit_hmac_chain: bool = False
    audit_retention_days: int = 0
    sandbox_enabled: bool = False
    seccomp_enabled: bool = False
    network_isolation: bool = False
    read_only_rootfs: bool = False
    tls_enforced: bool = False
    secrets_rotation_days: int = 999
    mfa_enabled: bool = False
    rbac_enabled: bool = False
    least_privilege_caps: bool = False
    vulnerability_scanning: bool = False
    sbom_enabled: bool = False
    change_approval_gates: bool = False
    incident_response_plan: bool = False
    data_classification: bool = False
    encrypt_at_rest: bool = False
    encrypt_in_transit: bool = False
    log_integrity: bool = False
    access_review_days: int = 999
    password_min_length: int = 0
    session_timeout_minutes: int = 9999
    agent_token_expiry_hours: int = 9999
    rate_limiting_enabled: bool = False
    waf_enabled: bool = False
    backup_enabled: bool = False
    backup_encryption: bool = False
    dr_rto_hours: int = 9999
    code_signing: bool = False
    dependency_pinning: bool = False
    sast_in_ci: bool = False
    phi_detection: bool = False
    data_residency_enforced: bool = False
    custom: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Policy definition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompliancePolicy:
    """A single compliance policy rule.

    Attributes:
        policy_id: Unique identifier (e.g. ``soc2-cc6-01``).
        name: Short human-readable policy name.
        framework: Compliance framework this policy belongs to.
        control_id: Standard control reference (e.g. ``CC6.1``, ``A.9.1.1``).
        description: What this policy checks and why it matters.
        severity: Severity if the policy fails.
        rego_rule: OPA Rego rule text for the policy (informational / export).
        check: Python callable ``(PolicyInput) -> bool`` — ``True`` = passing.
        remediation: Brief remediation guidance.
    """

    policy_id: str
    name: str
    framework: ComplianceFramework
    control_id: str
    description: str
    severity: PolicySeverity
    rego_rule: str
    check: Any  # Callable[[PolicyInput], bool] — not in frozen field hint
    remediation: str


# ---------------------------------------------------------------------------
# Policy evaluation result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyResult:
    """Result of evaluating a single :class:`CompliancePolicy`.

    Attributes:
        policy_id: ID of the evaluated policy.
        name: Policy name.
        framework: Framework the policy belongs to.
        control_id: Standard control reference.
        severity: Policy severity.
        passed: Whether the policy check passed.
        finding: Human-readable finding description if the check failed.
        remediation: Suggested remediation.
    """

    policy_id: str
    name: str
    framework: ComplianceFramework
    control_id: str
    severity: PolicySeverity
    passed: bool
    finding: str
    remediation: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {
            "policy_id": self.policy_id,
            "name": self.name,
            "framework": self.framework.value,
            "control_id": self.control_id,
            "severity": self.severity.value,
            "passed": self.passed,
            "finding": self.finding,
            "remediation": self.remediation,
        }


# ---------------------------------------------------------------------------
# Pre-built policy definitions (50+ rules)
# ---------------------------------------------------------------------------


def _p(
    policy_id: str,
    name: str,
    framework: ComplianceFramework,
    control_id: str,
    description: str,
    severity: PolicySeverity,
    rego: str,
    check: Any,
    remediation: str,
) -> CompliancePolicy:
    return CompliancePolicy(
        policy_id=policy_id,
        name=name,
        framework=framework,
        control_id=control_id,
        description=description,
        severity=severity,
        rego_rule=rego,
        check=check,
        remediation=remediation,
    )


_F = ComplianceFramework
_S = PolicySeverity

# ---------------------------------------------------------------------------
# SOC 2 — Trust Service Criteria
# ---------------------------------------------------------------------------

_SOC2_POLICIES: list[CompliancePolicy] = [
    _p(
        "soc2-cc6-01",
        "Logical Access Controls",
        _F.SOC2,
        "CC6.1",
        "Access to systems is restricted using RBAC and MFA.",
        _S.CRITICAL,
        """package bernstein.soc2.cc6_01
default allow = false
allow {
    input.rbac_enabled == true
    input.mfa_enabled == true
}""",
        lambda i: i.rbac_enabled and i.mfa_enabled,
        "Enable RBAC and MFA. Set rbac_enabled=true and mfa_enabled=true in compliance config.",
    ),
    _p(
        "soc2-cc6-02",
        "Credential Rotation",
        _F.SOC2,
        "CC6.1",
        "Secrets and credentials are rotated at least every 90 days.",
        _S.HIGH,
        """package bernstein.soc2.cc6_02
default allow = false
allow { input.secrets_rotation_days <= 90 }""",
        lambda i: i.secrets_rotation_days <= 90,
        "Configure secrets rotation to ≤90 days. Set secrets_rotation_days=90.",
    ),
    _p(
        "soc2-cc6-03",
        "Session Timeout",
        _F.SOC2,
        "CC6.1",
        "Operator sessions expire after at most 60 minutes of inactivity.",
        _S.MEDIUM,
        """package bernstein.soc2.cc6_03
default allow = false
allow { input.session_timeout_minutes <= 60 }""",
        lambda i: i.session_timeout_minutes <= 60,
        "Set session_timeout_minutes=60 in the server configuration.",
    ),
    _p(
        "soc2-cc6-04",
        "Agent Token Expiry",
        _F.SOC2,
        "CC6.1",
        "Agent tokens expire within 24 hours.",
        _S.HIGH,
        """package bernstein.soc2.cc6_04
default allow = false
allow { input.agent_token_expiry_hours <= 24 }""",
        lambda i: i.agent_token_expiry_hours <= 24,
        "Set agent_token_expiry_hours=24. Rotate agent tokens per run.",
    ),
    _p(
        "soc2-cc6-05",
        "Minimum Credential Length",
        _F.SOC2,
        "CC6.1",
        "Passwords and passphrases must be at least 12 characters.",
        _S.MEDIUM,
        """package bernstein.soc2.cc6_05
default allow = false
allow { input.password_min_length >= 12 }""",
        lambda i: i.password_min_length >= 12,
        "Enforce password_min_length=12 in the authentication configuration.",
    ),
    _p(
        "soc2-cc7-01",
        "Security Event Audit Logging",
        _F.SOC2,
        "CC7.1",
        "All security-relevant events are captured in an append-only audit log.",
        _S.CRITICAL,
        """package bernstein.soc2.cc7_01
default allow = false
allow { input.audit_logging == true }""",
        lambda i: i.audit_logging,
        "Enable audit_logging=true. Use CompliancePreset.STANDARD or higher.",
    ),
    _p(
        "soc2-cc7-02",
        "Tamper-Evident Audit Log",
        _F.SOC2,
        "CC7.2",
        "Audit log entries are HMAC-chained so tampering is detectable.",
        _S.HIGH,
        """package bernstein.soc2.cc7_02
default allow = false
allow {
    input.audit_logging == true
    input.audit_hmac_chain == true
}""",
        lambda i: i.audit_logging and i.audit_hmac_chain,
        "Enable audit_hmac_chain=true. CompliancePreset.REGULATED enables this.",
    ),
    _p(
        "soc2-cc7-03",
        "Audit Log Retention",
        _F.SOC2,
        "CC7.2",
        "Audit logs are retained for at least 365 days.",
        _S.MEDIUM,
        """package bernstein.soc2.cc7_03
default allow = false
allow { input.audit_retention_days >= 365 }""",
        lambda i: i.audit_retention_days >= 365,
        "Set audit_retention_days=365 in the compliance configuration.",
    ),
    _p(
        "soc2-cc7-04",
        "Log Integrity Protection",
        _F.SOC2,
        "CC7.2",
        "Log files are protected against unauthorised modification.",
        _S.HIGH,
        """package bernstein.soc2.cc7_04
default allow = false
allow { input.log_integrity == true }""",
        lambda i: i.log_integrity,
        "Enable log_integrity=true. Use signed WAL (wal_signed=true).",
    ),
    _p(
        "soc2-cc7-05",
        "Incident Response Plan",
        _F.SOC2,
        "CC7.5",
        "A documented incident response plan is in place.",
        _S.HIGH,
        """package bernstein.soc2.cc7_05
default allow = false
allow { input.incident_response_plan == true }""",
        lambda i: i.incident_response_plan,
        "Document and store an incident response runbook under docs/incident-response.md.",
    ),
    _p(
        "soc2-cc8-01",
        "Change Approval Gates",
        _F.SOC2,
        "CC8.1",
        "High-risk tasks require human approval before execution.",
        _S.HIGH,
        """package bernstein.soc2.cc8_01
default allow = false
allow { input.change_approval_gates == true }""",
        lambda i: i.change_approval_gates,
        "Enable change_approval_gates=true in compliance config.",
    ),
    _p(
        "soc2-cc9-01",
        "Agent Sandbox Isolation",
        _F.SOC2,
        "CC9.1",
        "Agents run inside containers to limit blast radius of compromise.",
        _S.HIGH,
        """package bernstein.soc2.cc9_01
default allow = false
allow { input.sandbox_enabled == true }""",
        lambda i: i.sandbox_enabled,
        "Enable container sandboxing. Use --sandbox flag or set sandbox_enabled=true.",
    ),
    _p(
        "soc2-cc9-02",
        "Syscall Filtering",
        _F.SOC2,
        "CC9.1",
        "Seccomp-BPF profiles restrict agent processes to a minimal syscall allowlist.",
        _S.MEDIUM,
        """package bernstein.soc2.cc9_02
default allow = false
allow {
    input.sandbox_enabled == true
    input.seccomp_enabled == true
}""",
        lambda i: i.sandbox_enabled and i.seccomp_enabled,
        "Enable seccomp_enabled=true. Apply AgentSeccompProfile via SecurityProfile.",
    ),
    _p(
        "soc2-cc9-03",
        "Vulnerability Scanning",
        _F.SOC2,
        "CC9.2",
        "Dependencies and container images are scanned for known CVEs.",
        _S.HIGH,
        """package bernstein.soc2.cc9_03
default allow = false
allow { input.vulnerability_scanning == true }""",
        lambda i: i.vulnerability_scanning,
        "Enable Trivy or Grype in CI/CD pipeline. Set vulnerability_scanning=true.",
    ),
    _p(
        "soc2-cc9-04",
        "SBOM Generation",
        _F.SOC2,
        "CC9.2",
        "A Software Bill of Materials is produced for each task run.",
        _S.LOW,
        """package bernstein.soc2.cc9_04
default allow = false
allow { input.sbom_enabled == true }""",
        lambda i: i.sbom_enabled,
        "Enable sbom_enabled=true in compliance config. Uses CycloneDX format.",
    ),
]

# ---------------------------------------------------------------------------
# ISO 27001:2022 — Annex A Controls
# ---------------------------------------------------------------------------

_ISO27001_POLICIES: list[CompliancePolicy] = [
    _p(
        "iso27001-a5-01",
        "Information Security Policy",
        _F.ISO27001,
        "A.5.1",
        "An information security policy is documented and enforced.",
        _S.HIGH,
        """package bernstein.iso27001.a5_01
default allow = false
allow {
    input.audit_logging == true
    input.rbac_enabled == true
}""",
        lambda i: i.audit_logging and i.rbac_enabled,
        "Document a security policy and enforce it through RBAC and audit logging.",
    ),
    _p(
        "iso27001-a8-01",
        "Asset Inventory (SBOM)",
        _F.ISO27001,
        "A.8.1",
        "All software assets are inventoried via SBOM generation.",
        _S.MEDIUM,
        """package bernstein.iso27001.a8_01
default allow = false
allow { input.sbom_enabled == true }""",
        lambda i: i.sbom_enabled,
        "Enable sbom_enabled=true to generate CycloneDX SBOMs per run.",
    ),
    _p(
        "iso27001-a8-02",
        "Data Classification",
        _F.ISO27001,
        "A.8.2",
        "Sensitive data is classified and handled according to its classification.",
        _S.HIGH,
        """package bernstein.iso27001.a8_02
default allow = false
allow { input.data_classification == true }""",
        lambda i: i.data_classification,
        "Implement data_classification=true. Tag tasks containing PII/secrets.",
    ),
    _p(
        "iso27001-a9-01",
        "Access Control Policy",
        _F.ISO27001,
        "A.9.1",
        "Role-based access control enforces least privilege.",
        _S.CRITICAL,
        """package bernstein.iso27001.a9_01
default allow = false
allow { input.rbac_enabled == true }""",
        lambda i: i.rbac_enabled,
        "Enable RBAC. Assign minimal roles to agents and operators.",
    ),
    _p(
        "iso27001-a9-02",
        "User Registration and Deprovisioning",
        _F.ISO27001,
        "A.9.2",
        "Access rights reviews are conducted at regular intervals (≤90 days).",
        _S.MEDIUM,
        """package bernstein.iso27001.a9_02
default allow = false
allow { input.access_review_days <= 90 }""",
        lambda i: i.access_review_days <= 90,
        "Set access_review_days=90. Schedule quarterly access reviews.",
    ),
    _p(
        "iso27001-a9-03",
        "Multi-Factor Authentication",
        _F.ISO27001,
        "A.9.4",
        "MFA is required for all operator accounts.",
        _S.HIGH,
        """package bernstein.iso27001.a9_03
default allow = false
allow { input.mfa_enabled == true }""",
        lambda i: i.mfa_enabled,
        "Enable MFA for all operator identities. Set mfa_enabled=true.",
    ),
    _p(
        "iso27001-a10-01",
        "Cryptographic Controls",
        _F.ISO27001,
        "A.10.1",
        "Encryption is applied to data at rest and in transit.",
        _S.CRITICAL,
        """package bernstein.iso27001.a10_01
default allow = false
allow {
    input.encrypt_at_rest == true
    input.encrypt_in_transit == true
}""",
        lambda i: i.encrypt_at_rest and i.encrypt_in_transit,
        "Enable encrypt_at_rest=true and encrypt_in_transit=true (TLS 1.3).",
    ),
    _p(
        "iso27001-a10-02",
        "TLS Enforcement",
        _F.ISO27001,
        "A.10.1",
        "All external API calls use TLS 1.2 or higher.",
        _S.HIGH,
        """package bernstein.iso27001.a10_02
default allow = false
allow { input.tls_enforced == true }""",
        lambda i: i.tls_enforced,
        "Set tls_enforced=true. Reject plaintext HTTP connections.",
    ),
    _p(
        "iso27001-a12-01",
        "Operational Security Logging",
        _F.ISO27001,
        "A.12.4",
        "Security events are logged and protected against tampering.",
        _S.HIGH,
        """package bernstein.iso27001.a12_01
default allow = false
allow {
    input.audit_logging == true
    input.log_integrity == true
}""",
        lambda i: i.audit_logging and i.log_integrity,
        "Enable audit_logging=true and log_integrity=true.",
    ),
    _p(
        "iso27001-a12-02",
        "Malware and Vulnerability Protection",
        _F.ISO27001,
        "A.12.2",
        "Container images and dependencies are scanned for vulnerabilities.",
        _S.HIGH,
        """package bernstein.iso27001.a12_02
default allow = false
allow { input.vulnerability_scanning == true }""",
        lambda i: i.vulnerability_scanning,
        "Add vulnerability scanning to CI/CD. Enable vulnerability_scanning=true.",
    ),
    _p(
        "iso27001-a12-03",
        "Backup and Recovery",
        _F.ISO27001,
        "A.12.3",
        "Encrypted backups are configured with a tested recovery procedure.",
        _S.HIGH,
        """package bernstein.iso27001.a12_03
default allow = false
allow {
    input.backup_enabled == true
    input.backup_encryption == true
}""",
        lambda i: i.backup_enabled and i.backup_encryption,
        "Enable backup_enabled=true and backup_encryption=true. Test restoration.",
    ),
    _p(
        "iso27001-a14-01",
        "Secure Development",
        _F.ISO27001,
        "A.14.2",
        "SAST and dependency scanning are integrated into the CI/CD pipeline.",
        _S.HIGH,
        """package bernstein.iso27001.a14_01
default allow = false
allow {
    input.sast_in_ci == true
    input.vulnerability_scanning == true
}""",
        lambda i: i.sast_in_ci and i.vulnerability_scanning,
        "Integrate Semgrep/Bandit (sast_in_ci=true) and Trivy (vulnerability_scanning=true).",
    ),
    _p(
        "iso27001-a14-02",
        "Dependency Integrity",
        _F.ISO27001,
        "A.14.2",
        "Dependencies are pinned to exact versions and verified via lock files.",
        _S.MEDIUM,
        """package bernstein.iso27001.a14_02
default allow = false
allow { input.dependency_pinning == true }""",
        lambda i: i.dependency_pinning,
        "Pin all dependencies. Use uv.lock / poetry.lock / package-lock.json.",
    ),
    _p(
        "iso27001-a16-01",
        "Incident Management Process",
        _F.ISO27001,
        "A.16.1",
        "An incident response procedure is documented and practiced.",
        _S.HIGH,
        """package bernstein.iso27001.a16_01
default allow = false
allow { input.incident_response_plan == true }""",
        lambda i: i.incident_response_plan,
        "Document incident response procedure. Set incident_response_plan=true.",
    ),
    _p(
        "iso27001-a17-01",
        "Business Continuity and DR",
        _F.ISO27001,
        "A.17.1",
        "Recovery Time Objective is ≤4 hours with tested DR procedures.",
        _S.MEDIUM,
        """package bernstein.iso27001.a17_01
default allow = false
allow { input.dr_rto_hours <= 4 }""",
        lambda i: i.dr_rto_hours <= 4,
        "Define and test DR runbooks. Set dr_rto_hours=4.",
    ),
]

# ---------------------------------------------------------------------------
# PCI DSS v4.0
# ---------------------------------------------------------------------------

_PCI_DSS_POLICIES: list[CompliancePolicy] = [
    _p(
        "pci-req2-01",
        "Secure System Configuration",
        _F.PCI_DSS,
        "Req 2.2",
        "System components are hardened and unnecessary services are disabled.",
        _S.HIGH,
        """package bernstein.pci_dss.req2_01
default allow = false
allow {
    input.sandbox_enabled == true
    input.least_privilege_caps == true
}""",
        lambda i: i.sandbox_enabled and i.least_privilege_caps,
        "Enable container sandboxing with dropped Linux capabilities.",
    ),
    _p(
        "pci-req3-01",
        "Encryption of Stored Cardholder Data",
        _F.PCI_DSS,
        "Req 3.4",
        "Sensitive data stored at rest is encrypted using strong cryptography.",
        _S.CRITICAL,
        """package bernstein.pci_dss.req3_01
default allow = false
allow { input.encrypt_at_rest == true }""",
        lambda i: i.encrypt_at_rest,
        "Enable encrypt_at_rest=true. Use AES-256-GCM for state files.",
    ),
    _p(
        "pci-req4-01",
        "TLS for Data in Transit",
        _F.PCI_DSS,
        "Req 4.2",
        "Cardholder data is transmitted only over encrypted connections.",
        _S.CRITICAL,
        """package bernstein.pci_dss.req4_01
default allow = false
allow {
    input.tls_enforced == true
    input.encrypt_in_transit == true
}""",
        lambda i: i.tls_enforced and i.encrypt_in_transit,
        "Enforce TLS 1.2+. Set tls_enforced=true and encrypt_in_transit=true.",
    ),
    _p(
        "pci-req6-01",
        "Vulnerability Management",
        _F.PCI_DSS,
        "Req 6.3",
        "Known vulnerabilities are identified and remediated promptly.",
        _S.HIGH,
        """package bernstein.pci_dss.req6_01
default allow = false
allow { input.vulnerability_scanning == true }""",
        lambda i: i.vulnerability_scanning,
        "Run Trivy/Grype in CI. Set vulnerability_scanning=true.",
    ),
    _p(
        "pci-req6-02",
        "SAST in Development Pipeline",
        _F.PCI_DSS,
        "Req 6.3",
        "Static analysis is integrated into the development pipeline.",
        _S.HIGH,
        """package bernstein.pci_dss.req6_02
default allow = false
allow { input.sast_in_ci == true }""",
        lambda i: i.sast_in_ci,
        "Integrate Semgrep or Bandit into CI. Set sast_in_ci=true.",
    ),
    _p(
        "pci-req7-01",
        "Least Privilege Access",
        _F.PCI_DSS,
        "Req 7.1",
        "Access to system components is limited to the minimum required.",
        _S.CRITICAL,
        """package bernstein.pci_dss.req7_01
default allow = false
allow {
    input.rbac_enabled == true
    input.least_privilege_caps == true
}""",
        lambda i: i.rbac_enabled and i.least_privilege_caps,
        "Enable RBAC and drop container capabilities to minimum required.",
    ),
    _p(
        "pci-req8-01",
        "Multi-Factor Authentication",
        _F.PCI_DSS,
        "Req 8.4",
        "MFA is required for all access to the cardholder data environment.",
        _S.CRITICAL,
        """package bernstein.pci_dss.req8_01
default allow = false
allow { input.mfa_enabled == true }""",
        lambda i: i.mfa_enabled,
        "Enforce MFA for all operator authentication. Set mfa_enabled=true.",
    ),
    _p(
        "pci-req8-02",
        "Password Complexity",
        _F.PCI_DSS,
        "Req 8.3",
        "Passwords must be at least 12 characters.",
        _S.HIGH,
        """package bernstein.pci_dss.req8_02
default allow = false
allow { input.password_min_length >= 12 }""",
        lambda i: i.password_min_length >= 12,
        "Set password_min_length=12 or higher.",
    ),
    _p(
        "pci-req10-01",
        "Audit Logging of All Access",
        _F.PCI_DSS,
        "Req 10.2",
        "All access to system components is logged.",
        _S.CRITICAL,
        """package bernstein.pci_dss.req10_01
default allow = false
allow {
    input.audit_logging == true
    input.audit_hmac_chain == true
}""",
        lambda i: i.audit_logging and i.audit_hmac_chain,
        "Enable audit_logging=true and audit_hmac_chain=true.",
    ),
    _p(
        "pci-req10-02",
        "Audit Log Retention (12 months)",
        _F.PCI_DSS,
        "Req 10.5",
        "Audit logs are retained for at least 12 months.",
        _S.HIGH,
        """package bernstein.pci_dss.req10_02
default allow = false
allow { input.audit_retention_days >= 365 }""",
        lambda i: i.audit_retention_days >= 365,
        "Set audit_retention_days=365.",
    ),
    _p(
        "pci-req11-01",
        "Penetration Testing and Scanning",
        _F.PCI_DSS,
        "Req 11.3",
        "Penetration testing and vulnerability scanning are conducted regularly.",
        _S.HIGH,
        """package bernstein.pci_dss.req11_01
default allow = false
allow { input.vulnerability_scanning == true }""",
        lambda i: i.vulnerability_scanning,
        "Run automated vulnerability scanning and schedule periodic penetration tests.",
    ),
    _p(
        "pci-req11-02",
        "WAF Deployment",
        _F.PCI_DSS,
        "Req 6.4",
        "A web application firewall is deployed in front of public-facing systems.",
        _S.HIGH,
        """package bernstein.pci_dss.req11_02
default allow = false
allow { input.waf_enabled == true }""",
        lambda i: i.waf_enabled,
        "Deploy a WAF (e.g. AWS WAF, Cloudflare) in front of the task server API.",
    ),
]

# ---------------------------------------------------------------------------
# NIST SP 800-53 Rev 5
# ---------------------------------------------------------------------------

_NIST_800_53_POLICIES: list[CompliancePolicy] = [
    _p(
        "nist-ac-02",
        "Account Management",
        _F.NIST_800_53,
        "AC-2",
        "User accounts are managed with defined roles and reviewed periodically.",
        _S.HIGH,
        """package bernstein.nist.ac_02
default allow = false
allow {
    input.rbac_enabled == true
    input.access_review_days <= 90
}""",
        lambda i: i.rbac_enabled and i.access_review_days <= 90,
        "Enable RBAC and configure quarterly access reviews.",
    ),
    _p(
        "nist-ac-03",
        "Access Enforcement",
        _F.NIST_800_53,
        "AC-3",
        "Access enforcement mechanisms prevent unauthorised operations.",
        _S.CRITICAL,
        """package bernstein.nist.ac_03
default allow = false
allow { input.rbac_enabled == true }""",
        lambda i: i.rbac_enabled,
        "Enable RBAC. Enforce deny-by-default access policy.",
    ),
    _p(
        "nist-ac-06",
        "Least Privilege",
        _F.NIST_800_53,
        "AC-6",
        "Processes are granted only the minimum privileges required.",
        _S.HIGH,
        """package bernstein.nist.ac_06
default allow = false
allow {
    input.least_privilege_caps == true
    input.sandbox_enabled == true
}""",
        lambda i: i.least_privilege_caps and i.sandbox_enabled,
        "Drop container capabilities. Enable seccomp filtering.",
    ),
    _p(
        "nist-ac-17",
        "Remote Access Security",
        _F.NIST_800_53,
        "AC-17",
        "Remote access requires MFA and encrypted sessions.",
        _S.HIGH,
        """package bernstein.nist.ac_17
default allow = false
allow {
    input.mfa_enabled == true
    input.tls_enforced == true
}""",
        lambda i: i.mfa_enabled and i.tls_enforced,
        "Require MFA for remote access. Enforce TLS on all connections.",
    ),
    _p(
        "nist-au-02",
        "Audit Event Selection",
        _F.NIST_800_53,
        "AU-2",
        "The organisation defines and logs auditable events.",
        _S.HIGH,
        """package bernstein.nist.au_02
default allow = false
allow { input.audit_logging == true }""",
        lambda i: i.audit_logging,
        "Enable audit_logging=true and define which events to capture.",
    ),
    _p(
        "nist-au-03",
        "Audit Record Content",
        _F.NIST_800_53,
        "AU-3",
        "Audit records contain sufficient information to reconstruct events.",
        _S.HIGH,
        """package bernstein.nist.au_03
default allow = false
allow {
    input.audit_logging == true
    input.audit_hmac_chain == true
}""",
        lambda i: i.audit_logging and i.audit_hmac_chain,
        "Enable HMAC-chained audit logging for tamper-evident records.",
    ),
    _p(
        "nist-au-09",
        "Audit Record Protection",
        _F.NIST_800_53,
        "AU-9",
        "Audit records are protected from unauthorised modification.",
        _S.HIGH,
        """package bernstein.nist.au_09
default allow = false
allow { input.log_integrity == true }""",
        lambda i: i.log_integrity,
        "Enable log_integrity=true with signed WAL.",
    ),
    _p(
        "nist-au-11",
        "Audit Record Retention",
        _F.NIST_800_53,
        "AU-11",
        "Audit logs are retained for a minimum of 3 years for federal contexts.",
        _S.MEDIUM,
        """package bernstein.nist.au_11
default allow = false
allow { input.audit_retention_days >= 365 }""",
        lambda i: i.audit_retention_days >= 365,
        "Retain audit logs for at least 365 days (extend to 1095 for federal).",
    ),
    _p(
        "nist-cm-02",
        "Baseline Configuration",
        _F.NIST_800_53,
        "CM-2",
        "A baseline configuration is established and maintained.",
        _S.MEDIUM,
        """package bernstein.nist.cm_02
default allow = false
allow { input.dependency_pinning == true }""",
        lambda i: i.dependency_pinning,
        "Pin all dependencies to exact versions. Use reproducible builds.",
    ),
    _p(
        "nist-cm-07",
        "Least Functionality",
        _F.NIST_800_53,
        "CM-7",
        "Systems are configured to provide only essential capabilities.",
        _S.HIGH,
        """package bernstein.nist.cm_07
default allow = false
allow {
    input.sandbox_enabled == true
    input.seccomp_enabled == true
    input.network_isolation == true
}""",
        lambda i: i.sandbox_enabled and i.seccomp_enabled and i.network_isolation,
        "Enable container sandbox, seccomp filtering, and network isolation.",
    ),
    _p(
        "nist-ia-02",
        "User Identification and Authentication",
        _F.NIST_800_53,
        "IA-2",
        "Users and agents are uniquely identified and authenticated.",
        _S.CRITICAL,
        """package bernstein.nist.ia_02
default allow = false
allow {
    input.mfa_enabled == true
    input.rbac_enabled == true
}""",
        lambda i: i.mfa_enabled and i.rbac_enabled,
        "Enforce MFA and RBAC for all identities.",
    ),
    _p(
        "nist-ia-05",
        "Authenticator Management",
        _F.NIST_800_53,
        "IA-5",
        "Credentials are rotated, of sufficient complexity, and protected.",
        _S.HIGH,
        """package bernstein.nist.ia_05
default allow = false
allow {
    input.secrets_rotation_days <= 90
    input.password_min_length >= 12
}""",
        lambda i: i.secrets_rotation_days <= 90 and i.password_min_length >= 12,
        "Rotate secrets ≤90 days. Enforce minimum credential length of 12.",
    ),
    _p(
        "nist-ra-05",
        "Vulnerability Monitoring",
        _F.NIST_800_53,
        "RA-5",
        "Systems are scanned for vulnerabilities on a regular basis.",
        _S.HIGH,
        """package bernstein.nist.ra_05
default allow = false
allow { input.vulnerability_scanning == true }""",
        lambda i: i.vulnerability_scanning,
        "Integrate automated vulnerability scanning into CI/CD.",
    ),
    _p(
        "nist-sc-08",
        "Transmission Confidentiality and Integrity",
        _F.NIST_800_53,
        "SC-8",
        "Data transmitted over networks is protected against disclosure and modification.",
        _S.HIGH,
        """package bernstein.nist.sc_08
default allow = false
allow {
    input.tls_enforced == true
    input.encrypt_in_transit == true
}""",
        lambda i: i.tls_enforced and i.encrypt_in_transit,
        "Enforce TLS 1.3. Set tls_enforced=true and encrypt_in_transit=true.",
    ),
    _p(
        "nist-sc-28",
        "Protection of Information at Rest",
        _F.NIST_800_53,
        "SC-28",
        "Information stored at rest is protected using encryption.",
        _S.HIGH,
        """package bernstein.nist.sc_28
default allow = false
allow { input.encrypt_at_rest == true }""",
        lambda i: i.encrypt_at_rest,
        "Enable encrypt_at_rest=true. Use AES-256-GCM.",
    ),
    _p(
        "nist-si-02",
        "Flaw Remediation",
        _F.NIST_800_53,
        "SI-2",
        "Security flaws are identified and corrected in a timely manner.",
        _S.HIGH,
        """package bernstein.nist.si_02
default allow = false
allow {
    input.vulnerability_scanning == true
    input.sast_in_ci == true
}""",
        lambda i: i.vulnerability_scanning and i.sast_in_ci,
        "Run automated SAST and vulnerability scanning. Remediate within SLA.",
    ),
    _p(
        "nist-si-03",
        "Malicious Code Protection",
        _F.NIST_800_53,
        "SI-3",
        "Mechanisms protect against malicious code at appropriate locations.",
        _S.HIGH,
        """package bernstein.nist.si_03
default allow = false
allow {
    input.sandbox_enabled == true
    input.seccomp_enabled == true
}""",
        lambda i: i.sandbox_enabled and i.seccomp_enabled,
        "Container sandboxing and seccomp filtering limit malicious code execution.",
    ),
    _p(
        "nist-si-10",
        "Information Input Validation",
        _F.NIST_800_53,
        "SI-10",
        "The system validates information inputs to prevent injection attacks.",
        _S.HIGH,
        """package bernstein.nist.si_10
default allow = false
allow {
    input.sandbox_enabled == true
    input.waf_enabled == true
}""",
        lambda i: i.sandbox_enabled and i.waf_enabled,
        "Deploy WAF and validate all agent task inputs before execution.",
    ),
]

# ---------------------------------------------------------------------------
# Master policy registry
# ---------------------------------------------------------------------------

ALL_POLICIES: list[CompliancePolicy] = (
    _SOC2_POLICIES + _ISO27001_POLICIES + _PCI_DSS_POLICIES + _NIST_800_53_POLICIES
)

_BY_FRAMEWORK: dict[ComplianceFramework, list[CompliancePolicy]] = {
    ComplianceFramework.SOC2: _SOC2_POLICIES,
    ComplianceFramework.ISO27001: _ISO27001_POLICIES,
    ComplianceFramework.PCI_DSS: _PCI_DSS_POLICIES,
    ComplianceFramework.NIST_800_53: _NIST_800_53_POLICIES,
}

_BY_ID: dict[str, CompliancePolicy] = {p.policy_id: p for p in ALL_POLICIES}


# ---------------------------------------------------------------------------
# Evaluation helpers
# ---------------------------------------------------------------------------


def evaluate_policy(policy: CompliancePolicy, inp: PolicyInput) -> PolicyResult:
    """Evaluate a single policy against the provided runtime snapshot.

    Args:
        policy: The policy to evaluate.
        inp: Runtime configuration snapshot.

    Returns:
        :class:`PolicyResult` with the outcome and any finding text.
    """
    try:
        passed = bool(policy.check(inp))
    except Exception:
        logger.exception("Policy check raised unexpectedly: %s", policy.policy_id)
        passed = False

    finding = "" if passed else f"Policy {policy.policy_id} ({policy.control_id}) FAILED: {policy.description}"
    return PolicyResult(
        policy_id=policy.policy_id,
        name=policy.name,
        framework=policy.framework,
        control_id=policy.control_id,
        severity=policy.severity,
        passed=passed,
        finding=finding,
        remediation=policy.remediation if not passed else "",
    )


def evaluate_framework(
    framework: ComplianceFramework,
    inp: PolicyInput,
) -> list[PolicyResult]:
    """Evaluate all policies for a given compliance framework.

    Args:
        framework: The compliance framework to evaluate.
        inp: Runtime configuration snapshot.

    Returns:
        List of :class:`PolicyResult` for every policy in the framework.
    """
    policies = _BY_FRAMEWORK.get(framework, [])
    return [evaluate_policy(p, inp) for p in policies]


def evaluate_all(inp: PolicyInput) -> list[PolicyResult]:
    """Evaluate every policy in the library against the runtime snapshot.

    Args:
        inp: Runtime configuration snapshot.

    Returns:
        List of all :class:`PolicyResult` objects.
    """
    return [evaluate_policy(p, inp) for p in ALL_POLICIES]


# ---------------------------------------------------------------------------
# Policy library manager
# ---------------------------------------------------------------------------


class CompliancePolicyLibrary:
    """Manages compliance policy evaluation and persistence.

    Policies for enabled frameworks are stored under ``<config_dir>/enabled/``
    as plain YAML marker files so the orchestrator can load them on startup.

    Usage::

        lib = CompliancePolicyLibrary()
        lib.enable(ComplianceFramework.SOC2, config_dir=Path(".sdd/compliance"))
        results = lib.evaluate(inp)
    """

    def __init__(self) -> None:
        self._enabled: set[ComplianceFramework] = set()

    def enable(
        self,
        framework: ComplianceFramework,
        config_dir: Path | None = None,
    ) -> None:
        """Activate a compliance framework.

        Writes a marker file to ``<config_dir>/enabled/<framework>.yaml`` so
        the setting persists across restarts.

        Args:
            framework: The framework to enable.
            config_dir: Directory to persist the marker file.  If ``None``, the
                framework is enabled in memory only.
        """
        self._enabled.add(framework)
        if config_dir is not None:
            enabled_dir = Path(config_dir) / "enabled"
            enabled_dir.mkdir(parents=True, exist_ok=True)
            marker = enabled_dir / f"{framework.value}.yaml"
            policy_ids = [p.policy_id for p in _BY_FRAMEWORK[framework]]
            marker.write_text(
                f"# Bernstein compliance-as-code — {framework.value}\n"
                f"framework: {framework.value}\n"
                f"enabled: true\n"
                f"policy_count: {len(policy_ids)}\n"
                f"policies:\n" + "".join(f"  - {pid}\n" for pid in policy_ids),
                encoding="utf-8",
            )
            logger.info("Enabled compliance framework %s (%d policies)", framework.value, len(policy_ids))

    def disable(
        self,
        framework: ComplianceFramework,
        config_dir: Path | None = None,
    ) -> None:
        """Deactivate a compliance framework.

        Args:
            framework: The framework to disable.
            config_dir: If provided, removes the marker file.
        """
        self._enabled.discard(framework)
        if config_dir is not None:
            marker = Path(config_dir) / "enabled" / f"{framework.value}.yaml"
            if marker.exists():
                marker.unlink()
            logger.info("Disabled compliance framework %s", framework.value)

    def load_enabled(self, config_dir: Path) -> None:
        """Load enabled frameworks from marker files in ``config_dir/enabled/``.

        Args:
            config_dir: Directory containing ``enabled/<framework>.yaml`` files.
        """
        enabled_dir = Path(config_dir) / "enabled"
        if not enabled_dir.exists():
            return
        for marker in enabled_dir.glob("*.yaml"):
            name = marker.stem
            try:
                fw = ComplianceFramework(name)
                self._enabled.add(fw)
                logger.debug("Loaded enabled framework: %s", name)
            except ValueError:
                logger.warning("Unknown compliance framework marker: %s", name)

    def evaluate(self, inp: PolicyInput) -> list[PolicyResult]:
        """Evaluate all enabled frameworks against the runtime snapshot.

        Args:
            inp: Runtime configuration snapshot.

        Returns:
            Combined list of :class:`PolicyResult` for all enabled frameworks.
        """
        results: list[PolicyResult] = []
        for fw in self._enabled:
            results.extend(evaluate_framework(fw, inp))
        return results

    @property
    def enabled_frameworks(self) -> list[ComplianceFramework]:
        """Return the list of currently enabled frameworks."""
        return list(self._enabled)

    def policy_count(self, framework: ComplianceFramework | None = None) -> int:
        """Return the number of policies available.

        Args:
            framework: If provided, count only policies for this framework.

        Returns:
            Integer count of available policies.
        """
        if framework is not None:
            return len(_BY_FRAMEWORK.get(framework, []))
        return len(ALL_POLICIES)

    def get_policy(self, policy_id: str) -> CompliancePolicy | None:
        """Look up a policy by its ID.

        Args:
            policy_id: Policy identifier string.

        Returns:
            :class:`CompliancePolicy` if found, else ``None``.
        """
        return _BY_ID.get(policy_id)

    def export_rego(
        self,
        framework: ComplianceFramework,
        dest_dir: Path,
    ) -> list[Path]:
        """Export Rego rule files for the given framework.

        Each policy's ``rego_rule`` is written to a separate ``.rego`` file so
        the rules can be loaded into an OPA server for live evaluation.

        Args:
            framework: Framework whose Rego rules to export.
            dest_dir: Target directory for the ``.rego`` files.

        Returns:
            List of paths to the written files.
        """
        policies = _BY_FRAMEWORK.get(framework, [])
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for policy in policies:
            out = dest_dir / f"{policy.policy_id}.rego"
            out.write_text(policy.rego_rule, encoding="utf-8")
            paths.append(out)
        logger.info("Exported %d Rego files to %s", len(paths), dest_dir)
        return paths
