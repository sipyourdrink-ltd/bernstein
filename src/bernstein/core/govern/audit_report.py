"""The ``govern audit`` report as a chain-anchored artefact (issue #5077).

A report is not a printout of what a run happened to observe. Its canonical
bytes are a deterministic function of the findings it carries, so its sha256
identifies a posture: two audits over an unchanged install produce the same
bytes and therefore the same identity.

That identity is anchored in the lineage spine over exactly those bytes, the
way :class:`~bernstein.core.security.governance.GovernanceDecision` is -- the
report's ``journal_entry_hash`` is the spine entry hash over its canonical
bytes. A stored report whose bytes were edited no longer hashes to any spine
entry, so tampering makes it *unverifiable* rather than merely different.

Because both audits are anchored artefacts, "what changed since the last
audit" is :func:`diff_reports` over two stored reports -- a comparison of two
hashes, not a re-run of the checks.

The report envelope deliberately does not define what a check is. Findings are
carried as the serialised records their producers emit; this module requires
only that each record carries a unique ``id`` (so the drift diff is
well-defined) and reads ``verdict`` and ``evidence`` when computing drift.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.lineage.spine import LineageSpine, content_hash_of

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Version stamped into every report preimage. Bump only on a wire-format change.
GOVERN_AUDIT_SCHEMA_VERSION = 1

#: The spine run id every audit report anchors to. One run id keeps successive
#: audits on one chain, so a drift comparison is a single chain walk.
GOVERN_AUDIT_RUN_ID = "govern-audit"

#: Actor recorded on the spine entry that anchors a report.
GOVERN_AUDIT_ACTOR = "bernstein.govern.audit"

#: Model string recorded on report spine entries (no model runs at audit time;
#: the field is part of the spine schema).
_GOVERN_AUDIT_MODEL = "none"

#: Sub-path (relative to the audit run's spine dir) the persisted reports land
#: in, colocated with the spine so the artefact and its anchor share one root.
_REPORT_SUBPATH = ("reports",)


def _canonical_bytes(payload: Any) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _finding_id(finding: dict[str, Any]) -> str:
    """Return *finding*'s stable id, rejecting a record that has none."""
    raw = finding.get("id")
    if not isinstance(raw, str) or not raw:
        raise ValueError("every audit finding must carry a non-empty string finding id")
    return raw


def evidence_hash(finding: dict[str, Any]) -> str:
    """Return the content hash of *finding*'s evidence list.

    Shape-agnostic: whatever the producer records under ``evidence`` is hashed
    canonically, so drift detects a re-read that returned different bytes
    without this module knowing how evidence is spelled.
    """
    return _sha256(finding.get("evidence", []))


