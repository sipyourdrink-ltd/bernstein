"""Compliance-as-code policy library with pre-built rules.

Provides a library of pure-Python compliance checks that inspect the
filesystem state of a Bernstein project (``project_root``).  Each check
verifies a concrete control requirement — audit logging directories exist,
auth is configured, encryption settings are present, etc.

Six compliance frameworks are supported:

- **SOC2** — Trust Service Criteria (CC-series controls)
- **ISO27001** — Annex A information security controls
- **PCI_DSS** — Payment Card Industry Data Security Standard
- **NIST_800_53** — NIST Special Publication 800-53 security controls
- **HIPAA** — Health Insurance Portability and Accountability Act
- **GDPR** — General Data Protection Regulation

Usage::

    from bernstein.core.security.compliance_library import (
        ComplianceFramework,
        get_framework_rules,
        run_compliance_check,
        render_compliance_report,
    )

    report = run_compliance_check(ComplianceFramework.SOC2, Path("."))
    print(render_compliance_report(report))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

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
    HIPAA = "hipaa"
    GDPR = "gdpr"


class Severity(StrEnum):
    """Rule severity levels."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyRule:
    """A single compliance policy rule.

    Attributes:
        rule_id: Unique identifier (e.g. ``soc2-audit-01``).
        framework: Compliance framework the rule belongs to.
        title: Short human-readable title.
        description: What this rule checks and why it matters.
        check_function_name: Name of the Python check function.
        severity: Impact level when the rule fails.
    """

    rule_id: str
    framework: ComplianceFramework
    title: str
    description: str
    check_function_name: str
    severity: Severity


@dataclass(frozen=True)
class PolicyResult:
    """Result of evaluating a single :class:`PolicyRule`.

    Attributes:
        rule: The evaluated rule.
        passed: Whether the check passed.
        evidence: Description of what was found (or not found).
        remediation: Suggested corrective action when the check fails.
    """

    rule: PolicyRule
    passed: bool
    evidence: str
    remediation: str


@dataclass(frozen=True)
class ComplianceReport:
    """Aggregated result of running all rules for a framework.

    Attributes:
        framework: The framework that was evaluated.
        rules_checked: Total number of rules evaluated.
        rules_passed: Number of rules that passed.
        rules_failed: Number of rules that failed.
        results: Individual results for each rule.
        score: Compliance score from 0.0 (all failed) to 1.0 (all passed).
    """

    framework: ComplianceFramework
    rules_checked: int
    rules_passed: int
    rules_failed: int
    results: tuple[PolicyResult, ...]
    score: float


# ---------------------------------------------------------------------------
# Check functions — pure Python, filesystem-based
# ---------------------------------------------------------------------------


def _load_yaml_config(project_root: Path) -> dict[str, Any]:
    """Load the bernstein.yaml config, returning empty dict on failure."""
    empty: dict[str, Any] = {}
    for name in ("bernstein.yaml", "bernstein.yml"):
        config_path = project_root / name
        if config_path.is_file():
            try:
                import yaml

                data: object = yaml.safe_load(config_path.read_text())
                if isinstance(data, dict):
                    return cast("dict[str, Any]", data)
            except Exception:
                logger.debug("Failed to parse %s", config_path)
    return empty


def _load_sdd_config(project_root: Path) -> dict[str, Any]:
    """Load .sdd/config.yaml, returning empty dict on failure."""
    empty: dict[str, Any] = {}
    config_path = project_root / ".sdd" / "config.yaml"
    if config_path.is_file():
        try:
            import yaml

            data: object = yaml.safe_load(config_path.read_text())
            if isinstance(data, dict):
                return cast("dict[str, Any]", data)
        except Exception:
            pass
    return empty


def check_audit_logging_enabled(project_root: Path) -> PolicyResult:
    """Verify that .sdd/audit/ directory exists for audit log storage."""
    audit_dir = project_root / ".sdd" / "audit"
    passed = audit_dir.is_dir()
    evidence = (
        f"Audit directory exists at {audit_dir}"
        if passed
        else f"Audit directory not found at {audit_dir}"
    )
    remediation = "" if passed else "Create .sdd/audit/ directory and enable audit logging in config."
    return PolicyResult(
        rule=_RULE_PLACEHOLDER,
        passed=passed,
        evidence=evidence,
        remediation=remediation,
    )


def check_auth_configured(project_root: Path) -> PolicyResult:
    """Verify that an auth section exists in configuration."""
    config = _load_yaml_config(project_root)
    sdd_config = _load_sdd_config(project_root)
    has_auth = "auth" in config or "auth" in sdd_config
    evidence = "Auth section found in config" if has_auth else "No auth section in bernstein.yaml or .sdd/config.yaml"
    remediation = "" if has_auth else "Add an 'auth' section to bernstein.yaml with authentication settings."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_auth, evidence=evidence, remediation=remediation)


