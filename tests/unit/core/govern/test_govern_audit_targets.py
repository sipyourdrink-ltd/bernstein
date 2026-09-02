"""Aggregation contract for a govern audit sweep over a selected target set (#5090).

The sweep runs the identical check set on every target, collects every result
without aborting on the first failure, and records one journal entry for the
whole sweep. These tests pin the properties that make a sweep's output usable:
a fleet run of one target is byte-identical to the local run of that target, an
unreachable target hides nothing on the others, the journal is one entry per
sweep rather than a per-target log flood, and the same check failing on
consecutive sweeps escalates instead of re-raising the same finding.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.govern.audit_sweep import (
    NOT_REPORTED,
    UNREACHABLE,
    CheckOutcome,
    CheckVerdict,
    FailureLedger,
    TargetProbe,
    audit_target,
    escalate_repeat_failures,
    record_sweep,
    run_audit_sweep,
)
from bernstein.core.lineage.spine import LineageSpine

CHECK_IDS = ("MDL-001", "OBS-004", "SEC-002")
HMAC_KEY = b"\x11" * 32


def _passing(check_id: str, area: str = "models") -> CheckOutcome:
    return CheckOutcome(
        check_id=check_id,
        area=area,
        verdict=CheckVerdict.MEASURED,
        passed=True,
        summary=f"{check_id} holds",
        evidence=(("bernstein.yaml", "sha256:" + "0" * 64),),
    )


def _failing(check_id: str, area: str = "models") -> CheckOutcome:
    return CheckOutcome(
        check_id=check_id,
        area=area,
        verdict=CheckVerdict.MEASURED,
        passed=False,
        summary=f"{check_id} does not hold",
        remediation="pin the model id",
        evidence=(("bernstein.yaml", "sha256:" + "1" * 64),),
    )


def _healthy_probe(target: str) -> TargetProbe:
    return TargetProbe(
        outcomes=tuple(_passing(cid) for cid in CHECK_IDS),
        components={"skill.review": "2.1.0", "plugin.lint": "0.4.0"},
    )


def test_local_run_and_single_target_fleet_run_are_byte_identical() -> None:
    """The fleet path is the local path with a target loop around it."""
    local = audit_target(target="host-a", check_ids=CHECK_IDS, executor=_healthy_probe)

    sweep = run_audit_sweep(
        targets=("host-a",),
        check_ids=CHECK_IDS,
        executor=_healthy_probe,
        selector="name host-a",
        check_set_version="checks-v1",
        timestamp=1_700_000_000,
    )

    fleet_of_one = sweep.target("host-a")
    assert fleet_of_one is not None
    assert fleet_of_one.to_canonical_bytes() == local.to_canonical_bytes()
    assert fleet_of_one.content_hash() == local.content_hash()


def test_one_unreachable_target_does_not_abort_the_sweep() -> None:
    """N-1 targets still report when one target cannot be reached."""

    def executor(target: str) -> TargetProbe:
        if target == "host-b":
            raise ConnectionError("no route to host")
        return _healthy_probe(target)

    sweep = run_audit_sweep(
        targets=("host-a", "host-b", "host-c"),
        check_ids=CHECK_IDS,
        executor=executor,
        selector="group edge",
        check_set_version="checks-v1",
        timestamp=1_700_000_000,
    )

    assert [t.target for t in sweep.targets] == ["host-a", "host-b", "host-c"]

    unreachable = sweep.target("host-b")
    assert unreachable is not None
    assert unreachable.reachable is False
    # Never pass, never absent: every requested check is recorded as
    # not_measurable for the target that could not be reached.
    assert [o.check_id for o in unreachable.outcomes] == sorted(CHECK_IDS)
    for outcome in unreachable.outcomes:
        assert outcome.verdict is CheckVerdict.NOT_MEASURABLE
        assert outcome.summary == UNREACHABLE
        assert outcome.passed is None
        assert outcome.what_would_make_it_measurable

    for name in ("host-a", "host-c"):
        reached = sweep.target(name)
        assert reached is not None
        assert reached.reachable is True
        assert all(o.verdict is CheckVerdict.MEASURED for o in reached.outcomes)

    assert sweep.journal.unreachable_targets == 1


def test_a_check_the_executor_did_not_report_is_not_measurable_not_absent() -> None:
    """A silent check is reported as unmeasured, never dropped from the report."""

    def partial(target: str) -> TargetProbe:
        return TargetProbe(outcomes=(_passing("MDL-001"),))

    audit = audit_target(target="host-a", check_ids=CHECK_IDS, executor=partial)

    assert [o.check_id for o in audit.outcomes] == sorted(CHECK_IDS)
    missing = {o.check_id: o for o in audit.outcomes if o.check_id != "MDL-001"}
    assert set(missing) == {"OBS-004", "SEC-002"}
    for outcome in missing.values():
        assert outcome.verdict is CheckVerdict.NOT_MEASURABLE
        assert outcome.summary == NOT_REPORTED
        assert outcome.passed is None
        assert outcome.what_would_make_it_measurable


def test_version_skew_reports_a_count_and_names_the_outliers() -> None:
    """Distinct versions per component across the selected set, outliers named."""
    versions = {
        "host-a": "2.1.0",
        "host-b": "2.1.0",
        "host-c": "1.9.0",
    }

    def executor(target: str) -> TargetProbe:
        return TargetProbe(
            outcomes=tuple(_passing(cid) for cid in CHECK_IDS),
            components={"skill.review": versions[target], "plugin.lint": "0.4.0"},
        )

    sweep = run_audit_sweep(
        targets=("host-a", "host-b", "host-c"),
        check_ids=CHECK_IDS,
        executor=executor,
        selector="group edge",
        check_set_version="checks-v1",
        timestamp=1_700_000_000,
    )

    by_component = {s.component: s for s in sweep.version_skew}
    assert by_component["skill.review"].distinct_versions == 2
    assert by_component["skill.review"].outliers == ("host-c",)
    assert by_component["plugin.lint"].distinct_versions == 1
    assert by_component["plugin.lint"].outliers == ()


def test_journal_entry_per_sweep_not_per_target(tmp_path: Path) -> None:
    """Five targets produce one journal entry, not five."""
    spine = LineageSpine(tmp_path / "lineage", run_id="govern-audit", hmac_key=HMAC_KEY)

    sweep = run_audit_sweep(
        targets=("h1", "h2", "h3", "h4", "h5"),
        check_ids=CHECK_IDS,
        executor=_healthy_probe,
        selector="group edge",
        check_set_version="checks-v1",
        timestamp=1_700_000_000,
    )
    record_sweep(sweep, spine=spine, timestamp=1_700_000_000)

    entries = list(spine.iter_entries())
    assert len(entries) == 1

    assert sweep.journal.check_set_version == "checks-v1"
    assert sweep.journal.selector == "group edge"
    assert sweep.journal.target_count == 5
    assert dict(sweep.journal.counts)["measured_pass"] == 15


def test_consecutive_failure_opens_decision_not_duplicate_finding(tmp_path: Path) -> None:
    """The same check failing twice on one target escalates instead of repeating."""
    ledger_path = tmp_path / "sweep-failures.json"

    def executor(target: str) -> TargetProbe:
        outcomes = [_failing("MDL-001"), _passing("OBS-004"), _passing("SEC-002")]
        return TargetProbe(outcomes=tuple(outcomes), components={})

    def one_sweep(timestamp: int) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
        report = run_audit_sweep(
            targets=("host-a",),
            check_ids=CHECK_IDS,
            executor=executor,
            selector="name host-a",
            check_set_version="checks-v1",
            timestamp=timestamp,
        )
        result = escalate_repeat_failures(
            report=report,
            ledger=FailureLedger.load(ledger_path),
            timestamp=timestamp,
        )
        result.ledger.save(ledger_path)
        return result.raised_findings, tuple(d.check_id for d in result.decisions)

    first_findings, first_decisions = one_sweep(1_700_000_000)
    assert first_findings == (("host-a", "MDL-001"),)
    assert first_decisions == ()

    second_findings, second_decisions = one_sweep(1_700_003_600)
    assert second_findings == ()
    assert second_decisions == ("MDL-001",)

    third_findings, third_decisions = one_sweep(1_700_007_200)
    assert third_findings == ()
    assert third_decisions == ()


def test_unreachable_target_does_not_reset_a_failure_streak(tmp_path: Path) -> None:
    """An unreachable sweep is not a pass, so it cannot clear a pending streak."""
    ledger_path = tmp_path / "sweep-failures.json"
    state = {"reachable": True}

    def executor(target: str) -> TargetProbe:
        if not state["reachable"]:
            raise TimeoutError("probe timed out")
        return TargetProbe(outcomes=(_failing("MDL-001"),), components={})

    def one_sweep(timestamp: int) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
        report = run_audit_sweep(
            targets=("host-a",),
            check_ids=("MDL-001",),
            executor=executor,
            selector="name host-a",
            check_set_version="checks-v1",
            timestamp=timestamp,
        )
        result = escalate_repeat_failures(
            report=report,
            ledger=FailureLedger.load(ledger_path),
            timestamp=timestamp,
        )
        result.ledger.save(ledger_path)
        return result.raised_findings, tuple(d.check_id for d in result.decisions)

    one_sweep(1_700_000_000)

    state["reachable"] = False
    unreachable_findings, unreachable_decisions = one_sweep(1_700_003_600)
    assert unreachable_findings == ()
    assert unreachable_decisions == ()

    state["reachable"] = True
    _, decisions = one_sweep(1_700_007_200)
    assert decisions == ("MDL-001",)
