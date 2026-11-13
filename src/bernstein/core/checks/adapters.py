"""Adapters wrapping existing diagnostic and compliance checks into the Check contract (#5072).

Adapts:
1. ``compliance_prerequisite_summary`` from :mod:`bernstein.core.compliance`, the
   same producer the ``bernstein doctor`` compliance row renders.
2. ``check_encryption_at_rest`` from :mod:`bernstein.core.security.compliance_library`.
3. Each check of the duplication collector in
   :mod:`bernstein.core.govern.duplication_audit` (#5105).
"""

from __future__ import annotations

import os
from functools import lru_cache, wraps
from inspect import signature
from pathlib import Path
from typing import TYPE_CHECKING, Any, ParamSpec

from bernstein.core.checks.contract import Evidence, Finding, Verdict

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from bernstein.core.govern.duplication_audit import DuplicationReport


_P = ParamSpec("_P")


def _installation_finding(
    check_id: str,
    rows: list[dict[str, Any]],
    observation: Any = None,
    env: Mapping[str, str] | None = None,
) -> Finding:
    """Bind a doctor's display rows to one stable, evidence-backed outcome."""
    prerequisite = ""
    if not rows:
        prerequisite = (
            "Install a tracked adapter CLI (e.g. agy) with a readable version to measure its posture."
            if check_id in {"INS-003", "INS-004"}
            else "Configure this check's subject to collect installation observations."
        )
    elif check_id == "INS-005":
        import shutil

        auth_tools = {"Auth: claude": "claude", "Auth: codex": "codex", "Auth: gemini": "gcloud"}
        missing = [
            auth_tools[row["name"]]
            for row in rows
            # Codex's configured-model fallback can succeed after a missing
            # CLI probe. An API-key observation does not require that probe.
            if (not row["ok"] or (row["name"] == "Auth: codex" and row["detail"].startswith("config.toml (")))
            and shutil.which(auth_tools[row["name"]], path=env.get("PATH") if env is not None else None) is None
        ]
        if missing:
            prerequisite = f"Install authentication probe tools and ensure they are on PATH: {', '.join(missing)}."
    elif check_id == "INS-004" and observation is not None:
        unknown = [
            f"{entry['adapter']} --version" for entry in observation["entries"] if entry["verdict"] == "unknown_version"
        ]
        if unknown:
            prerequisite = f"Make adapter version probes runnable with readable version output: {', '.join(unknown)}."
    elif check_id == "INS-010":
        prerequisite = "; ".join(f"{row['name']}: {row['detail']}. {row['fix']}" for row in rows if not row["ok"])
    elif check_id == "INS-015":
        import shutil

        if shutil.which("git") is None:
            prerequisite = "Install git and ensure it is on PATH to measure commit attribution."
    elif check_id == "INS-011" and any("asyncpg not installed" in row["detail"] for row in rows):
        prerequisite = "Install asyncpg (bernstein[postgres]) to measure PostgreSQL connectivity."
    elif check_id == "INS-012" and any("CLI (op) not installed" in row["detail"] for row in rows):
        prerequisite = "Install the 1Password CLI (op) to measure secrets provider connectivity."

    summary = "; ".join(f"{row['name']}: {row['detail']}" for row in rows)
    payload = {"checks": rows}
    if check_id == "INS-004" and observation is not None:
        # A receipt includes the observation time. Hash the measured versions,
        # not that changing timestamp, while doctor still displays the receipt.
        payload = observation
        summary = rows[0]["detail"].split("; receipt sha256:", 1)[0]
    verdict = Verdict.PASS if all(row["ok"] for row in rows) else Verdict.FAIL
    if prerequisite:
        verdict = Verdict.NOT_MEASURABLE
    return Finding(
        check_id=check_id,
        area="INS",
        verdict=verdict,
        evidence=(Evidence.from_payload(check_id, payload),),
        summary=summary or "No installation observations collected.",
        remediation="; ".join(row["fix"] for row in rows if row["fix"]),
        what_would_make_it_measurable=prerequisite or None,
    )


def doctor_producer(check_id: str) -> Callable[[Callable[_P, Any]], Callable[_P, Finding]]:
    """Turn a legacy row emitter into a Finding producer, retaining its rows.

    Both the list and callback forms used by doctor keep the same display
    data. The return value is always the audit-contract Finding.
    """

    def decorate(emit: Callable[_P, Any]) -> Callable[_P, Finding]:
        parameters = signature(emit)

        @wraps(emit)
        def produce(*args: _P.args, **kwargs: _P.kwargs) -> Finding:
            bound = parameters.bind(*args, **kwargs)
            rows = bound.arguments.get("checks")
            if rows is None:
                rows = []
                callback = bound.arguments["_check"]

                def record(*values: Any, **named: Any) -> None:
                    row = {"fix": "", "fix_id": ""}
                    row.update(zip(("name", "ok", "detail", "fix", "fix_id"), values, strict=False))
                    row.update(named)
                    rows.append(row)
                    callback(*values, **named)

                bound.arguments["_check"] = record
            start = len(rows)
            observation = emit(*bound.args, **bound.kwargs)
            return _installation_finding(check_id, rows[start:], observation, bound.arguments.get("env"))

        produce.__annotations__ = {**emit.__annotations__, "return": Finding}
        produce.__signature__ = parameters.replace(return_annotation=Finding)  # type: ignore[attr-defined]
        produce.check_id = check_id  # type: ignore[attr-defined]
        return produce

    return decorate


class AuditSentinelCheck:
    """Harmless, explicitly labelled failure for the audit selftest (#5091)."""

    check_id = "sentinel:failure"
    area = "sentinel"
    title = "Governance Audit Failure Sentinel"
    description = "Force a synthetic measured failure with BERNSTEIN_GOVERN_AUDIT_SENTINEL=1."
    env_var = "BERNSTEIN_GOVERN_AUDIT_SENTINEL"
    failure_reason = "Injected governance audit failure (selftest sentinel)."

    @property
    def active(self) -> bool:
        return os.environ.get(self.env_var) == "1"

    @property
    def opt_in(self) -> bool:
        # An inactive sentinel must not change the default check set.
        return not self.active

    def run(self, workdir: Path | None = None) -> Finding:
        active = self.active
        return Finding(
            check_id=self.check_id,
            verdict=Verdict.MEASURED,
            passed=not active,
            reason=self.failure_reason if active else None,
            message=f"{self.env_var}=1 active: {self.failure_reason}" if active else "Selftest sentinel inactive.",
            remediation=f"Unset {self.env_var}." if active else "",
            evidence=(Evidence.from_payload(f"env://{self.env_var}", {"active": active}),),
        )

    def __call__(self, workdir: Path | None = None) -> Finding:
        return self.run(workdir)


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


class DeclarationQuarantineAdapter:
    """Expose the same persisted quarantine finding as doctor."""

    check_id = "doctor:declaration-quarantine"
    title = "Declaration quarantine"
    description = "Report declarations disabled after initialization failure."

    def run(self, workdir: Path | None = None) -> Finding:
        from bernstein.core.security.declaration_quarantine import quarantine_summary

        path = workdir or Path.cwd()
        _name, ok, detail, fix = quarantine_summary(path)
        return Finding(
            check_id=self.check_id,
            verdict=Verdict.PASS if ok else Verdict.FAIL,
            evidence=(Evidence.from_payload(locator=f"{self.check_id}:{path}", payload={"detail": detail}),),
            message=detail,
            remediation=fix,
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