def check_encryption_at_rest(project_root: Path) -> PolicyResult:
    """Verify that state_encryption settings are configured."""
    config = _load_yaml_config(project_root)
    sdd_config = _load_sdd_config(project_root)
    has_encryption = (
        "state_encryption" in config
        or "state_encryption" in sdd_config
        or config.get("compliance", {}).get("encrypt_state_at_rest", False)
    )
    evidence = (
        "State encryption settings found"
        if has_encryption
        else "No state_encryption or encrypt_state_at_rest setting found"
    )
    remediation = (
        ""
        if has_encryption
        else "Add 'state_encryption' config or set compliance.encrypt_state_at_rest: true."
    )
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_encryption, evidence=evidence, remediation=remediation)


def check_access_controls(project_root: Path) -> PolicyResult:
    """Verify that RBAC is configured."""
    config = _load_yaml_config(project_root)
    sdd_config = _load_sdd_config(project_root)
    has_rbac = (
        "rbac" in config
        or "rbac" in sdd_config
        or "roles" in config
        or config.get("security", {}).get("rbac_enabled", False)
    )
    evidence = "RBAC / role configuration found" if has_rbac else "No RBAC or roles configuration found"
    remediation = "" if has_rbac else "Add 'rbac' section to config with role definitions and permissions."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_rbac, evidence=evidence, remediation=remediation)


def check_data_retention(project_root: Path) -> PolicyResult:
    """Verify that a data retention policy exists."""
    config = _load_yaml_config(project_root)
    sdd_config = _load_sdd_config(project_root)
    has_retention = (
        "retention" in config
        or "data_retention" in config
        or "retention" in sdd_config
        or "data_retention" in sdd_config
    )
    evidence = (
        "Data retention policy found in config"
        if has_retention
        else "No retention or data_retention setting found"
    )
    remediation = "" if has_retention else "Add a 'data_retention' section specifying log and data retention periods."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_retention, evidence=evidence, remediation=remediation)


def check_backup_configured(project_root: Path) -> PolicyResult:
    """Verify that backup configuration exists."""
    config = _load_yaml_config(project_root)
    sdd_config = _load_sdd_config(project_root)
    has_backup = "backup" in config or "backup" in sdd_config or "backups" in config
    evidence = "Backup configuration found" if has_backup else "No backup configuration found"
    remediation = "" if has_backup else "Add a 'backup' section with schedule, destination, and encryption settings."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_backup, evidence=evidence, remediation=remediation)


def check_tls_enforced(project_root: Path) -> PolicyResult:
    """Verify that TLS enforcement is configured."""
    config = _load_yaml_config(project_root)
    sdd_config = _load_sdd_config(project_root)
    has_tls = (
        config.get("security", {}).get("tls_enforced", False)
        or config.get("tls", {}).get("enabled", False)
        or sdd_config.get("tls", {}).get("enabled", False)
    )
    evidence = "TLS enforcement configured" if has_tls else "No TLS enforcement setting found"
    remediation = "" if has_tls else "Set security.tls_enforced: true or configure TLS certificates."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_tls, evidence=evidence, remediation=remediation)


def check_incident_response_plan(project_root: Path) -> PolicyResult:
    """Verify that an incident response plan document exists."""
    candidates = [
        project_root / "docs" / "incident-response.md",
        project_root / "docs" / "incident_response.md",
        project_root / "docs" / "INCIDENT_RESPONSE.md",
        project_root / ".sdd" / "incident-response.yaml",
        project_root / "INCIDENT_RESPONSE.md",
    ]
    found = any(p.is_file() for p in candidates)
    evidence = "Incident response plan document found" if found else "No incident response plan document found"
    remediation = (
        "" if found else "Create docs/incident-response.md with response procedures, contacts, and escalation paths."
    )
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=found, evidence=evidence, remediation=remediation)


def check_secrets_management(project_root: Path) -> PolicyResult:
    """Verify that secrets management settings exist (vault, rotation, etc.)."""
    config = _load_yaml_config(project_root)
    sdd_config = _load_sdd_config(project_root)
    has_secrets = (
        "secrets" in config
        or "vault" in config
        or "secrets" in sdd_config
        or "key_rotation" in config
        or config.get("security", {}).get("secrets_rotation_days", 0) > 0
    )
    evidence = "Secrets management configuration found" if has_secrets else "No secrets management configuration found"
    remediation = "" if has_secrets else "Configure secrets management with rotation policies and a vault backend."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_secrets, evidence=evidence, remediation=remediation)


def check_vulnerability_scanning(project_root: Path) -> PolicyResult:
    """Verify that vulnerability scanning is configured."""
    config = _load_yaml_config(project_root)
    sdd_config = _load_sdd_config(project_root)
    has_scanning = (
        config.get("security", {}).get("vulnerability_scanning", False)
        or "vulnerability_scanning" in sdd_config
        or config.get("quality_gates", {}).get("security_scan", False)
    )
    evidence = (
        "Vulnerability scanning configured" if has_scanning else "No vulnerability scanning configuration found"
    )
    remediation = "" if has_scanning else "Enable vulnerability scanning in security settings or quality gates."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_scanning, evidence=evidence, remediation=remediation)


