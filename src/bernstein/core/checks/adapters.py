"""Adapters wrapping existing diagnostic and compliance checks into the Check contract (#5072).

Adapts:
1. ``compliance_prerequisite_summary`` from :mod:`bernstein.core.compliance`, the
   same producer the ``bernstein doctor`` compliance row renders.
2. ``check_encryption_at_rest`` from :mod:`bernstein.core.security.compliance_library`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bernstein.core.checks.contract import Evidence, Finding, Verdict


class DoctorComplianceAdapter:
    """Adapter wrapping the doctor compliance prerequisite check into the Check contract."""

    @property
    def check_id(self) -> str:
        return "doctor:compliance"

    @property
    def title(self) -> str:
        return "Doctor Compliance Prerequisites"

    @property
    def description(self) -> str:
        return "Verify compliance mode configuration and prerequisite health."

    def run(self, workdir: Path | None = None) -> Finding:
        from bernstein.core.compliance import compliance_prerequisite_summary

        path = workdir or Path(".")
        summary = compliance_prerequisite_summary(path)

        if summary is None:
            return Finding(
                check_id=self.check_id,
                verdict=Verdict.NOT_MEASURABLE,
                what_would_make_it_measurable="No compliance configuration (.sdd or BERNSTEIN_COMPLIANCE) found",
                reason="missing_compliance_config",
                message="Compliance prerequisites not configured for this workspace.",
            )

        name, ok, detail, fix = summary
        payload: dict[str, Any] = {"name": name, "ok": ok, "detail": detail, "fix": fix}
        evidence = Evidence.from_payload(
            locator=f"doctor:compliance:{path}",
            payload=payload,
        )
        return Finding(
            check_id=self.check_id,
            verdict=Verdict.PASS if ok else Verdict.FAIL,
            evidence=(evidence,),
            message=detail,
            remediation=fix,
        )

    def __call__(self, workdir: Path | None = None) -> Finding:
        return self.run(workdir)


class ComplianceEncryptionAtRestAdapter:
    """Adapter wrapping ``check_encryption_at_rest`` from the compliance library."""

    @property
    def check_id(self) -> str:
        return "compliance:soc2:encryption_at_rest"

    @property
    def title(self) -> str:
        return "Compliance SOC2 Encryption At Rest"

    @property
    def description(self) -> str:
        return "Verify that state_encryption or compliance.encrypt_state_at_rest is configured."

    def run(self, workdir: Path | None = None) -> Finding:
        from bernstein.core.security.compliance_library import check_encryption_at_rest

        path = workdir or Path(".")
        result = check_encryption_at_rest(path)

        payload = {
            "passed": bool(result.passed),
            "evidence": str(result.evidence),
            "remediation": str(result.remediation),
        }
        evidence = Evidence.from_payload(
            locator=f"compliance:encryption_at_rest:{path}",
            payload=payload,
        )
        return Finding(
            check_id=self.check_id,
            verdict=Verdict.PASS if result.passed else Verdict.FAIL,
            evidence=(evidence,),
            message=str(result.evidence),
            remediation=str(result.remediation),
        )

    def __call__(self, workdir: Path | None = None) -> Finding:
        return self.run(workdir)
