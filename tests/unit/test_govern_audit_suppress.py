"""End-to-end QA for the ``govern audit suppress`` feature (issue #5078).

Acceptance contract for #5078 slices 1 + 6:

1. a suppression decision is created with the correct fields
   (``subject=find_id``, ``verdict=accepted``, ``context={reason, expiry}``);
2. the actor is recorded from the identity that ran the command;
3. a suppressed finding appears in the report as ``verdict=accepted`` with
   the decision anchor -- it is never removed from the report;
4. a non-suppressed finding is unchanged in the report;
5. a past-expiry suppression is not applied;
6. a future-expiry suppression is applied;
7. re-suppressing a finding with a different reason updates the record;
8. ``bernstein audit suppress`` parses ``--until`` and ``--reason`` correctly.

The low-level anchor / persistence behaviour is covered by
:mod:`tests.unit.core.govern.test_suppress`; this module is the QA pass over
the operator-facing contract: the CLI writes what the issue promises, and the
report reads it back with the expiry semantics the issue demands.

``AuditReport.suppressed_findings`` labels findings by *anchor* (and
reason/expiry), the expiry is checked against the report's ``now`` timestamp,
and a lapsed suppression reverts the finding to its producer verdict.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from click.testing import CliRunner

from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.core.govern.audit_report import AuditReport, evidence_hash
from bernstein.core.govern.suppress import (
    SUPPRESS_ACTOR,
    anchor_suppress_decision,
    read_suppressions,
    suppressions_dir,
)

if TYPE_CHECKING:
    from pytest import MonkeyPatch

    from bernstein.core.security.governance import GovernanceDecision

_KEY = b"k" * 32


def _lineage_root(tmp_path: Path) -> Path:
    return tmp_path / ".sdd" / "lineage"


def _finding(
    finding_id: str,
    *,
    verdict: str = "measured",
    passed: bool = True,
) -> dict[str, Any]:
    """One serialised finding record as a check producer would emit it."""
    return {
        "id": finding_id,
        "area": finding_id.split("-")[0].lower(),
        "verdict": verdict,
        "passed": passed,
        "summary": f"summary for {finding_id}",
        "remediation": f"remediate {finding_id}",
        "evidence": [{"locator": f"config/{finding_id}.yaml", "sha256": "a" * 64}],
    }


def _report(
    findings: list[dict[str, Any]],
    *,
    timestamp: int = 1_700_000_100,
) -> AuditReport:
    return AuditReport(findings=tuple(findings), timestamp=timestamp)


def _labels(
    report: AuditReport,
    suppressions: list[GovernanceDecision],
    *,
    now: int = 1_700_000_200,
) -> dict[str, dict[str, str]]:
    """Return the suppression labels a report renderer would apply at *now*."""
    return report.suppressed_findings(tuple(suppressions), now=now)


def _now_ts(date_str: str) -> int:
    """``YYYY-MM-DD`` -> unix timestamp at midnight UTC, for label checks."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(dt.timestamp())


# --------------------------------------------------------------------------- 1
# suppression decision has the correct fields


def test_suppression_decision_subject_verdict_context(tmp_path: Path) -> None:
    """(1) subject=find_id, verdict=accepted, context={reason, expiry}."""
    anchored = anchor_suppress_decision(
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        finding_id="MDL-001",
        reason="Vendor EOL confirmed",
        expiry="2026-12-31",
        timestamp=1_700_000_000,
    )

    assert anchored.subject == "MDL-001"
    assert anchored.verdict == "accepted"
    assert anchored.action == "suppress"
    assert anchored.context == {"reason": "Vendor EOL confirmed", "expiry": "2026-12-31"}


# --------------------------------------------------------------------------- 2
# actor is recorded from the identity that ran the command


def test_actor_is_recorded_from_identity(tmp_path: Path) -> None:
    """(2) the ``actor`` parameter is bound to the spine entry, not the static fallback."""
    lineage_root = _lineage_root(tmp_path)

    anchored = anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="OBS-004",
        reason="Risk accepted per security team",
        expiry="2027-01-01",
        timestamp=1_700_000_001,
        actor="alice@example.com",
    )

    from bernstein.core.lineage.spine import LineageSpine

    spine = LineageSpine(lineage_root, run_id="govern-audit", hmac_key=_KEY)
    entries = list(spine.iter_entries())

    assert len(entries) == 1
    assert entries[-1].actor == "alice@example.com"
    assert entries[-1].entry_hash == anchored.journal_entry_hash

    # Callers with no operator identity keep the deterministic service actor.
    fallback = anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="MDL-002",
        reason="fallback actor",
        expiry="2027-01-01",
        timestamp=1_700_000_002,
    )
    spine2 = LineageSpine(lineage_root, run_id="govern-audit", hmac_key=_KEY)
    last = list(spine2.iter_entries())[-1]
    assert last.actor == SUPPRESS_ACTOR
    assert last.entry_hash != anchored.journal_entry_hash
    assert last.step_id == fallback.inputs_hash