def check_change_management(project_root: Path) -> PolicyResult:
    """Verify that change approval gates are configured."""
    config = _load_yaml_config(project_root)
    qg = config.get("quality_gates", {})
    has_gates = qg.get("enabled", False) or "approval" in config or "change_management" in config
    evidence = "Change management / quality gates found" if has_gates else "No change management controls found"
    remediation = "" if has_gates else "Enable quality_gates with lint, type_check, and approval requirements."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_gates, evidence=evidence, remediation=remediation)


def check_network_isolation(project_root: Path) -> PolicyResult:
    """Verify that network isolation settings exist for agent sandboxes."""
    config = _load_yaml_config(project_root)
    sdd_config = _load_sdd_config(project_root)
    has_isolation = (
        config.get("security", {}).get("network_isolation", False)
        or config.get("sandbox", {}).get("network_mode", "") == "none"
        or "network_isolation" in sdd_config
    )
    evidence = "Network isolation configured" if has_isolation else "No network isolation settings found"
    remediation = "" if has_isolation else "Configure sandbox.network_mode: none or security.network_isolation: true."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_isolation, evidence=evidence, remediation=remediation)


def check_logging_integrity(project_root: Path) -> PolicyResult:
    """Verify that log integrity mechanisms are configured (HMAC chain, etc.)."""
    config = _load_yaml_config(project_root)
    sdd_config = _load_sdd_config(project_root)
    has_integrity = (
        config.get("compliance", {}).get("audit_hmac_chain", False)
        or config.get("security", {}).get("log_integrity", False)
        or sdd_config.get("audit_hmac_chain", False)
    )
    evidence = "Log integrity mechanism configured" if has_integrity else "No log integrity (HMAC chain) configured"
    remediation = "" if has_integrity else "Enable compliance.audit_hmac_chain: true for tamper-evident logging."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_integrity, evidence=evidence, remediation=remediation)


def check_session_management(project_root: Path) -> PolicyResult:
    """Verify that session timeout / token expiry is configured."""
    config = _load_yaml_config(project_root)
    security = config.get("security", {})
    has_session = (
        "session_timeout_minutes" in security
        or "agent_token_expiry_hours" in security
        or "session" in config
    )
    evidence = "Session management settings found" if has_session else "No session timeout or token expiry configured"
    remediation = "" if has_session else "Set security.session_timeout_minutes and security.agent_token_expiry_hours."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_session, evidence=evidence, remediation=remediation)


def check_password_policy(project_root: Path) -> PolicyResult:
    """Verify that a minimum password length policy is configured."""
    config = _load_yaml_config(project_root)
    security = config.get("security", {})
    min_len = security.get("password_min_length", 0)
    passed = min_len >= 12  # NIST 800-63B recommendation
    evidence = f"Password min length: {min_len}" if min_len > 0 else "No password policy configured"
    remediation = "" if passed else "Set security.password_min_length to at least 12 characters (NIST 800-63B)."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=passed, evidence=evidence, remediation=remediation)


def check_mfa_enabled(project_root: Path) -> PolicyResult:
    """Verify that multi-factor authentication is enabled."""
    config = _load_yaml_config(project_root)
    sdd_config = _load_sdd_config(project_root)
    has_mfa = config.get("security", {}).get("mfa_enabled", False) or sdd_config.get("mfa_enabled", False)
    evidence = "MFA enabled" if has_mfa else "MFA not enabled"
    remediation = "" if has_mfa else "Set security.mfa_enabled: true to require multi-factor authentication."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_mfa, evidence=evidence, remediation=remediation)


def check_sdd_state_directory(project_root: Path) -> PolicyResult:
    """Verify that the .sdd/ state directory structure exists."""
    sdd = project_root / ".sdd"
    passed = sdd.is_dir()
    evidence = f".sdd/ state directory {'exists' if passed else 'not found'} at {sdd}"
    remediation = "" if passed else "Initialize the project with 'bernstein init' to create the .sdd/ state directory."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=passed, evidence=evidence, remediation=remediation)


def check_rate_limiting(project_root: Path) -> PolicyResult:
    """Verify that API rate limiting is configured."""
    config = _load_yaml_config(project_root)
    sdd_config = _load_sdd_config(project_root)
    has_rate_limit = (
        config.get("security", {}).get("rate_limiting_enabled", False)
        or "rate_limit" in config
        or "rate_limiting" in sdd_config
    )
    evidence = "Rate limiting configured" if has_rate_limit else "No rate limiting configuration found"
    remediation = "" if has_rate_limit else "Enable security.rate_limiting_enabled: true to protect API endpoints."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_rate_limit, evidence=evidence, remediation=remediation)


def check_dependency_pinning(project_root: Path) -> PolicyResult:
    """Verify that dependencies are pinned (lock file exists)."""
    candidates = [
        project_root / "uv.lock",
        project_root / "poetry.lock",
        project_root / "Pipfile.lock",
        project_root / "requirements.txt",
        project_root / "package-lock.json",
    ]
    found = any(p.is_file() for p in candidates)
    evidence = "Dependency lock file found" if found else "No dependency lock file found"
    remediation = "" if found else "Create a lock file (uv.lock, poetry.lock, etc.) to pin dependency versions."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=found, evidence=evidence, remediation=remediation)


