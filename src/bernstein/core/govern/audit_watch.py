"""The ``govern audit`` watch cycle: detect, remediate once, re-verify, still fail.

Issue #5125. Detection without a bounded, logged response is a pager that never
stops ringing. This module is the loop that turns a repeating failure into *one*
attempted fix plus a visible record.

One tick runs the whole check set. A check that fails produces a
:class:`WatchFinding` carrying the health document read at detection time. If an
:class:`ApprovedRemediation` is bound to that check id, the plan is executed
exactly once and the check is re-run, so the finding also carries the health
document read after the attempt. Both snapshots are content-addressed, so an
operator reading the finding six weeks later can answer "what did it see before,
and what did it see after?" without re-running anything.

The property that is easy to get wrong: **a check remediated back to green still
fails the cycle it failed in**. The intuitive loop re-verifies, sees green and
exits 0 -- and an operator watching exit codes then never learns that anything
needed fixing. Here the finding is minted at detection, so the exit code of a
cycle is a fact about what failed, not about what is still failing at the end of
it.

This module owns the cycle only. It does not define what a check is beyond the
structural :class:`Check` protocol (that contract is #5072's), and it does not
author, approve or execute remediation plans -- an :class:`ApprovedRemediation`
is consumed as already approved and executed through the
:class:`RemediationRunner` seam that ``govern apply`` (#4982) satisfies.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from bernstein.core.lineage.spine import LineageSpine

#: Actor recorded on the lineage entry for a journaled remediation.
_JOURNAL_ACTOR = "bernstein.govern.audit"


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize *payload* to canonical JSON bytes (sorted keys, no padding)."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class HealthDocument:
    """What one check read about one surface at one moment.

    Attributes:
        check_id: Stable id of the check that produced this document.
        passed: Whether the check judged the surface healthy.
        observed: Everything the check read, verbatim. Kept whole rather than
            summarised: a finding that only carried "failed" would not tell a
            later reader what changed between the before and after snapshots.
        timestamp: Integer timestamp of the observation.
    """

    check_id: str
    passed: bool
    observed: dict[str, Any]
    timestamp: int

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "check_id": self.check_id,
            "passed": self.passed,
            "observed": self.observed,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> HealthDocument:
        """Rebuild a HealthDocument from a serialized dict."""
        return cls(
            check_id=str(raw["check_id"]),
            passed=bool(raw["passed"]),
            observed=dict(raw.get("observed", {})),
            timestamp=int(raw["timestamp"]),
        )

    def content_hash(self) -> str:
        """Return the ``sha256:``-prefixed content address of this document."""
        return "sha256:" + hashlib.sha256(_canonical_bytes(self.to_dict())).hexdigest()


class Check(Protocol):
    """A check the watch cycle can run and re-run.

    The full check contract -- areas, the three-state verdict, evidence pairs --
    is #5072's. The cycle needs only an identity and something it can call
    twice.
    """

    check_id: str

    def run(self) -> HealthDocument:
        """Read the surface and report its health."""
        ...  # pragma: no cover - protocol


@dataclass(frozen=True, slots=True)
class ApprovedRemediation:
    """A remediation plan a human has already approved, bound to one check.

    This module never mints one: approval happens in ``govern apply`` (#4982).
    A plan reaching the cycle is, by construction, already reviewed.

    Attributes:
        check_id: The check this plan is bound to. A plan runs only for its
            own check.
        plan_id: Identity of the approved plan, recorded on the finding.
        approved_by: Who approved it.
        approved_at: Integer timestamp of the approval.
    """

    check_id: str
    plan_id: str
    approved_by: str
    approved_at: int

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "check_id": self.check_id,
            "plan_id": self.plan_id,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }


@dataclass(frozen=True, slots=True)
class RemediationOutcome:
    """What executing an approved plan reported.

    ``ok`` is the executor's own verdict on the run; it is deliberately not the
    same question as "is the check green now", which only the re-verification
    answers.
    """

    plan_id: str
    ok: bool
    detail: str


class RemediationRunner(Protocol):
    """Executes an approved remediation plan.

    ``govern apply`` (#4982) is the implementation; the cycle holds only this
    seam so it can ship and be tested before that command exists.
    """

    def run(self, remediation: ApprovedRemediation) -> RemediationOutcome:
        """Execute *remediation* and report what happened."""
        ...  # pragma: no cover - protocol


@dataclass(frozen=True, slots=True)
class WatchFinding:
    """One check that failed in one cycle, with what was done about it.

    Attributes:
        check_id: The check that failed.
        health_before: The health document read at detection time.
        health_after: The health document read after the remediation attempt,
            or ``None`` when no plan was bound to the check and nothing was
            attempted.
        plan_id: The approved plan that was run, or ``None``.
        remediation_attempted: Whether a plan was executed for this finding.
        remediation_detail: The executor's own account of the attempt, or the
            exception that ended it. Empty when nothing was attempted.
        tick: The 1-based cycle this finding was minted in.
    """

    check_id: str
    health_before: HealthDocument
    health_after: HealthDocument | None
    plan_id: str | None
    remediation_attempted: bool
    remediation_detail: str
    tick: int

    @property
    def remediated_to_green(self) -> bool:
        """Whether a remediation ran and the re-verification then passed."""
        return self.remediation_attempted and self.health_after is not None and self.health_after.passed

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "check_id": self.check_id,
            "health_before": self.health_before.to_dict(),
            "health_after": self.health_after.to_dict() if self.health_after is not None else None,
            "plan_id": self.plan_id,
            "remediation_attempted": self.remediation_attempted,
            "remediation_detail": self.remediation_detail,
            "remediated_to_green": self.remediated_to_green,
            "tick": self.tick,
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialize the finding to canonical JSON bytes.

        This is the form hashed into the lineage spine, so two replays over the
        same observations journal byte-identical entries.
        """
        return _canonical_bytes(self.to_dict())

    def content_hash(self) -> str:
        """Return the ``sha256:``-prefixed content address of this finding."""
        return "sha256:" + hashlib.sha256(self.to_canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class CycleResult:
    """The outcome of one tick.

    Attributes:
        tick: The 1-based cycle number.
        checks_run: The check ids run this cycle, in order.
        findings: One finding per check that failed at detection time.
    """

    tick: int
    checks_run: tuple[str, ...]
    findings: tuple[WatchFinding, ...]

    @property
    def exit_code(self) -> int:
        """Non-zero when anything failed in this cycle, remediated or not.

        A finding is minted at detection, so a check fixed inside the cycle
        keeps its finding and the cycle keeps failing. An operator reading exit
        codes alone sees that something needed fixing, not just that it is fine
        now.
        """
        return 1 if self.findings else 0


class AuditWatch:
    """Runs a check set on a schedule and responds once to each failure.

    Args:
        checks: The checks to run each tick, in order.
        remediations: Already-approved plans, at most one per check id. A check
            with no plan is only ever reported.
        runner: Executes approved plans. Required whenever *remediations* is
            non-empty.
        spine: Lineage spine to journal attempted remediations into. ``None``
            disables journaling.
        clock: Source of the cycle timestamp, injected so a replay journals the
            same bytes.

    Raises:
        ValueError: When *remediations* is non-empty and no *runner* was given,
            or when two plans claim the same check id.
    """

    def __init__(
        self,
        *,
        checks: Iterable[Check],
        remediations: Iterable[ApprovedRemediation] = (),
        runner: RemediationRunner | None = None,
        spine: LineageSpine | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._checks = tuple(checks)
        plans: dict[str, ApprovedRemediation] = {}
        for plan in remediations:
            if plan.check_id in plans:
                msg = f"two approved plans bound to check {plan.check_id!r}"
                raise ValueError(msg)
            plans[plan.check_id] = plan
        if plans and runner is None:
            msg = "approved remediations were given with no runner to execute them"
            raise ValueError(msg)
        self._plans = plans
        self._runner = runner
        self._spine = spine
        self._clock = clock
        self._tick = 0

    def tick(self) -> CycleResult:
        """Run every check once, respond to each failure, and report the cycle."""
        self._tick += 1
        findings: list[WatchFinding] = []
        for check in self._checks:
            health = check.run()
            if health.passed:
                continue
            findings.append(self._respond(check, health))
        return CycleResult(
            tick=self._tick,
            checks_run=tuple(check.check_id for check in self._checks),
            findings=tuple(findings),
        )

    def watch(
        self,
        *,
        interval: float,
        once: bool = False,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> Iterator[CycleResult]:
        """Yield one :class:`CycleResult` per tick, sleeping *interval* between.

        Args:
            interval: Seconds to wait between ticks.
            once: Run a single tick and stop, for a cron-driven harness.
            sleeper: The wait between ticks, injected so the interval is
                assertable without wall-clock timing.
        """
        while True:
            yield self.tick()
            if once:
                return
            sleeper(interval)

    def _respond(self, check: Check, health_before: HealthDocument) -> WatchFinding:
        """Attempt the approved plan for *check*, if any, and re-verify."""
        plan = self._plans.get(check.check_id)
        runner = self._runner
        # ``runner is None`` cannot happen while a plan exists -- ``__init__``
        # rejects that wiring -- but folding it in here keeps the "only report"
        # path the single answer to "nothing can be attempted".
        if plan is None or runner is None:
            return WatchFinding(
                check_id=check.check_id,
                health_before=health_before,
                health_after=None,
                plan_id=None,
                remediation_attempted=False,
                remediation_detail="",
                tick=self._tick,
            )

        try:
            detail = runner.run(plan).detail
        except Exception as exc:
            # A plan that explodes ends the attempt, not the watch loop: the
            # exception becomes the record instead of the last thing the
            # operator sees.
            detail = f"{type(exc).__name__}: {exc}"

        finding = WatchFinding(
            check_id=check.check_id,
            health_before=health_before,
            health_after=check.run(),
            plan_id=plan.plan_id,
            remediation_attempted=True,
            remediation_detail=detail,
            tick=self._tick,
        )
        self._journal(finding)
        return finding

    def _journal(self, finding: WatchFinding) -> None:
        """Anchor an attempted remediation in the lineage spine."""
        if self._spine is None:
            return
        self._spine.record(
            artifact_path=f"governance-audit-remediation-{finding.check_id}.json",
            content=finding.to_canonical_bytes(),
            actor=_JOURNAL_ACTOR,
            step_id=finding.plan_id or "",
            model="none",
            timestamp=int(self._clock()),
        )


__all__ = [
    "ApprovedRemediation",
    "AuditWatch",
    "Check",
    "CycleResult",
    "HealthDocument",
    "RemediationOutcome",
    "RemediationRunner",
    "WatchFinding",
]