@dataclass(frozen=True, slots=True)
class FindingDrift:
    """One finding whose verdict or evidence differs between two reports.

    Attributes:
        finding_id: The stable finding id that drifted.
        change: ``verdict`` when the verdict differs (checked first, so a
            finding that changed both is reported as a verdict change),
            ``evidence`` when only the evidence hash differs, ``appeared`` when
            the id is new in the later report, ``disappeared`` when it is gone.
        before_verdict: The earlier verdict (empty when the finding appeared).
        after_verdict: The later verdict (empty when the finding disappeared).
        before_evidence_hash: Hash of the earlier evidence (empty when appeared).
        after_evidence_hash: Hash of the later evidence (empty when disappeared).
    """

    finding_id: str
    change: str
    before_verdict: str = ""
    after_verdict: str = ""
    before_evidence_hash: str = ""
    after_evidence_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the serialised drift row."""
        return {
            "finding_id": self.finding_id,
            "change": self.change,
            "before_verdict": self.before_verdict,
            "after_verdict": self.after_verdict,
            "before_evidence_hash": self.before_evidence_hash,
            "after_evidence_hash": self.after_evidence_hash,
        }


@dataclass(frozen=True, slots=True)
class AuditReport:
    """One ``govern audit`` run, as an anchored artefact.

    ``journal_entry_hash`` anchors the report in the lineage spine over the
    report's canonical bytes -- its chain-verifiable identity. It is not part
    of those bytes, exactly as on ``GovernanceDecision``.

    Attributes:
        findings: The serialised finding records this audit produced. Order is
            the producer's business: the canonical form sorts them by id, so a
            registry that enumerates checks in a different order still emits
            byte-identical bytes.
        timestamp: Integer timestamp; caller-chosen but stable so identical
            fixtures anchor byte-identically.
        inventory_hash: Content hash of the inventory this audit ran over
            (empty when the audit was not scoped to one).
        inventory_anchor: The spine entry hash anchoring that inventory, so
            "audited what was enumerated on that date" is one chain walk.
        journal_entry_hash: The lineage-spine entry hash anchoring this report.
            Empty until :func:`anchor_audit_report` records it.
    """

    findings: tuple[dict[str, Any], ...]
    timestamp: int
    inventory_hash: str = ""
    inventory_anchor: str = ""
    journal_entry_hash: str = ""

    def _canonical_findings(self) -> list[dict[str, Any]]:
        """Return the findings in canonical order, rejecting duplicate ids."""
        seen: set[str] = set()
        for finding in self.findings:
            fid = _finding_id(finding)
            if fid in seen:
                raise ValueError(f"duplicate finding id in audit report: {fid}")
            seen.add(fid)
        return sorted(self.findings, key=_finding_id)

    def _binding(self) -> dict[str, Any]:
        """Return the anchored binding (everything except the anchor itself)."""
        return {
            "v": GOVERN_AUDIT_SCHEMA_VERSION,
            "findings": self._canonical_findings(),
            "timestamp": self.timestamp,
            "inventory_hash": self.inventory_hash,
            "inventory_anchor": self.inventory_anchor,
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes (spine-hashed)."""
        return _canonical_bytes(self._binding())

    def report_hash(self) -> str:
        """Return the ``sha256:``-prefixed identity of this posture."""
        return "sha256:" + hashlib.sha256(self.to_canonical_bytes()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Return the serialised report, anchor included."""
        return self._binding() | {"journal_entry_hash": self.journal_entry_hash}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> AuditReport:
        """Rebuild a report from a serialised row."""
        raw_findings: object = row.get("findings") or []
        if not isinstance(raw_findings, list):
            raise ValueError("audit report findings must be a list")
        rows = cast("list[dict[str, Any]]", raw_findings)
        return cls(
            findings=tuple(dict(f) for f in rows),
            timestamp=int(row["timestamp"]),
            inventory_hash=str(row.get("inventory_hash", "")),
            inventory_anchor=str(row.get("inventory_anchor", "")),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
        )

    def finding_by_id(self, finding_id: str) -> dict[str, Any] | None:
        """Return the finding record with *finding_id*, or ``None``."""
        for finding in self.findings:
            if finding.get("id") == finding_id:
                return finding
        return None


def diff_reports(before: AuditReport, after: AuditReport) -> tuple[FindingDrift, ...]:
    """Return the findings that drifted between two anchored reports.

    Drift is a comparison of two artefacts, never a re-run: a finding appears
    only when its verdict or the hash of the evidence it read changed, or when
    the id entered or left the check set. A report emitted at a later timestamp
    over an unchanged install produces no drift rows.

    Rows are ordered by finding id so two operators holding the same pair of
    reports print the same list.
    """
    before_map = {_finding_id(f): f for f in before.findings}
    after_map = {_finding_id(f): f for f in after.findings}

    drift: list[FindingDrift] = []
    for fid in sorted(before_map.keys() | after_map.keys()):
        old = before_map.get(fid)
        new = after_map.get(fid)
        if old is None and new is not None:
            drift.append(
                FindingDrift(
                    finding_id=fid,
                    change="appeared",
                    after_verdict=str(new.get("verdict", "")),
                    after_evidence_hash=evidence_hash(new),
                )
            )
            continue
        if new is None and old is not None:
            drift.append(
                FindingDrift(
                    finding_id=fid,
                    change="disappeared",
                    before_verdict=str(old.get("verdict", "")),
                    before_evidence_hash=evidence_hash(old),
                )
            )
            continue
        if old is None or new is None:  # pragma: no cover - both sides present here
            continue
        old_verdict, new_verdict = str(old.get("verdict", "")), str(new.get("verdict", ""))
        old_evidence, new_evidence = evidence_hash(old), evidence_hash(new)
        if old_verdict == new_verdict and old_evidence == new_evidence:
            continue
        drift.append(
            FindingDrift(
                finding_id=fid,
                change="verdict" if old_verdict != new_verdict else "evidence",
                before_verdict=old_verdict,
                after_verdict=new_verdict,
                before_evidence_hash=old_evidence,
                after_evidence_hash=new_evidence,
            )
        )
    return tuple(drift)


# ---------------------------------------------------------------------------
# Persistence (colocated with the audit spine)
# ---------------------------------------------------------------------------


def reports_dir(lineage_root: Path) -> Path:
    """Return the directory holding persisted audit reports."""
    return lineage_root / GOVERN_AUDIT_RUN_ID / _REPORT_SUBPATH[0]


def _report_filename(report: AuditReport, seq: int) -> str:
    """Return a stable, ordered artefact filename for *report*.

    ``seq`` is a zero-padded monotonic index so reports sort in emit order, and
    the report-hash fragment keeps names distinct within a timestamp.
    """
    digest = report.report_hash()
    frag = digest[7:23] if digest.startswith("sha256:") else digest[:16]
    return f"{seq:06d}-{frag}.json"


def _next_seq(out_dir: Path) -> int:
    """Return the next zero-based emit index for *out_dir* (append order)."""
    if not out_dir.is_dir():
        return 0
    return sum(1 for _ in out_dir.glob("*.json"))


def anchor_audit_report(
    *,
    lineage_root: Path,
    hmac_key: bytes,
    report: AuditReport,
) -> AuditReport:
    """Anchor *report* in the audit spine and persist it. Returns the anchored copy.

    The report's canonical bytes are what the spine hashes, so the returned
    record's ``journal_entry_hash`` is the spine entry hash over exactly those
    bytes.
    """
    out_dir = reports_dir(lineage_root)
    filename = _report_filename(report, _next_seq(out_dir))
    artifact_path = "/".join((*_REPORT_SUBPATH, filename))

    anchor = LineageSpine(lineage_root, run_id=GOVERN_AUDIT_RUN_ID, hmac_key=hmac_key).record(
        artifact_path=artifact_path,
        content=report.to_canonical_bytes(),
        actor=GOVERN_AUDIT_ACTOR,
        step_id=report.report_hash(),
        model=_GOVERN_AUDIT_MODEL,
        timestamp=report.timestamp,
    )
    anchored = AuditReport(
        findings=report.findings,
        timestamp=report.timestamp,
        inventory_hash=report.inventory_hash,
        inventory_anchor=report.inventory_anchor,
        journal_entry_hash=anchor,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / filename).write_text(
        json.dumps(anchored.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return anchored


def read_audit_reports(lineage_root: Path) -> list[AuditReport]:
    """Load every persisted audit report (append order)."""
    out_dir = reports_dir(lineage_root)
    if not out_dir.is_dir():
        return []
    reports: list[AuditReport] = []
    for path in sorted(out_dir.glob("*.json")):
        try:
            reports.append(AuditReport.from_dict(json.loads(path.read_bytes())))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("govern audit: malformed report at %s", path)
            continue
    return reports


def read_audit_report(lineage_root: Path, report_hash: str) -> AuditReport | None:
    """Return the stored report whose canonical bytes hash to *report_hash*.

    The lookup recomputes each stored report's hash rather than trusting a
    filename, so a report is addressable only by the posture it actually
    records: edited bytes address nothing and this returns ``None``.

    *report_hash* may be the full ``sha256:``-prefixed digest or a bare hex
    prefix of at least eight characters.
    """
    wanted = report_hash.removeprefix("sha256:").strip().lower()
    if len(wanted) < 8:
        return None
    for report in read_audit_reports(lineage_root):
        try:
            digest = report.report_hash().removeprefix("sha256:")
        except ValueError:
            continue
        if digest.startswith(wanted):
            return report
    return None


# ---------------------------------------------------------------------------
# Offline verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditReportVerifyResult:
    """Outcome of verifying one stored audit report.

    Attributes:
        ok: True only when the spine is intact, the report's canonical bytes
            are anchored in it, the recorded anchor is that entry, and any
            named inventory anchor is an entry on the same chain.
        reason: Why verification failed (empty when ``ok``).
        report: The report that was checked, when one was loaded.
    """

    ok: bool
    reason: str = ""
    report: AuditReport | None = None
    errors: tuple[str, ...] = field(default_factory=tuple)


def _recompute_anchor(spine: LineageSpine, canonical: bytes) -> str | None:
    """Return the spine entry hash whose content matches ``canonical`` bytes."""
    want = content_hash_of(canonical)
    for entry in spine.iter_entries():
        if entry.content_hash == want:
            return entry.entry_hash
    return None


def verify_audit_report(
    *,
    lineage_root: Path,
    hmac_key: bytes,
    report: AuditReport,
) -> AuditReportVerifyResult:
    """Prove offline that *report* is the artefact the chain anchored.

    Recomputes, from the stored report and the spine alone:

    * the whole audit spine;
    * the spine entry whose content hash is the report's canonical bytes -- a
      byte-flipped report matches no entry and is unverifiable, not merely
      different;
    * the recorded ``journal_entry_hash`` against that entry;
    * the named ``inventory_anchor``, when present, as an entry on the same
      chain, so a report cannot claim an inventory the chain never saw.
    """
    spine = LineageSpine(lineage_root, run_id=GOVERN_AUDIT_RUN_ID, hmac_key=hmac_key)
    spine_result = spine.verify()
    if not spine_result.ok:
        return AuditReportVerifyResult(
            ok=False,
            reason=f"govern-audit spine failed verification ({spine_result.status.value})",
            report=report,
        )

    try:
        canonical = report.to_canonical_bytes()
    except ValueError as exc:
        return AuditReportVerifyResult(ok=False, reason=f"report is malformed ({exc})", report=report)

    recomputed = _recompute_anchor(spine, canonical)
    if recomputed is None:
        return AuditReportVerifyResult(
            ok=False,
            reason="report is not anchored in the govern-audit spine",
            report=report,
        )
    if recomputed != report.journal_entry_hash:
        return AuditReportVerifyResult(
            ok=False,
            reason="recorded journal_entry_hash does not match the spine anchor over the report bytes",
            report=report,
        )

    if report.inventory_anchor:
        anchors = {entry.entry_hash for entry in spine.iter_entries()}
        if report.inventory_anchor not in anchors:
            return AuditReportVerifyResult(
                ok=False,
                reason="referenced inventory anchor is not an entry on the govern-audit spine",
                report=report,
            )

    return AuditReportVerifyResult(ok=True, report=report)


def verify_all_audit_reports(workdir: Path, *, hmac_key: bytes) -> list[AuditReportVerifyResult]:
    """Verify every stored audit report under ``workdir/.sdd/lineage``.

    Used by ``bernstein audit verify`` so a tampered posture report is detected
    exactly like a tampered chain entry. Returns one result per stored report
    (empty list when no reports exist).
    """
    lineage_root = workdir / ".sdd" / "lineage"
    out_dir = reports_dir(lineage_root)
    if not out_dir.is_dir():
        return []
    results: list[AuditReportVerifyResult] = []
    for path in sorted(out_dir.glob("*.json")):
        try:
            report = AuditReport.from_dict(json.loads(path.read_bytes()))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            results.append(AuditReportVerifyResult(ok=False, reason=f"malformed report at {path.name}"))
            continue
        results.append(verify_audit_report(lineage_root=lineage_root, hmac_key=hmac_key, report=report))
    return results


__all__ = [
    "GOVERN_AUDIT_ACTOR",
    "GOVERN_AUDIT_RUN_ID",
    "GOVERN_AUDIT_SCHEMA_VERSION",
    "AuditReport",
    "AuditReportVerifyResult",
    "FindingDrift",
    "anchor_audit_report",
    "diff_reports",
    "evidence_hash",
    "read_audit_report",
    "read_audit_reports",
    "reports_dir",
    "verify_all_audit_reports",
    "verify_audit_report",
]