def check_privacy_policy(project_root: Path) -> PolicyResult:
    """Verify that a privacy policy or data processing document exists."""
    candidates = [
        project_root / "docs" / "privacy-policy.md",
        project_root / "docs" / "PRIVACY.md",
        project_root / "PRIVACY.md",
        project_root / "docs" / "data-processing.md",
        project_root / ".sdd" / "privacy.yaml",
    ]
    found = any(p.is_file() for p in candidates)
    evidence = "Privacy policy document found" if found else "No privacy policy document found"
    remediation = "" if found else "Create docs/privacy-policy.md documenting data processing and retention practices."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=found, evidence=evidence, remediation=remediation)


def check_data_classification(project_root: Path) -> PolicyResult:
    """Verify that data classification settings exist."""
    config = _load_yaml_config(project_root)
    sdd_config = _load_sdd_config(project_root)
    has_classification = (
        "data_classification" in config
        or "data_classification" in sdd_config
        or config.get("security", {}).get("data_classification", False)
    )
    evidence = (
        "Data classification configured"
        if has_classification
        else "No data classification configuration found"
    )
    remediation = (
        ""
        if has_classification
        else "Add 'data_classification' settings to label and classify sensitive data."
    )
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_classification, evidence=evidence, remediation=remediation)


def check_phi_detection(project_root: Path) -> PolicyResult:
    """Verify that PHI/PII detection is enabled (HIPAA requirement)."""
    config = _load_yaml_config(project_root)
    sdd_config = _load_sdd_config(project_root)
    has_phi = (
        config.get("compliance", {}).get("phi_detection", False)
        or config.get("hipaa_mode", False)
        or sdd_config.get("phi_detection", False)
    )
    evidence = "PHI/PII detection enabled" if has_phi else "PHI/PII detection not enabled"
    remediation = "" if has_phi else "Enable compliance.phi_detection: true for HIPAA-required PHI scanning."
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_phi, evidence=evidence, remediation=remediation)


def check_consent_management(project_root: Path) -> PolicyResult:
    """Verify that consent management or data subject rights processes exist."""
    config = _load_yaml_config(project_root)
    sdd_config = _load_sdd_config(project_root)
    candidates = [
        project_root / "docs" / "consent-management.md",
        project_root / "docs" / "data-subject-rights.md",
        project_root / ".sdd" / "consent.yaml",
    ]
    has_consent = (
        any(p.is_file() for p in candidates)
        or "consent" in config
        or "consent" in sdd_config
        or "data_subject_rights" in config
    )
    evidence = "Consent / data subject rights management found" if has_consent else "No consent management found"
    remediation = (
        ""
        if has_consent
        else "Document consent management and data subject rights procedures for GDPR compliance."
    )
    return PolicyResult(rule=_RULE_PLACEHOLDER, passed=has_consent, evidence=evidence, remediation=remediation)


# Sentinel rule used during individual check function calls before rule binding.
_RULE_PLACEHOLDER = PolicyRule(
    rule_id="placeholder",
    framework=ComplianceFramework.SOC2,
    title="placeholder",
    description="placeholder",
    check_function_name="placeholder",
    severity=Severity.LOW,
)

# ---------------------------------------------------------------------------
# Check function registry
# ---------------------------------------------------------------------------

_CHECK_FUNCTIONS: dict[str, Any] = {
    "check_audit_logging_enabled": check_audit_logging_enabled,
    "check_auth_configured": check_auth_configured,
    "check_encryption_at_rest": check_encryption_at_rest,
    "check_access_controls": check_access_controls,
    "check_data_retention": check_data_retention,
    "check_backup_configured": check_backup_configured,
    "check_tls_enforced": check_tls_enforced,
    "check_incident_response_plan": check_incident_response_plan,
    "check_secrets_management": check_secrets_management,
    "check_vulnerability_scanning": check_vulnerability_scanning,
    "check_change_management": check_change_management,
    "check_network_isolation": check_network_isolation,
    "check_logging_integrity": check_logging_integrity,
    "check_session_management": check_session_management,
    "check_password_policy": check_password_policy,
    "check_mfa_enabled": check_mfa_enabled,
    "check_sdd_state_directory": check_sdd_state_directory,
    "check_rate_limiting": check_rate_limiting,
    "check_dependency_pinning": check_dependency_pinning,
    "check_privacy_policy": check_privacy_policy,
    "check_data_classification": check_data_classification,
    "check_phi_detection": check_phi_detection,
    "check_consent_management": check_consent_management,
}


# ---------------------------------------------------------------------------
# Rule definitions per framework
# ---------------------------------------------------------------------------

