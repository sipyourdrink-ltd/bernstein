"""Adapters wrapping existing diagnostic and compliance checks into the Check contract (#5072).

Adapts:
1. ``compliance_prerequisite_summary`` from :mod:`bernstein.core.compliance`, the
   same producer the ``bernstein doctor`` compliance row renders.
2. ``check_encryption_at_rest`` from :mod:`bernstein.core.security.compliance_library`.
3. Each check of the duplication collector in
   :mod:`bernstein.core.govern.duplication_audit` (#5105).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.checks.contract import Evidence, Finding, Verdict

if TYPE_CHECKING:
    from bernstein.core.govern.duplication_audit import DuplicationReport


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


#: The duplication collector's checks, each reported by ``govern audit`` as
#: ``duplication:<name>`` (#5105). Pinned rather than read from the collector so
#: a check id cannot appear or vanish from the audit without this list, and the
#: id-stability tombstones, saying so.
DUPLICATION_CHECK_NAMES: tuple[str, ...] = (
    "inline-canonical-bytes-sites",
    "orphan-modules",
    "private-hmac-key-loaders",
    "receipt-verify-kinds",
    "registry-duplicate-ids",
    "verification-result-shapes",
)


@lru_cache(maxsize=4)
def _duplication_report(source_root: str) -> DuplicationReport:
    """One walk of *source_root* shared by every duplication check in a run.

    The collector parses the whole package, a few seconds of work, and six
    adapters asking for one finding each would otherwise repeat it six times.
    Keyed on the root, which for the installed package does not change while
    the process runs.
    """
    from bernstein.core.govern.duplication_audit import collect_duplication

    return collect_duplication(Path(source_root))


def _installed_package_root() -> Path:
    import bernstein

    return Path(bernstein.__file__).resolve().parent


class DuplicationAuditAdapter:
    """One duplication-collector check under the Check contract (#5105).

    It measures the installed ``bernstein`` package, not ``--workdir``: the
    question is how many implementations of one capability this install
    carries, which no project tree can change. That is also why it is
    ``opt_in``: it runs under ``govern audit --only duplication`` and never in
    a default audit, which would otherwise fail every workspace for something
    only a release can fix.

    A measured count over the expected one is ``fail`` with the offending
    paths, a count within it is ``pass``, and a check whose subject does not
    exist yet is ``not_measurable`` with the collector's own reason. The
    collector carries no score or grade, and neither does the finding.
    """

    #: Runs only when ``--only duplication`` names its area; see the class docstring.
    opt_in = True

    def __init__(self, name: str, source_root: Path | None = None) -> None:
        self._name = name
        self._source_root = source_root

    @property
    def check_id(self) -> str:
        return f"duplication:{self._name}"

    @property
    def title(self) -> str:
        return f"Duplicate implementations: {self._name}"

    @property
    def description(self) -> str:
        return (
            "Count implementations of one capability in the installed package against the expected count "
            "(opt-in: --only duplication)."
        )

    def run(self, workdir: Path | None = None) -> Finding:
        from bernstein.core.govern.duplication_audit import Verdict as DuplicationVerdict

        del workdir  # the install is measured, not the project; see the class docstring
        root = self._source_root if self._source_root is not None else _installed_package_root()
        report = _duplication_report(str(root))
        matches = [f for f in report.findings if f.check_id == self._name]
        if not matches:
            raise LookupError(f"the duplication collector no longer reports '{self._name}'")
        found = matches[0]

        if found.verdict is DuplicationVerdict.NOT_YET_MEASURABLE:
            return Finding(
                check_id=self.check_id,
                verdict=Verdict.NOT_MEASURABLE,
                what_would_make_it_measurable=found.summary,
                # The summary already names what would make it measurable, and
                # the report prints that beside the message; say it once.
                message="Not yet measurable; nothing was counted.",
            )

        # Locator names the check, never the path of this install, so two
        # installs of one release produce the same evidence bytes.
        evidence = Evidence.from_payload(locator=self.check_id, payload=found.to_dict())
        count_line = f"{found.count} found, expected at most {found.expected}"
        if found.verdict is DuplicationVerdict.MEASURED_PASSED:
            return Finding(
                check_id=self.check_id,
                verdict=Verdict.PASS,
                evidence=(evidence,),
                message=f"{count_line}: {found.summary}",
            )
        shown = ", ".join(found.paths[:5])
        more = f" and {len(found.paths) - 5} more" if len(found.paths) > 5 else ""
        return Finding(
            check_id=self.check_id,
            verdict=Verdict.FAIL,
            evidence=(evidence,),
            message=f"{count_line}: {found.summary}",
            remediation=f"Consolidate onto one implementation. Offending: {shown}{more}.",
        )

    def __call__(self, workdir: Path | None = None) -> Finding:
        return self.run(workdir)