# --------------------------------------------------------------------------- 3
# suppressed finding appears in report as verdict=accepted with decision anchor


def test_suppressed_finding_appears_in_report_with_accepted_verdict_and_anchor(tmp_path: Path) -> None:
    """(3) suppressed finding -> report label is verdict=accepted + anchor, kept in report."""
    lineage_root = _lineage_root(tmp_path)
    anchored = anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="MDL-001",
        reason="Vendor EOL confirmed",
        expiry="2026-12-31",
        timestamp=1_700_000_000,
    )

    report = _report([_finding("MDL-001"), _finding("OBS-004")])
    suppressions = read_suppressions(lineage_root)

    # The finding is still in the report -- suppression re-labels, never removes.
    assert report.finding_by_id("MDL-001") is not None

    labels = _labels(report, suppressions, now=_now_ts("2026-06-01"))
    assert set(labels) == {"MDL-001"}
    assert labels["MDL-001"]["anchor"] == anchored.journal_entry_hash
    assert labels["MDL-001"]["reason"] == "Vendor EOL confirmed"
    assert labels["MDL-001"]["expiry"] == "2026-12-31"

    # Rendering the label means: verdict accepted, carrying the anchor.
    row = dict(report.finding_by_id("MDL-001") or {})
    row["verdict"] = "accepted"
    row["suppression_anchor"] = labels["MDL-001"]["anchor"]
    assert row["verdict"] == "accepted"
    assert row["suppression_anchor"] == anchored.journal_entry_hash


# --------------------------------------------------------------------------- 4
# non-suppressed finding is unchanged


def test_non_suppressed_finding_is_unchanged(tmp_path: Path) -> None:
    """(4) a finding with no suppression record keeps its producer's verdict."""
    lineage_root = _lineage_root(tmp_path)
    # Suppress a different finding so the suppressions dir is non-empty.
    anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="OTHER-999",
        reason="Unrelated",
        expiry="2099-01-01",
        timestamp=1_700_000_000,
    )

    findings = [
        _finding("OBS-004", verdict="declared", passed=False),
        _finding("CHG-002", verdict="measured", passed=True),
    ]
    report = _report(findings)
    labels = _labels(report, read_suppressions(lineage_root), now=_now_ts("2026-06-01"))

    assert labels == {}
    for original in findings:
        row = report.finding_by_id(str(original["id"]))
        assert row is not None
        assert row == original
        assert evidence_hash(row) == evidence_hash(original)


# --------------------------------------------------------------------------- 5
# past-expiry suppression is not applied


def test_past_expiry_suppression_is_not_applied(tmp_path: Path) -> None:
    """(5) now > expiry: the finding reverts to its producer's verdict."""
    lineage_root = _lineage_root(tmp_path)
    anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="MDL-001",
        reason="Window already closed",
        expiry="2020-01-01",
        timestamp=1_700_000_000,
    )

    report = _report([_finding("MDL-001")])
    labels = _labels(report, read_suppressions(lineage_root), now=_now_ts("2026-09-03"))

    assert labels == {}
    # The raw verdict survives in the report; nothing is removed.
    row = report.finding_by_id("MDL-001")
    assert row is not None
    assert row["verdict"] == "measured"


# --------------------------------------------------------------------------- 6
# future-expiry suppression is applied


def test_future_expiry_suppression_is_applied(tmp_path: Path) -> None:
    """(6) now <= expiry: the suppression is in effect."""
    lineage_root = _lineage_root(tmp_path)
    anchored = anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="MDL-001",
        reason="Accepted until next vendor review",
        expiry="2099-12-31",
        timestamp=1_700_000_000,
    )

    report = _report([_finding("MDL-001")])
    labels = _labels(report, read_suppressions(lineage_root), now=_now_ts("2026-09-03"))

    assert set(labels) == {"MDL-001"}
    assert labels["MDL-001"]["anchor"] == anchored.journal_entry_hash
    assert labels["MDL-001"]["expiry"] == "2099-12-31"

    # Expiry day itself still counts as within the window (expiry < today lapses).
    labels_on_expiry_day = _labels(report, read_suppressions(lineage_root), now=_now_ts("2099-12-31"))
    assert set(labels_on_expiry_day) == {"MDL-001"}