_ALL_RULES: list[PolicyRule] = [
    # --- SOC2 ---
    PolicyRule(
        rule_id="soc2-audit-01",
        framework=ComplianceFramework.SOC2,
        title="Audit Logging Enabled",
        description="CC7.2: Verify that audit logging directory exists for event capture.",
        check_function_name="check_audit_logging_enabled",
        severity=Severity.CRITICAL,
    ),
    PolicyRule(
        rule_id="soc2-access-01",
        framework=ComplianceFramework.SOC2,
        title="Authentication Configured",
        description="CC6.1: Verify that authentication is configured for access control.",
        check_function_name="check_auth_configured",
        severity=Severity.CRITICAL,
    ),
    PolicyRule(
        rule_id="soc2-access-02",
        framework=ComplianceFramework.SOC2,
        title="RBAC Access Controls",
        description="CC6.3: Verify that role-based access controls are configured.",
        check_function_name="check_access_controls",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="soc2-change-01",
        framework=ComplianceFramework.SOC2,
        title="Change Management Controls",
        description="CC8.1: Verify that change approval gates are configured.",
        check_function_name="check_change_management",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="soc2-avail-01",
        framework=ComplianceFramework.SOC2,
        title="Backup Configuration",
        description="A1.2: Verify that backup procedures are configured.",
        check_function_name="check_backup_configured",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="soc2-monitor-01",
        framework=ComplianceFramework.SOC2,
        title="Vulnerability Scanning",
        description="CC7.1: Verify that vulnerability scanning is enabled.",
        check_function_name="check_vulnerability_scanning",
        severity=Severity.MEDIUM,
    ),
    PolicyRule(
        rule_id="soc2-incident-01",
        framework=ComplianceFramework.SOC2,
        title="Incident Response Plan",
        description="CC7.3: Verify that an incident response plan exists.",
        check_function_name="check_incident_response_plan",
        severity=Severity.HIGH,
    ),
    # --- ISO 27001 ---
    PolicyRule(
        rule_id="iso27001-crypto-01",
        framework=ComplianceFramework.ISO27001,
        title="Encryption at Rest",
        description="A.10.1.1: Verify that state data is encrypted at rest.",
        check_function_name="check_encryption_at_rest",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="iso27001-crypto-02",
        framework=ComplianceFramework.ISO27001,
        title="TLS Enforcement",
        description="A.10.1.1: Verify that TLS is enforced for data in transit.",
        check_function_name="check_tls_enforced",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="iso27001-access-01",
        framework=ComplianceFramework.ISO27001,
        title="Access Control Policy",
        description="A.9.1.1: Verify that access control policies are defined.",
        check_function_name="check_access_controls",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="iso27001-access-02",
        framework=ComplianceFramework.ISO27001,
        title="Authentication Configuration",
        description="A.9.2.1: Verify that user authentication is configured.",
        check_function_name="check_auth_configured",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="iso27001-ops-01",
        framework=ComplianceFramework.ISO27001,
        title="Audit Logging",
        description="A.12.4.1: Verify that event logging is enabled.",
        check_function_name="check_audit_logging_enabled",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="iso27001-ops-02",
        framework=ComplianceFramework.ISO27001,
        title="Log Integrity",
        description="A.12.4.2: Verify that log integrity mechanisms are in place.",
        check_function_name="check_logging_integrity",
        severity=Severity.MEDIUM,
    ),
    PolicyRule(
        rule_id="iso27001-ops-03",
        framework=ComplianceFramework.ISO27001,
        title="Vulnerability Management",
        description="A.12.6.1: Verify that vulnerability scanning is configured.",
        check_function_name="check_vulnerability_scanning",
        severity=Severity.MEDIUM,
    ),
    PolicyRule(
        rule_id="iso27001-supplier-01",
        framework=ComplianceFramework.ISO27001,
        title="Dependency Pinning",
        description="A.15.1.1: Verify that dependencies are pinned to known versions.",
        check_function_name="check_dependency_pinning",
        severity=Severity.MEDIUM,
    ),
    PolicyRule(
        rule_id="iso27001-classify-01",
        framework=ComplianceFramework.ISO27001,
        title="Data Classification",
        description="A.8.2.1: Verify that data classification scheme is defined.",
        check_function_name="check_data_classification",
        severity=Severity.MEDIUM,
    ),
    # --- PCI DSS ---
    PolicyRule(
        rule_id="pci-network-01",
        framework=ComplianceFramework.PCI_DSS,
        title="Network Isolation",
        description="Req 1: Verify that network isolation is configured for agent sandboxes.",
        check_function_name="check_network_isolation",
        severity=Severity.CRITICAL,
    ),
    PolicyRule(
        rule_id="pci-crypto-01",
        framework=ComplianceFramework.PCI_DSS,
        title="Encryption at Rest",
        description="Req 3: Verify that stored data is encrypted at rest.",
        check_function_name="check_encryption_at_rest",
        severity=Severity.CRITICAL,
    ),
    PolicyRule(
        rule_id="pci-crypto-02",
        framework=ComplianceFramework.PCI_DSS,
        title="TLS for Data in Transit",
        description="Req 4: Verify that TLS is enforced for data transmission.",
        check_function_name="check_tls_enforced",
        severity=Severity.CRITICAL,
    ),
    PolicyRule(
        rule_id="pci-auth-01",
        framework=ComplianceFramework.PCI_DSS,
        title="Strong Authentication",
        description="Req 8: Verify that authentication and MFA are configured.",
        check_function_name="check_mfa_enabled",
        severity=Severity.CRITICAL,
    ),
    PolicyRule(
        rule_id="pci-auth-02",
        framework=ComplianceFramework.PCI_DSS,
        title="Password Policy",
        description="Req 8: Verify that minimum password length is enforced.",
        check_function_name="check_password_policy",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="pci-access-01",
        framework=ComplianceFramework.PCI_DSS,
        title="Access Controls",
        description="Req 7: Verify that role-based access controls restrict data access.",
        check_function_name="check_access_controls",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="pci-audit-01",
        framework=ComplianceFramework.PCI_DSS,
        title="Audit Trail",
        description="Req 10: Verify that audit logging captures access events.",
        check_function_name="check_audit_logging_enabled",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="pci-audit-02",
        framework=ComplianceFramework.PCI_DSS,
        title="Log Integrity",
        description="Req 10: Verify that log integrity protection is enabled.",
        check_function_name="check_logging_integrity",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="pci-scan-01",
        framework=ComplianceFramework.PCI_DSS,
        title="Vulnerability Scanning",
        description="Req 11: Verify that vulnerability scanning is configured.",
        check_function_name="check_vulnerability_scanning",
        severity=Severity.HIGH,
    ),
    # --- NIST 800-53 ---
    PolicyRule(
        rule_id="nist-ac-01",
        framework=ComplianceFramework.NIST_800_53,
        title="Access Control Policy",
        description="AC-1: Verify that access control policies are documented and enforced.",
        check_function_name="check_access_controls",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="nist-ac-02",
        framework=ComplianceFramework.NIST_800_53,
        title="Account Management",
        description="AC-2: Verify that authentication and session management are configured.",
        check_function_name="check_auth_configured",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="nist-ac-03",
        framework=ComplianceFramework.NIST_800_53,
        title="Session Management",
        description="AC-12: Verify that session timeouts are configured.",
        check_function_name="check_session_management",
        severity=Severity.MEDIUM,
    ),
    PolicyRule(
        rule_id="nist-au-01",
        framework=ComplianceFramework.NIST_800_53,
        title="Audit Events",
        description="AU-2: Verify that auditable events are being captured.",
        check_function_name="check_audit_logging_enabled",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="nist-au-02",
        framework=ComplianceFramework.NIST_800_53,
        title="Audit Record Integrity",
        description="AU-10: Verify that audit records are protected against tampering.",
        check_function_name="check_logging_integrity",
        severity=Severity.MEDIUM,
    ),
    PolicyRule(
        rule_id="nist-au-03",
        framework=ComplianceFramework.NIST_800_53,
        title="Audit Retention",
        description="AU-11: Verify that audit record retention is configured.",
        check_function_name="check_data_retention",
        severity=Severity.MEDIUM,
    ),
    PolicyRule(
        rule_id="nist-ia-01",
        framework=ComplianceFramework.NIST_800_53,
        title="Multi-Factor Authentication",
        description="IA-2: Verify that MFA is enabled for privileged accounts.",
        check_function_name="check_mfa_enabled",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="nist-sc-01",
        framework=ComplianceFramework.NIST_800_53,
        title="Encryption in Transit",
        description="SC-8: Verify that data in transit is encrypted.",
        check_function_name="check_tls_enforced",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="nist-sc-02",
        framework=ComplianceFramework.NIST_800_53,
        title="Encryption at Rest",
        description="SC-28: Verify that data at rest is encrypted.",
        check_function_name="check_encryption_at_rest",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="nist-cm-01",
        framework=ComplianceFramework.NIST_800_53,
        title="Configuration Management",
        description="CM-3: Verify that change management controls are in place.",
        check_function_name="check_change_management",
        severity=Severity.MEDIUM,
    ),
    PolicyRule(
        rule_id="nist-cp-01",
        framework=ComplianceFramework.NIST_800_53,
        title="Contingency Planning",
        description="CP-9: Verify that backup procedures are configured.",
        check_function_name="check_backup_configured",
        severity=Severity.MEDIUM,
    ),
    PolicyRule(
        rule_id="nist-ir-01",
        framework=ComplianceFramework.NIST_800_53,
        title="Incident Response",
        description="IR-1: Verify that incident response procedures exist.",
        check_function_name="check_incident_response_plan",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="nist-ra-01",
        framework=ComplianceFramework.NIST_800_53,
        title="Vulnerability Scanning",
        description="RA-5: Verify that vulnerability scanning is configured.",
        check_function_name="check_vulnerability_scanning",
        severity=Severity.MEDIUM,
    ),
    PolicyRule(
        rule_id="nist-sa-01",
        framework=ComplianceFramework.NIST_800_53,
        title="Supply Chain Risk",
        description="SA-12: Verify that dependencies are pinned and managed.",
        check_function_name="check_dependency_pinning",
        severity=Severity.MEDIUM,
    ),
    PolicyRule(
        rule_id="nist-sc-03",
        framework=ComplianceFramework.NIST_800_53,
        title="Rate Limiting",
        description="SC-5: Verify that rate limiting protects against denial-of-service.",
        check_function_name="check_rate_limiting",
        severity=Severity.MEDIUM,
    ),
    # --- HIPAA ---
    PolicyRule(
        rule_id="hipaa-access-01",
        framework=ComplianceFramework.HIPAA,
        title="Access Controls",
        description="164.312(a): Verify that access controls limit PHI access.",
        check_function_name="check_access_controls",
        severity=Severity.CRITICAL,
    ),
    PolicyRule(
        rule_id="hipaa-audit-01",
        framework=ComplianceFramework.HIPAA,
        title="Audit Controls",
        description="164.312(b): Verify that audit logging captures PHI access events.",
        check_function_name="check_audit_logging_enabled",
        severity=Severity.CRITICAL,
    ),
    PolicyRule(
        rule_id="hipaa-integrity-01",
        framework=ComplianceFramework.HIPAA,
        title="Integrity Controls",
        description="164.312(c): Verify that data integrity mechanisms are in place.",
        check_function_name="check_logging_integrity",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="hipaa-encrypt-01",
        framework=ComplianceFramework.HIPAA,
        title="Encryption at Rest",
        description="164.312(a)(2)(iv): Verify that PHI is encrypted at rest.",
        check_function_name="check_encryption_at_rest",
        severity=Severity.CRITICAL,
    ),
    PolicyRule(
        rule_id="hipaa-encrypt-02",
        framework=ComplianceFramework.HIPAA,
        title="Encryption in Transit",
        description="164.312(e): Verify that PHI is encrypted during transmission.",
        check_function_name="check_tls_enforced",
        severity=Severity.CRITICAL,
    ),
    PolicyRule(
        rule_id="hipaa-phi-01",
        framework=ComplianceFramework.HIPAA,
        title="PHI Detection",
        description="164.530(c): Verify that PHI/PII detection scanning is enabled.",
        check_function_name="check_phi_detection",
        severity=Severity.CRITICAL,
    ),
    PolicyRule(
        rule_id="hipaa-auth-01",
        framework=ComplianceFramework.HIPAA,
        title="Authentication",
        description="164.312(d): Verify that person authentication is configured.",
        check_function_name="check_auth_configured",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="hipaa-backup-01",
        framework=ComplianceFramework.HIPAA,
        title="Data Backup",
        description="164.308(a)(7): Verify that backup and recovery procedures exist.",
        check_function_name="check_backup_configured",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="hipaa-incident-01",
        framework=ComplianceFramework.HIPAA,
        title="Incident Response",
        description="164.308(a)(6): Verify that incident response procedures exist.",
        check_function_name="check_incident_response_plan",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="hipaa-secrets-01",
        framework=ComplianceFramework.HIPAA,
        title="Secrets Management",
        description="164.312(a)(2)(iv): Verify secrets and keys are managed securely.",
        check_function_name="check_secrets_management",
        severity=Severity.HIGH,
    ),
    # --- GDPR ---
    PolicyRule(
        rule_id="gdpr-privacy-01",
        framework=ComplianceFramework.GDPR,
        title="Privacy Policy",
        description="Art. 13/14: Verify that privacy policy documentation exists.",
        check_function_name="check_privacy_policy",
        severity=Severity.CRITICAL,
    ),
    PolicyRule(
        rule_id="gdpr-consent-01",
        framework=ComplianceFramework.GDPR,
        title="Consent Management",
        description="Art. 7: Verify that consent management processes are documented.",
        check_function_name="check_consent_management",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="gdpr-encrypt-01",
        framework=ComplianceFramework.GDPR,
        title="Encryption at Rest",
        description="Art. 32: Verify appropriate encryption of personal data at rest.",
        check_function_name="check_encryption_at_rest",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="gdpr-encrypt-02",
        framework=ComplianceFramework.GDPR,
        title="Encryption in Transit",
        description="Art. 32: Verify appropriate encryption of personal data in transit.",
        check_function_name="check_tls_enforced",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="gdpr-access-01",
        framework=ComplianceFramework.GDPR,
        title="Access Controls",
        description="Art. 32: Verify access controls protect personal data.",
        check_function_name="check_access_controls",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="gdpr-retention-01",
        framework=ComplianceFramework.GDPR,
        title="Data Retention Policy",
        description="Art. 5(1)(e): Verify data retention limits are defined.",
        check_function_name="check_data_retention",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="gdpr-audit-01",
        framework=ComplianceFramework.GDPR,
        title="Processing Records",
        description="Art. 30: Verify that processing activity logs exist.",
        check_function_name="check_audit_logging_enabled",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="gdpr-breach-01",
        framework=ComplianceFramework.GDPR,
        title="Breach Response",
        description="Art. 33/34: Verify incident response for data breach notification.",
        check_function_name="check_incident_response_plan",
        severity=Severity.HIGH,
    ),
    PolicyRule(
        rule_id="gdpr-classify-01",
        framework=ComplianceFramework.GDPR,
        title="Data Classification",
        description="Art. 9: Verify that personal data categories are classified.",
        check_function_name="check_data_classification",
        severity=Severity.MEDIUM,
    ),
]

