"""Run one check set over a selected target set and collect every result (#5090).

``bernstein governance verify`` runs one check against exactly one target and
has no fleet dimension. ``bernstein fleet bulk-stop`` resolves a target set and
reports succeeded/failed as two collections without aborting on the first
failure, but for live fleet projects rather than governed surfaces. This module
is the aggregation contract between the two: the same check set, run on every
selected target, with every result collected.

Four properties hold:

* **A fleet run of one target is byte-identical to the local run of that
  target.** Both paths call :func:`audit_target`; the sweep is that function
  with a target loop around it. "Why did this fail on host X" is answered by
  re-running the exact check that failed, not a fleet-flavoured cousin of it.
* **One unreachable target hides nothing on the other N-1.** An executor that
  raises records every requested check for that target as ``not_measurable``
  with the reason ``unreachable`` -- never as a pass, never as an absent row.
  A check the executor simply did not report is recorded the same way.
* **One journal entry per sweep, not per target.** :func:`record_sweep`
  anchors a single entry carrying the check-set version, the selector and the
  counts by outcome, so "how many sweeps have run and what changed between
  them" is a short readable series.
* **A repeated failure escalates instead of repeating.** A target failing the
  same check on consecutive sweeps opens a governance decision record; the
  identical finding is not raised a second time.

Transport is deliberately out of scope. ``core/protocols/ssh_backend.py`` is a
single-host agent-spawn backend bound to one ``remote:`` config block, not a
fleet transport, so the sweep takes a caller-supplied executor: any transport
-- in-process, subprocess, or one built on that SSH backend -- plugs in as that
callable.

The check contract itself belongs to #5072 and the target selector to #5116.
:class:`CheckOutcome` here is the minimal shape the aggregation needs, named
after #5072's acceptance criteria so the two meet without translation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.lineage.spine import LineageSpine

#: Summary recorded for every check of a target the executor could not reach.
UNREACHABLE = "unreachable"

#: Summary recorded for a requested check the executor returned no result for.
NOT_REPORTED = "not_reported"

#: Artifact path the per-sweep journal entry is anchored under.
JOURNAL_ARTIFACT_PATH = "govern-audit-sweep.json"


class CheckVerdict(Enum):
    """The three things an audit can say about a check.

    ``MEASURED`` -- the check ran and read evidence; it also carries ``passed``.
    ``DECLARED`` -- the operator asserted it; nothing was read.
    ``NOT_MEASURABLE`` -- the check could not run, and says what would change that.
    """

    MEASURED = "measured"
    DECLARED = "declared"
    NOT_MEASURABLE = "not_measurable"


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """One check's result on one target.

    Attributes:
        check_id: Stable, area-namespaced check identifier (``MDL-001``).
        area: The area the check belongs to.
        verdict: One of :class:`CheckVerdict`.
        passed: Set only for a measured verdict; ``None`` otherwise.
        summary: One line describing what was found.
        remediation: What to do about it.
        evidence: ``(locator, sha256)`` pairs naming what was read.
        what_would_make_it_measurable: Required for a not-measurable verdict.
    """

    check_id: str
    area: str
    verdict: CheckVerdict
    passed: bool | None = None
    summary: str = ""
    remediation: str = ""
    evidence: tuple[tuple[str, str], ...] = ()
    what_would_make_it_measurable: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "check_id": self.check_id,
            "area": self.area,
            "verdict": self.verdict.value,
            "passed": self.passed,
            "summary": self.summary,
            "remediation": self.remediation,
            "evidence": [list(pair) for pair in self.evidence],
            "what_would_make_it_measurable": self.what_would_make_it_measurable,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CheckOutcome:
        """Rebuild a CheckOutcome from a serialized dict."""
        passed = raw.get("passed")
        return cls(
            check_id=str(raw["check_id"]),
            area=str(raw.get("area", "")),
            verdict=CheckVerdict(str(raw["verdict"])),
            passed=None if passed is None else bool(passed),
            summary=str(raw.get("summary", "")),
            remediation=str(raw.get("remediation", "")),
            evidence=tuple((str(a), str(b)) for a, b in raw.get("evidence", [])),
            what_would_make_it_measurable=str(raw.get("what_would_make_it_measurable", "")),
        )


@dataclass(frozen=True, slots=True)
class TargetProbe:
    """What an executor returns for one target.

    Attributes:
        outcomes: The check results the target produced. A requested check
            missing from this sequence is recorded as not-measurable, not
            dropped.
        components: ``component -> version`` for the plugins and skills in use
            on the target, the input to the cross-target version-skew check.
    """

    outcomes: Sequence[CheckOutcome] = ()
    components: Mapping[str, str] = field(default_factory=dict[str, str])


#: Runs the check set on one target. Raising means the target is unreachable.
TargetExecutor = Callable[[str], TargetProbe]


@dataclass(frozen=True, slots=True)
class TargetAudit:
    """One target's complete report: every requested check, in a stable order.

    Attributes:
        target: The target identifier the selector resolved.
        outcomes: One outcome per requested check, ordered by ``check_id``.
        components: ``(component, version)`` pairs, ordered by component.
        reachable: False when the executor raised for this target.
    """

    target: str
    outcomes: tuple[CheckOutcome, ...]
    components: tuple[tuple[str, str], ...] = ()
    reachable: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "target": self.target,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "components": [list(pair) for pair in self.components],
            "reachable": self.reachable,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TargetAudit:
        """Rebuild a TargetAudit from a serialized dict."""
        return cls(
            target=str(raw["target"]),
            outcomes=tuple(CheckOutcome.from_dict(o) for o in raw.get("outcomes", [])),
            components=tuple((str(a), str(b)) for a, b in raw.get("components", [])),
            reachable=bool(raw.get("reachable", True)),
        )

    def to_canonical_bytes(self) -> bytes:
        """Serialize to canonical JSON bytes (sorted keys, minimal separators)."""
        return _canonical_bytes(self.to_dict())

    def content_hash(self) -> str:
        """Return the ``sha256:``-prefixed content address of this report."""
        return "sha256:" + hashlib.sha256(self.to_canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class VersionSkew:
    """How many distinct versions of one component the selected set runs.

    Attributes:
        component: The plugin or skill identifier.
        distinct_versions: Count of distinct versions across reached targets.
        versions: ``(version, targets)`` pairs, both sides ordered.
        outliers: Targets not on the most-used version, ordered.
    """

    component: str
    distinct_versions: int
    versions: tuple[tuple[str, tuple[str, ...]], ...]
    outliers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "component": self.component,
            "distinct_versions": self.distinct_versions,
            "versions": [[version, list(targets)] for version, targets in self.versions],
            "outliers": list(self.outliers),
        }


@dataclass(frozen=True, slots=True)
class SweepJournalEntry:
    """The one entry a sweep writes, whatever its target count.

    Attributes:
        check_set_version: Version of the check set that ran.
        selector: The selector string the target set was resolved from.
        target_count: How many targets the sweep covered.
        unreachable_targets: How many of those could not be reached.
        counts: ``(outcome, count)`` pairs, ordered by outcome name.
        timestamp: Integer timestamp of the sweep.
    """

    check_set_version: str
    selector: str
    target_count: int
    unreachable_targets: int
    counts: tuple[tuple[str, int], ...]
    timestamp: int

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "check_set_version": self.check_set_version,
            "selector": self.selector,
            "target_count": self.target_count,
            "unreachable_targets": self.unreachable_targets,
            "counts": [list(pair) for pair in self.counts],
            "timestamp": self.timestamp,
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialize to canonical JSON bytes (sorted keys, minimal separators)."""
        return _canonical_bytes(self.to_dict())