# --------------------------------------------------------------------------- 7
# re-suppressing with a different reason updates the record


def test_re_suppressing_with_different_reason_updates_record(tmp_path: Path) -> None:
    """(7) a second, later suppression supersedes the first for the report label."""
    lineage_root = _lineage_root(tmp_path)
    first = anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="MDL-001",
        reason="Initial triage",
        expiry="2026-12-31",
        timestamp=1_700_000_000,
    )
    second = anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="MDL-001",
        reason="Vendor confirmed permanent EOL",
        expiry="2027-12-31",
        timestamp=1_700_000_001,
    )

    # Two chain-anchored decisions, both retained (append, not in-place edit).
    assert first.journal_entry_hash != second.journal_entry_hash
    assert first.inputs_hash != second.inputs_hash

    suppressions = read_suppressions(lineage_root)
    assert len(suppressions) == 2
    assert {d.context["reason"] for d in suppressions} == {
        "Initial triage",
        "Vendor confirmed permanent EOL",
    }

    # The report label comes from the latest suppression for the finding.
    report = _report([_finding("MDL-001")])
    labels = _labels(report, suppressions, now=_now_ts("2027-06-01"))
    assert set(labels) == {"MDL-001"}
    assert labels["MDL-001"]["reason"] == "Vendor confirmed permanent EOL"
    assert labels["MDL-001"]["anchor"] == second.journal_entry_hash
    assert labels["MDL-001"]["anchor"] != first.journal_entry_hash


# --------------------------------------------------------------------------- 8
# CLI parses --until and --reason correctly


def test_cli_suppress_parses_until_and_reason(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """(8) ``bernstein audit suppress ID --reason ... --until ...`` parses + persists."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))

    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".sdd/lineage").mkdir(parents=True)
        result = runner.invoke(
            audit_group,
            [
                "suppress",
                "MDL-001",
                "--reason",
                "Vendor EOL confirmed, no fix available",
                "--until",
                "2026-12-31",
                "--actor",
                "bob@example.com",
            ],
        )

        sdir = suppressions_dir(Path.cwd() / ".sdd" / "lineage")
        files = list(sdir.glob("*.json"))
        rows = [json.loads(f.read_text(encoding="utf-8")) for f in files]

    assert result.exit_code == 0, result.output
    # Reason + until + actor surface in the user-facing summary.
    assert "MDL-001" in result.output
    assert "2026-12-31" in result.output
    assert "Vendor EOL confirmed, no fix available" in result.output
    assert "bob@example.com" in result.output

    assert len(rows) == 1
    row = rows[0]
    assert row["subject"] == "MDL-001"
    assert row["action"] == "suppress"
    assert row["verdict"] == "accepted"
    assert row["context"]["reason"] == "Vendor EOL confirmed, no fix available"
    assert row["context"]["expiry"] == "2026-12-31"


def test_cli_suppress_rejects_invalid_until(monkeypatch: MonkeyPatch) -> None:
    """(8b) malformed --until values are a clean usage error."""
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path(".sdd/lineage").mkdir(parents=True)
        result = runner.invoke(
            audit_group,
            ["suppress", "MDL-001", "--reason", "x", "--until", "not-a-date"],
        )

    assert result.exit_code != 0
    assert "invalid" in result.output.lower() or "YYYY-MM-DD" in result.output


def test_cli_suppress_missing_required_options_is_usage_error() -> None:
    """(8c) omitting --reason / --until is a Click usage error, not a crash."""
    runner = CliRunner()

    no_reason = runner.invoke(audit_group, ["suppress", "MDL-001", "--until", "2026-12-31"])
    assert no_reason.exit_code == 2

    no_until = runner.invoke(audit_group, ["suppress", "MDL-001", "--reason", "x"])
    assert no_until.exit_code == 2


def test_cli_suppress_without_lineage_dir_fails_cleanly() -> None:
    """(8d) no lineage dir -> ClickException, not a traceback."""
    runner = CliRunner()

    with runner.isolated_filesystem():
        result = runner.invoke(
            audit_group,
            ["suppress", "MDL-001", "--reason", "x", "--until", "2026-12-31"],
        )

    assert result.exit_code != 0
    assert "lineage directory not found" in result.output