# Index by framework for fast lookup.
_RULES_BY_FRAMEWORK: dict[ComplianceFramework, tuple[PolicyRule, ...]] = {}
for _rule in _ALL_RULES:
    _fw_list = list(_RULES_BY_FRAMEWORK.get(_rule.framework, ()))
    _fw_list.append(_rule)
    _RULES_BY_FRAMEWORK[_rule.framework] = tuple(_fw_list)

# Index by rule_id for fast lookup.
_RULES_BY_ID: dict[str, PolicyRule] = {r.rule_id: r for r in _ALL_RULES}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_framework_rules(framework: ComplianceFramework) -> tuple[PolicyRule, ...]:
    """Return all rules for a given compliance framework.

    Args:
        framework: The compliance framework to retrieve rules for.

    Returns:
        Tuple of ``PolicyRule`` instances for the framework.
    """
    return _RULES_BY_FRAMEWORK.get(framework, ())


def get_all_rules() -> tuple[PolicyRule, ...]:
    """Return all registered compliance rules across all frameworks."""
    return tuple(_ALL_RULES)


def get_registered_check_names() -> frozenset[str]:
    """Return the set of registered check function names."""
    return frozenset(_CHECK_FUNCTIONS.keys())


def get_rule_by_id(rule_id: str) -> PolicyRule | None:
    """Look up a rule by its unique ID.

    Args:
        rule_id: Unique rule identifier (e.g. ``soc2-audit-01``).

    Returns:
        The matching ``PolicyRule`` or ``None`` if not found.
    """
    return _RULES_BY_ID.get(rule_id)


