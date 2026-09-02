"""Tests for the govern audit watch cycle (issue #5125).

The properties under test are the cycle's, not any individual check's: that a
tick re-runs the whole check set, that a failed check with an approved
remediation bound to it gets exactly one attempt plus a re-verification, that
both health documents survive onto the finding, and -- the one that is easy to
get wrong -- that a check which was remediated back to green still fails the
cycle it failed in.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from bernstein.core.govern.audit_watch import (
    ApprovedRemediation,
    AuditWatch,
    HealthDocument,
    RemediationOutcome,
)
from bernstein.core.lineage.spine import LineageSpine

_KEY = b"0" * 32
_NOW = 1_700_000_000


class _Check:
    """A check whose verdict is read from a list of scripted outcomes."""

    def __init__(self, check_id: str, verdicts: list[bool]) -> None:
        self.check_id = check_id
        self._verdicts = verdicts
        self.runs = 0

    def run(self) -> HealthDocument:
        passed = self._verdicts[min(self.runs, len(self._verdicts) - 1)]
        self.runs += 1
        return HealthDocument(
            check_id=self.check_id,
            passed=passed,
            observed={"run": self.runs, "state": "green" if passed else "red"},
            timestamp=_NOW + self.runs,
        )


class _Runner:
    """A remediation runner that flips its bound check green when it succeeds."""

    def __init__(self, *, ok: bool = True, flips: _Check | None = None) -> None:
        self.calls: list[ApprovedRemediation] = []
        self._ok = ok
        self._flips = flips

    def run(self, remediation: ApprovedRemediation) -> RemediationOutcome:
        self.calls.append(remediation)
        if self._ok and self._flips is not None:
            self._flips._verdicts = [True]
            self._flips.runs = 0
        return RemediationOutcome(
            plan_id=remediation.plan_id,
            ok=self._ok,
            detail="applied" if self._ok else "refused",
        )


class _RaisingRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, remediation: ApprovedRemediation) -> RemediationOutcome:
        self.calls += 1
        raise RuntimeError("plan execution exploded")


def _approved(check_id: str, plan_id: str = "plan-1") -> ApprovedRemediation:
    return ApprovedRemediation(
        check_id=check_id,
        plan_id=plan_id,
        approved_by="operator@example.com",
        approved_at=_NOW,
    )


def test_watch_ticks_on_interval_and_reruns_check_set() -> None:
    """Every tick re-runs every check, and the loop sleeps the given interval."""
    slept: list[float] = []
    first = _Check("MDL-001", [True])
    second = _Check("OBS-004", [True])
    watch = AuditWatch(checks=[first, second])

    results = list(itertools.islice(watch.watch(interval=5.0, sleeper=slept.append), 3))

    assert len(results) == 3
    assert [r.tick for r in results] == [1, 2, 3]
    assert first.runs == 3
    assert second.runs == 3
    assert all(r.checks_run == ("MDL-001", "OBS-004") for r in results)
    # Three ticks, two gaps between them.
    assert slept == [5.0, 5.0]


def test_watch_once_runs_a_single_tick_and_never_sleeps() -> None:
    """``once`` is the cron-driven escape: one tick, no interval wait."""
    slept: list[float] = []
    check = _Check("MDL-001", [True])
    watch = AuditWatch(checks=[check])

    results = list(watch.watch(interval=5.0, once=True, sleeper=slept.append))

    assert len(results) == 1
    assert check.runs == 1
    assert slept == []


def test_passing_check_set_exits_zero_and_records_no_finding() -> None:
    """A cycle with nothing failing is the only cycle that exits zero."""
    watch = AuditWatch(checks=[_Check("MDL-001", [True])])

    result = watch.tick()

    assert result.findings == ()
    assert result.exit_code == 0


def test_check_with_no_approved_plan_only_reports() -> None:
    """No plan bound to the check means no remediation attempt, ever."""
    check = _Check("OBS-004", [False])
    runner = _Runner(flips=check)
    watch = AuditWatch(checks=[check], remediations=[], runner=runner)

    result = watch.tick()

    assert runner.calls == []
    assert check.runs == 1, "a check with no plan is never re-verified"
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.remediation_attempted is False
    assert finding.plan_id is None
    assert finding.health_after is None
    assert result.exit_code == 1


def test_finding_carries_health_document_before_and_after() -> None:
    """Both snapshots are present on a remediated finding, and distinguishable."""
    check = _Check("OBS-004", [False])
    runner = _Runner(flips=check)
    watch = AuditWatch(checks=[check], remediations=[_approved("OBS-004")], runner=runner)

    finding = watch.tick().findings[0]

    assert finding.health_before.passed is False
    assert finding.health_before.observed["state"] == "red"
    assert finding.health_after is not None
    assert finding.health_after.passed is True
    assert finding.health_after.observed["state"] == "green"
    assert finding.health_before.content_hash() != finding.health_after.content_hash()


def test_remediated_then_green_check_still_fails_the_cycle() -> None:
    """A check fixed inside the cycle still fails that cycle.

    The intuitive-but-wrong loop re-verifies, sees green, and exits 0 -- which
    hides from an operator reading exit codes that anything needed fixing.
    """
    check = _Check("OBS-004", [False])
    runner = _Runner(flips=check)
    watch = AuditWatch(checks=[check], remediations=[_approved("OBS-004")], runner=runner)

    result = watch.tick()

    assert len(runner.calls) == 1
    finding = result.findings[0]
    assert finding.remediated_to_green is True
    assert result.exit_code == 1


def test_failed_remediation_is_attempted_exactly_once_per_cycle() -> None:
    """One bounded attempt per cycle, whether or not it worked."""
    check = _Check("OBS-004", [False])
    runner = _Runner(ok=False, flips=check)
    watch = AuditWatch(checks=[check], remediations=[_approved("OBS-004")], runner=runner)

    result = watch.tick()

    assert len(runner.calls) == 1
    finding = result.findings[0]
    assert finding.remediation_attempted is True
    assert finding.remediated_to_green is False
    assert finding.health_after is not None
    assert finding.health_after.passed is False
    assert result.exit_code == 1


def test_raising_remediation_runner_is_recorded_not_propagated() -> None:
    """A plan that explodes ends the attempt, not the watch loop."""
    check = _Check("OBS-004", [False])
    runner = _RaisingRunner()
    watch = AuditWatch(checks=[check], remediations=[_approved("OBS-004")], runner=runner)

    result = watch.tick()

    assert runner.calls == 1
    finding = result.findings[0]
    assert finding.remediation_attempted is True
    assert "RuntimeError" in finding.remediation_detail
    assert result.exit_code == 1


def test_remediation_is_journaled_to_the_lineage_spine(tmp_path: Path) -> None:
    """The remediated finding lands on the chain, verifiable offline."""
    check = _Check("OBS-004", [False])
    runner = _Runner(flips=check)
    spine = LineageSpine(tmp_path / ".sdd" / "lineage", run_id="govern-audit", hmac_key=_KEY)
    watch = AuditWatch(
        checks=[check],
        remediations=[_approved("OBS-004")],
        runner=runner,
        spine=spine,
    )

    finding = watch.tick().findings[0]

    entries = list(spine.iter_entries())
    assert len(entries) == 1
    assert entries[0].artifact_path == "governance-audit-remediation-OBS-004.json"
    assert entries[0].content_hash == finding.content_hash()
    assert spine.verify().ok


def test_unremediated_finding_is_not_journaled(tmp_path: Path) -> None:
    """Only an attempted remediation produces a journal entry; a report does not."""
    check = _Check("OBS-004", [False])
    spine = LineageSpine(tmp_path / ".sdd" / "lineage", run_id="govern-audit", hmac_key=_KEY)
    watch = AuditWatch(checks=[check], spine=spine)

    watch.tick()

    assert list(spine.iter_entries()) == []


def test_remediations_without_a_runner_are_rejected() -> None:
    """An approved plan with nothing able to execute it is a wiring error."""
    with pytest.raises(ValueError, match="runner"):
        AuditWatch(checks=[_Check("OBS-004", [False])], remediations=[_approved("OBS-004")])


def test_health_document_serialization_round_trips() -> None:
    doc = HealthDocument(check_id="MDL-001", passed=False, observed={"b": 1, "a": 2}, timestamp=_NOW)
    assert HealthDocument.from_dict(doc.to_dict()) == doc
    assert doc.content_hash().startswith("sha256:")