@dataclass(frozen=True, slots=True)
class SweepReport:
    """Every target's report plus the sweep-level aggregates.

    Attributes:
        targets: Per-target reports, in the order the targets were given.
        journal: The single journal entry describing this sweep.
        version_skew: One entry per component seen on a reached target.
    """

    targets: tuple[TargetAudit, ...]
    journal: SweepJournalEntry
    version_skew: tuple[VersionSkew, ...]

    def target(self, name: str) -> TargetAudit | None:
        """Return one target's report, or None when it was not in the sweep."""
        for audit in self.targets:
            if audit.target == name:
                return audit
        return None

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "targets": [t.to_dict() for t in self.targets],
            "journal": self.journal.to_dict(),
            "version_skew": [s.to_dict() for s in self.version_skew],
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialize to canonical JSON bytes (sorted keys, minimal separators)."""
        return _canonical_bytes(self.to_dict())

    def content_hash(self) -> str:
        """Return the ``sha256:``-prefixed content address of this report."""
        return "sha256:" + hashlib.sha256(self.to_canonical_bytes()).hexdigest()


def audit_target(
    *,
    target: str,
    check_ids: Iterable[str],
    executor: TargetExecutor,
) -> TargetAudit:
    """Run the check set on one target and report every requested check.

    This is the single-target path. The sweep is this function with a target
    loop around it, which is what makes a fleet run of one target byte-identical
    to a local run of that target.

    An executor that raises means the target could not be reached: every
    requested check is recorded as not-measurable with the reason
    ``unreachable``. An executor that returns without a result for a requested
    check gets the same treatment with the reason ``not_reported``. Neither is
    ever recorded as a pass, and neither is ever left out of the report.

    Args:
        target: The target identifier.
        check_ids: The check set to run. Duplicates are collapsed.
        executor: Runs the check set on *target*.

    Returns:
        The target's report, with outcomes ordered by ``check_id``.
    """
    requested = tuple(dict.fromkeys(check_ids))

    try:
        probe = executor(target)
    except Exception as exc:  # an unreachable target is data, not a crash
        return TargetAudit(
            target=target,
            outcomes=tuple(_unmeasurable(cid, UNREACHABLE, _unreachable_hint(exc)) for cid in sorted(requested)),
            components=(),
            reachable=False,
        )

    reported = {outcome.check_id: outcome for outcome in probe.outcomes}
    for check_id in requested:
        if check_id not in reported:
            reported[check_id] = _unmeasurable(
                check_id,
                NOT_REPORTED,
                "the check set must return a result for this id",
            )

    return TargetAudit(
        target=target,
        outcomes=tuple(reported[cid] for cid in sorted(reported)),
        components=tuple(sorted((str(k), str(v)) for k, v in probe.components.items())),
        reachable=True,
    )


def run_audit_sweep(
    *,
    targets: Iterable[str],
    check_ids: Iterable[str],
    executor: TargetExecutor,
    selector: str,
    check_set_version: str,
    timestamp: int,
) -> SweepReport:
    """Run the identical check set on every target and collect every result.

    No target's failure ends the sweep: one unreachable target must not hide
    findings on the other N-1.

    Args:
        targets: Target identifiers, already resolved by the selector.
            Duplicates are collapsed; the given order is preserved.
        check_ids: The check set to run on each target.
        executor: Runs the check set on one target.
        selector: The selector string the targets were resolved from, recorded
            in the journal entry.
        check_set_version: Version of the check set, recorded in the journal.
        timestamp: Integer timestamp of the sweep.

    Returns:
        Every target's report plus the sweep-level journal entry and skew.
    """
    resolved = tuple(dict.fromkeys(targets))
    checks = tuple(dict.fromkeys(check_ids))

    audits = tuple(audit_target(target=t, check_ids=checks, executor=executor) for t in resolved)

    journal = SweepJournalEntry(
        check_set_version=check_set_version,
        selector=selector,
        target_count=len(audits),
        unreachable_targets=sum(1 for a in audits if not a.reachable),
        counts=_count_outcomes(audits),
        timestamp=timestamp,
    )
    return SweepReport(targets=audits, journal=journal, version_skew=compute_version_skew(audits))


def compute_version_skew(audits: Sequence[TargetAudit]) -> tuple[VersionSkew, ...]:
    """Report how many distinct versions of each component the set runs.

    Only reached targets contribute: a target that could not be reached has no
    observed version, and an absent observation is not evidence of agreement.

    Args:
        audits: The per-target reports of one sweep.

    Returns:
        One entry per component, ordered by component name. ``outliers`` names
        the targets that are not on the most-used version -- empty when the set
        agrees. Ties are broken by version string so the answer is stable.
    """
    by_component: dict[str, dict[str, list[str]]] = {}
    for audit in audits:
        if not audit.reachable:
            continue
        for component, version in audit.components:
            by_component.setdefault(component, {}).setdefault(version, []).append(audit.target)

    skew: list[VersionSkew] = []
    for component in sorted(by_component):
        versions = by_component[component]
        majority = sorted(versions.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0][0]
        outliers = sorted(t for version, hosts in versions.items() if version != majority for t in hosts)
        skew.append(
            VersionSkew(
                component=component,
                distinct_versions=len(versions),
                versions=tuple((v, tuple(sorted(hosts))) for v, hosts in sorted(versions.items())),
                outliers=tuple(outliers),
            )
        )
    return tuple(skew)


def record_sweep(
    report: SweepReport,
    *,
    spine: LineageSpine,
    timestamp: int,
    actor: str = "bernstein.govern",
) -> str:
    """Anchor exactly one journal entry for the whole sweep.

    Args:
        report: The sweep to journal.
        spine: The lineage spine to append to.
        timestamp: Integer timestamp for the entry.
        actor: Recorded actor.

    Returns:
        The entry hash of the appended journal entry.
    """
    return spine.record(
        artifact_path=JOURNAL_ARTIFACT_PATH,
        content=report.journal.to_canonical_bytes(),
        actor=actor,
        step_id=report.content_hash(),
        model="none",
        timestamp=timestamp,
    )


# -- Repeat-failure escalation ---------------------------------------------


@dataclass(frozen=True, slots=True)
class FailureLedgerEntry:
    """How long one (target, check) pair has been failing.

    Attributes:
        consecutive: Consecutive sweeps this check failed on this target.
        decision_open: Whether a decision record has already been opened.
    """

    consecutive: int
    decision_open: bool


@dataclass(frozen=True, slots=True)
class SweepDecisionRecord:
    """A governance decision opened because a failure repeated.

    Attributes:
        target: The target the check keeps failing on.
        check_id: The check that keeps failing.
        consecutive_failures: How many consecutive sweeps it has failed on.
        summary: The failing outcome's summary at the time it was opened.
        opened_at: Integer timestamp of the sweep that opened it.
    """

    target: str
    check_id: str
    consecutive_failures: int
    summary: str
    opened_at: int

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "target": self.target,
            "check_id": self.check_id,
            "consecutive_failures": self.consecutive_failures,
            "summary": self.summary,
            "opened_at": self.opened_at,
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialize to canonical JSON bytes (sorted keys, minimal separators)."""
        return _canonical_bytes(self.to_dict())

    def content_hash(self) -> str:
        """Return the ``sha256:``-prefixed content address of this record."""
        return "sha256:" + hashlib.sha256(self.to_canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class FailureLedger:
    """What each (target, check) pair looked like on the previous sweep.

    Attributes:
        entries: ``target -> check_id -> entry``.
    """

    entries: Mapping[str, Mapping[str, FailureLedgerEntry]] = field(
        default_factory=dict[str, Mapping[str, FailureLedgerEntry]]
    )

    def get(self, target: str, check_id: str) -> FailureLedgerEntry:
        """Return the recorded state, or a zeroed entry when there is none."""
        return self.entries.get(target, {}).get(check_id, FailureLedgerEntry(consecutive=0, decision_open=False))

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            target: {
                check_id: {"consecutive": e.consecutive, "decision_open": e.decision_open}
                for check_id, e in sorted(checks.items())
            }
            for target, checks in sorted(self.entries.items())
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FailureLedger:
        """Rebuild a ledger from a serialized dict."""
        entries: dict[str, dict[str, FailureLedgerEntry]] = {}
        for target, checks in raw.items():
            entries[str(target)] = {
                str(check_id): FailureLedgerEntry(
                    consecutive=int(state.get("consecutive", 0)),
                    decision_open=bool(state.get("decision_open", False)),
                )
                for check_id, state in checks.items()
            }
        return cls(entries=entries)

    @classmethod
    def load(cls, path: Path) -> FailureLedger:
        """Read the ledger from *path*, or return an empty one when absent."""
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        """Write the ledger to *path*, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class EscalationResult:
    """What a sweep raises, what it escalates, and the state to carry forward.

    Attributes:
        raised_findings: ``(target, check_id)`` pairs failing for the first
            time -- the findings this sweep reports.
        decisions: Decision records opened because a failure repeated.
        ledger: The ledger to persist for the next sweep.
    """

    raised_findings: tuple[tuple[str, str], ...]
    decisions: tuple[SweepDecisionRecord, ...]
    ledger: FailureLedger


def escalate_repeat_failures(
    *,
    report: SweepReport,
    ledger: FailureLedger,
    timestamp: int,
) -> EscalationResult:
    """Raise first failures, escalate repeats, and stay quiet after that.

    A check failing for the first time on a target is a finding. The same check
    failing on the next sweep opens a governance decision record instead of a
    second identical finding. Once that decision is open, further sweeps report
    neither -- the decision is the open item, not a growing pile of duplicates.

    A check that could not be measured -- an unreachable target, a check the
    executor did not report -- leaves the streak untouched. It is not a pass,
    so it cannot clear a pending failure; and it is not a failure, so it cannot
    advance one either. Only a measured pass clears the streak.

    Args:
        report: The sweep just completed.
        ledger: State from the previous sweep.
        timestamp: Integer timestamp of this sweep.

    Returns:
        The findings, the decisions, and the ledger for the next sweep.
    """
    entries: dict[str, dict[str, FailureLedgerEntry]] = {
        target: dict(checks) for target, checks in ledger.entries.items()
    }
    findings: list[tuple[str, str]] = []
    decisions: list[SweepDecisionRecord] = []

    for audit in report.targets:
        for outcome in audit.outcomes:
            if outcome.verdict is not CheckVerdict.MEASURED:
                continue
            if outcome.passed:
                entries.get(audit.target, {}).pop(outcome.check_id, None)
                continue

            previous = ledger.get(audit.target, outcome.check_id)
            consecutive = previous.consecutive + 1
            decision_open = previous.decision_open

            if consecutive == 1:
                findings.append((audit.target, outcome.check_id))
            elif not decision_open:
                decisions.append(
                    SweepDecisionRecord(
                        target=audit.target,
                        check_id=outcome.check_id,
                        consecutive_failures=consecutive,
                        summary=outcome.summary,
                        opened_at=timestamp,
                    )
                )
                decision_open = True

            entries.setdefault(audit.target, {})[outcome.check_id] = FailureLedgerEntry(
                consecutive=consecutive,
                decision_open=decision_open,
            )

    pruned = {target: checks for target, checks in entries.items() if checks}
    return EscalationResult(
        raised_findings=tuple(findings),
        decisions=tuple(decisions),
        ledger=FailureLedger(entries=pruned),
    )


# -- Internals -------------------------------------------------------------


def _unmeasurable(check_id: str, summary: str, hint: str) -> CheckOutcome:
    return CheckOutcome(
        check_id=check_id,
        area="",
        verdict=CheckVerdict.NOT_MEASURABLE,
        passed=None,
        summary=summary,
        what_would_make_it_measurable=hint,
    )


def _unreachable_hint(exc: BaseException) -> str:
    return f"a reachable target; the executor raised {type(exc).__name__}"


def _count_outcomes(audits: Sequence[TargetAudit]) -> tuple[tuple[str, int], ...]:
    counts = {"measured_pass": 0, "measured_fail": 0, "declared": 0, "not_measurable": 0}
    for audit in audits:
        for outcome in audit.outcomes:
            if outcome.verdict is CheckVerdict.DECLARED:
                counts["declared"] += 1
            elif outcome.verdict is CheckVerdict.NOT_MEASURABLE:
                counts["not_measurable"] += 1
            elif outcome.passed:
                counts["measured_pass"] += 1
            else:
                counts["measured_fail"] += 1
    return tuple(sorted(counts.items()))


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


__all__ = [
    "JOURNAL_ARTIFACT_PATH",
    "NOT_REPORTED",
    "UNREACHABLE",
    "CheckOutcome",
    "CheckVerdict",
    "EscalationResult",
    "FailureLedger",
    "FailureLedgerEntry",
    "SweepDecisionRecord",
    "SweepJournalEntry",
    "SweepReport",
    "TargetAudit",
    "TargetExecutor",
    "TargetProbe",
    "VersionSkew",
    "audit_target",
    "compute_version_skew",
    "escalate_repeat_failures",
    "record_sweep",
    "run_audit_sweep",
]