def run_compliance_check(
    framework: ComplianceFramework,
    project_root: Path,
) -> ComplianceReport:
    """Run all rules for a framework against a project directory.

    Args:
        framework: The compliance framework to evaluate.
        project_root: Root directory of the Bernstein project.

    Returns:
        A :class:`ComplianceReport` summarising pass/fail for every rule.
    """
    rules = get_framework_rules(framework)
    results: list[PolicyResult] = []

    for rule in rules:
        check_fn = _CHECK_FUNCTIONS.get(rule.check_function_name)
        if check_fn is None:
            logger.warning("No check function found for %s", rule.check_function_name)
            result = PolicyResult(
                rule=rule,
                passed=False,
                evidence=f"Check function '{rule.check_function_name}' not registered.",
                remediation="Register the check function in _CHECK_FUNCTIONS.",
            )
        else:
            raw_result: PolicyResult = check_fn(project_root)
            # Rebind the result with the actual rule (check functions use placeholder).
            result = PolicyResult(
                rule=rule,
                passed=raw_result.passed,
                evidence=raw_result.evidence,
                remediation=raw_result.remediation,
            )
        results.append(result)

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    score = passed / len(results) if results else 0.0

    return ComplianceReport(
        framework=framework,
        rules_checked=len(results),
        rules_passed=passed,
        rules_failed=failed,
        results=tuple(results),
        score=score,
    )


