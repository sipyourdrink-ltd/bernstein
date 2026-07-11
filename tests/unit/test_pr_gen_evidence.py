"""PR-body evidence-summary block tests (issue #2362, AC3).

A PR opened for a task with a sealed evidence bundle carries an Evidence
section linking the bundle (task id, anchor, pass/fail counts, and the offline
``bernstein evidence show`` command) so review happens against sealed proof.
"""

from __future__ import annotations

from bernstein.core.integrations.pr_gen import (
    EvidenceSummary,
    GateResult,
    SessionSummary,
    build_pr_body,
)


def _summary(evidence: EvidenceSummary | None) -> SessionSummary:
    return SessionSummary(
        session_id="abcdef123456",
        goal="add a feature",
        branch="feat/x",
        gates=(GateResult(name="tests", passed=True),),
        evidence=evidence,
    )


def test_pr_body_includes_evidence_block_linking_bundle() -> None:
    ev = EvidenceSummary(
        task_id="task-42",
        anchor="sha256:deadbeefcafebabe0000",
        passed=3,
        failed=0,
        gate_passed=True,
    )
    body = build_pr_body(_summary(ev))
    assert "## Evidence" in body
    assert "task-42" in body
    assert "deadbeefcafe" in body  # anchor prefix is surfaced
    assert "bernstein evidence show task-42" in body


def test_pr_body_evidence_reports_gate_failure() -> None:
    ev = EvidenceSummary(
        task_id="task-7",
        anchor="sha256:00112233",
        passed=1,
        failed=2,
        gate_passed=False,
    )
    body = build_pr_body(_summary(ev))
    assert "## Evidence" in body
    assert "task-7" in body
    # A failing gate is surfaced, not hidden.
    assert "fail" in body.lower()


def test_pr_body_without_evidence_omits_block() -> None:
    body = build_pr_body(_summary(None))
    assert "## Evidence" not in body
    # The other sections are still present.
    assert "## Summary" in body
    assert "## Verification" in body
