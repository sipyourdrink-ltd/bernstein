"""Audit check contract, registry, and producer adapters (#5072, #5076, #5091)."""

from __future__ import annotations

from bernstein.core.checks.adapters import (
    ComplianceEncryptionAtRestAdapter,
    DoctorComplianceAdapter,
)
from bernstein.core.checks.contract import (
    Check,
    Evidence,
    Finding,
    Verdict,
)
from bernstein.core.checks.formatters import (
    compute_audit_exit_code,
    findings_to_json,
    findings_to_sarif,
    verdict_to_kind,
)
from bernstein.core.checks.registry import (
    CheckRegistry,
    clear,
    get_check,
    iter_checks,
    register,
    run_all,
    unregister,
)
from bernstein.core.checks.sentinel import (
    SENTINEL_CHECK_ID,
    SENTINEL_ENV_VAR,
    SENTINEL_FILE_NAME,
    SENTINEL_REASON,
    AuditSentinelCheck,
    get_active_sentinel,
    is_sentinel_active,
    notify_audit_failures,
    record_audit_run,
)

__all__ = [
    "SENTINEL_CHECK_ID",
    "SENTINEL_ENV_VAR",
    "SENTINEL_FILE_NAME",
    "SENTINEL_REASON",
    "AuditSentinelCheck",
    "Check",
    "CheckRegistry",
    "ComplianceEncryptionAtRestAdapter",
    "DoctorComplianceAdapter",
    "Evidence",
    "Finding",
    "Verdict",
    "clear",
    "compute_audit_exit_code",
    "findings_to_json",
    "findings_to_sarif",
    "get_active_sentinel",
    "get_check",
    "is_sentinel_active",
    "iter_checks",
    "notify_audit_failures",
    "record_audit_run",
    "register",
    "run_all",
    "unregister",
    "verdict_to_kind",
]