def render_compliance_report(report: ComplianceReport) -> str:
    """Render a compliance report as Markdown.

    Args:
        report: The compliance report to render.

    Returns:
        A Markdown string with a pass/fail table and summary score.
    """
    lines: list[str] = []
    framework_label = report.framework.value.upper().replace("_", " ")
    lines.append(f"# {framework_label} Compliance Report")
    lines.append("")
    pct = report.score * 100
    lines.append(f"**Score:** {pct:.1f}% ({report.rules_passed}/{report.rules_checked} passed)")
    lines.append("")

    # Summary table
    lines.append("| Status | Rule ID | Title | Severity | Evidence |")
    lines.append("|--------|---------|-------|----------|----------|")
    for result in report.results:
        status = "PASS" if result.passed else "FAIL"
        icon = "+" if result.passed else "-"
        lines.append(
            f"| {icon} {status} | {result.rule.rule_id} | {result.rule.title} "
            f"| {result.rule.severity.value} | {result.evidence} |"
        )

    # Failed rules detail
    failed = [r for r in report.results if not r.passed]
    if failed:
        lines.append("")
        lines.append("## Failed Rules")
        lines.append("")
        for result in failed:
            lines.append(f"### {result.rule.rule_id}: {result.rule.title}")
            lines.append("")
            lines.append(f"- **Severity:** {result.rule.severity.value}")
            lines.append(f"- **Description:** {result.rule.description}")
            lines.append(f"- **Evidence:** {result.evidence}")
            lines.append(f"- **Remediation:** {result.remediation}")
            lines.append("")

    return "\n".join(lines)
