"""Injectable failure sentinel for govern audit (#5091).

Proves the detect -> record -> notify path end to end by deliberately forcing
a named check to report `measured, failed` with a fixed reason without
breaking real system state.

The sentinel's presence is itself reported in run outputs so a forgotten
sentinel cannot pass for a real organic failure.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

from bernstein.core.checks.contract import Evidence, Finding, Verdict

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

SENTINEL_ENV_VAR = "BERNSTEIN_AUDIT_SENTINEL"
SENTINEL_FILE_NAME = "audit_failure.sentinel"
SENTINEL_CHECK_ID = "sentinel:injected_failure"
SENTINEL_REASON = "InjectedAuditFailureSentinel"


def get_active_sentinel(workdir: Path | None = None) -> str | None:
    """Return description of active sentinel source, or None if inactive.

    Canonical source is the environment variable ``BERNSTEIN_AUDIT_SENTINEL``.
    File sentinel ``.sdd/sentinel/audit_failure.sentinel`` or ``.sdd/audit_failure.sentinel``
    is also checked for persistence across subprocess boundaries.
    """
    env_val = os.environ.get(SENTINEL_ENV_VAR)
    if env_val:
        return f"env {SENTINEL_ENV_VAR}={env_val}"

    if workdir is not None:
        candidate_paths = [
            workdir / ".sdd" / "sentinel" / SENTINEL_FILE_NAME,
            workdir / ".sdd" / SENTINEL_FILE_NAME,
        ]
        for p in candidate_paths:
            if p.is_file():
                return f"file {p}"

    return None


def is_sentinel_active(workdir: Path | None = None) -> bool:
    """Check whether the failure injection sentinel is active."""
    return get_active_sentinel(workdir) is not None


class AuditSentinelCheck:
    """Audit check producer that fails when the sentinel is active."""

    @property
    def check_id(self) -> str:
        return SENTINEL_CHECK_ID

    @property
    def title(self) -> str:
        return "Injectable failure sentinel"

    @property
    def description(self) -> str:
        return "Deliberately injects a measured failure to verify the detect -> record -> notify path."

    def run(self, workdir: Path | None = None) -> Finding:
        active_source = get_active_sentinel(workdir)
        if active_source:
            evidence = Evidence.from_bytes(
                locator=f"sentinel://{SENTINEL_CHECK_ID}",
                raw=f"active:{active_source}".encode(),
            )
            return Finding(
                check_id=self.check_id,
                verdict=Verdict.FAIL,
                evidence=(evidence,),
                reason=SENTINEL_REASON,
                message=f"Injected failure sentinel is active via {active_source}",
                summary="Injected failure sentinel active",
                remediation=f"Clear {SENTINEL_ENV_VAR} or remove {SENTINEL_FILE_NAME}",
                area="sentinel",
                passed=False,
            )

        evidence = Evidence.from_bytes(
            locator=f"sentinel://{SENTINEL_CHECK_ID}",
            raw=b"inactive",
        )
        return Finding(
            check_id=self.check_id,
            verdict=Verdict.PASS,
            evidence=(evidence,),
            message="Injected failure sentinel is inactive",
            summary="Failure sentinel inactive",
            area="sentinel",
            passed=True,
        )

    def __call__(self, workdir: Path | None = None) -> Finding:
        return self.run(workdir)


def record_audit_run(
    findings: Sequence[Finding],
    *,
    workdir: Path,
    run_id: str = "govern-audit",
    timestamp: float | None = None,
) -> dict[str, Any] | None:
    """Record an audit run into the lineage journal under .sdd/lineage/govern-audit.

    Returns the recorded journal entry dict if .sdd directory exists, or None.
    """
    sdd_dir = workdir / ".sdd"
    if not sdd_dir.is_dir():
        return None

    now = timestamp if timestamp is not None else time.time()
    journal_dir = sdd_dir / "lineage" / run_id
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal_file = journal_dir / "events.jsonl"

    failures = [f.to_dict() for f in findings if f.verdict == Verdict.FAIL or f.passed is False]
    sentinel_active = is_sentinel_active(workdir)

    entry = {
        "event_type": "audit.run",
        "run_id": run_id,
        "timestamp": now,
        "total_checks": len(findings),
        "failures_count": len(failures),
        "sentinel_active": sentinel_active,
        "failures": failures,
    }

    with journal_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return entry


def notify_audit_failures(
    findings: Sequence[Finding],
    *,
    workdir: Path | None = None,
) -> list[dict[str, Any]]:
    """Dispatch/collect notifications for any failed audit findings.

    Returns a list of notification payload dictionaries.
    """
    notifications: list[dict[str, Any]] = []
    for f in findings:
        if f.verdict == Verdict.FAIL or f.passed is False:
            notif = {
                "check_id": f.id,
                "verdict": f.verdict.value,
                "summary": f.summary or f.message,
                "reason": f.reason,
                "remediation": f.remediation,
                "sentinel": f.reason == SENTINEL_REASON,
            }
            notifications.append(notif)
    return notifications
