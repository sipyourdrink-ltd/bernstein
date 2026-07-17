"""Tests for lineage-attested on_fail recovery receipts (issue #2557).

These tests pin the issue's acceptance criteria for the failure-receipt
handoff:

* Correctness - the recovery task's prompt carries the failing status, gate
  findings, and journal tail (not merely routed control).
* Verifiability - the receipt is anchored on the run's ``LineageSpine`` and the
  returned entry hash resolves to a valid Merkle-chained, HMAC-tagged entry.
* Determinism - two runs over identical fixtures produce a byte-identical
  receipt content hash and an identical spine entry hash.
* Tamper evidence - mutating any receipt field breaks the content-address bind
  to the spine, and forging the spine row to cover it up breaks the HMAC chain.
* Isolation - the receipt's ``artifact_path`` stays repo-relative and is
  accepted by ``_reject_unsafe_artifact_path``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from bernstein.core.models import Task, TaskStatus

from bernstein.core.lineage.spine import (
    LineageSpine,
    SpineStatus,
    _reject_unsafe_artifact_path,
    content_hash_of,
)
from bernstein.core.planning.recovery_receipt import (
    RECOVERY_RECEIPT_VERSION,
    RecoveryReceipt,
    build_receipt,
    gate_report_findings,
    journal_tail_for_task,
    record_receipt_on_spine,
    recovery_step_id,
    resolve_receipt_on_spine,
    verify_receipt,
)
from bernstein.core.quality.quality_gates import QualityGateCheckResult, QualityGatesResult
from bernstein.core.replay.journal import EventJournal

_KEY = b"k" * 32


# ---------------------------------------------------------------------------
# Fixtures - deterministic failure inputs
# ---------------------------------------------------------------------------


def _failing_task(task_id: str = "run-tests-fixed") -> Task:
    return Task(
        id=task_id,
        title="run tests",
        description="run the test suite",
        role="qa",
        status=TaskStatus.FAILED,
        result_summary='{"status": "failed", "failed": 3, "suite": "unit"}',
    )


def _gate_report(task_id: str = "run-tests-fixed") -> QualityGatesResult:
    return QualityGatesResult(
        task_id=task_id,
        passed=False,
        gate_results=[
            QualityGateCheckResult(gate="lint", passed=True, blocked=False, detail="clean", status="pass"),
            QualityGateCheckResult(
                gate="tests", passed=False, blocked=True, detail="3 failing in test_math", status="fail"
            ),
        ],
    )


def _journal(tmp_path: Path, run_id: str, task_id: str = "run-tests-fixed") -> EventJournal:
    journal = EventJournal(run_id=run_id, sdd_dir=tmp_path / ".sdd")
    journal.record("task_claimed", task_id=task_id, agent_id="A-1")
    journal.record("gate_failed", task_id=task_id, gate="tests")
    journal.record("task_failed", task_id=task_id, reason="tests")
    journal.record("unrelated", task_id="other-task", note="noise")
    return journal


def _make_spine(tmp_path: Path, run_id: str = "run-1") -> LineageSpine:
    return LineageSpine(tmp_path / ".sdd" / "lineage", run_id=run_id, hmac_key=_KEY)


def _build_fixture_receipt(tmp_path: Path, run_id: str = "run-1") -> RecoveryReceipt:
    task = _failing_task()
    journal = _journal(tmp_path, run_id)
    from bernstein.core.replay.journal import load_events

    return build_receipt(
        failing_node_id="run-tests",
        recovery_node_id="fix-bugs",
        source_status=task.status.value,
        condition_context={
            "status": task.status.value,
            "result": task.result_summary or "",
            "output": {"status": "failed", "failed": 3, "suite": "unit"},
        },
        source_task_id=task.id,
        journal_events=load_events(journal.path),
        gate_report=_gate_report(),
    )


# ---------------------------------------------------------------------------
# Journal tail projection
# ---------------------------------------------------------------------------


class TestJournalTail:
    def test_filters_to_task_and_strips_envelope(self, tmp_path: Path) -> None:
        from bernstein.core.replay.journal import load_events

        journal = _journal(tmp_path, "run-jt")
        tail = journal_tail_for_task(load_events(journal.path), task_id="run-tests-fixed", limit=10)

        assert [e["event"] for e in tail] == ["task_claimed", "gate_failed", "task_failed"]
        # Envelope + derived chain fields are excluded so timing never leaks.
        for event in tail:
            assert "ts" not in event
            assert "elapsed_s" not in event
            assert "event_hash" not in event
            assert "prev_hash" not in event

    def test_bounded_tail(self, tmp_path: Path) -> None:
        from bernstein.core.replay.journal import load_events

        journal = _journal(tmp_path, "run-jt2")
        tail = journal_tail_for_task(load_events(journal.path), task_id="run-tests-fixed", limit=1)
        assert len(tail) == 1
        assert tail[0]["event"] == "task_failed"

    def test_zero_limit_yields_nothing(self, tmp_path: Path) -> None:
        from bernstein.core.replay.journal import load_events

        journal = _journal(tmp_path, "run-jt3")
        assert journal_tail_for_task(load_events(journal.path), task_id="run-tests-fixed", limit=0) == ()


class TestGateFindings:
    def test_projects_each_gate(self) -> None:
        findings = gate_report_findings(_gate_report())
        assert [f["gate"] for f in findings] == ["lint", "tests"]
        assert findings[1] == {
            "gate": "tests",
            "passed": False,
            "blocked": True,
            "status": "fail",
            "detail": "3 failing in test_math",
        }

    def test_none_report(self) -> None:
        assert gate_report_findings(None) == ()


# ---------------------------------------------------------------------------
# Content addressing + determinism (AC3)
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_receipt_content_hash_byte_identical_across_runs(self, tmp_path: Path) -> None:
        r1 = _build_fixture_receipt(tmp_path / "a", "run-1")
        r2 = _build_fixture_receipt(tmp_path / "b", "run-1")
        assert r1.canonical_bytes() == r2.canonical_bytes()
        assert r1.content_hash() == r2.content_hash()

    def test_spine_entry_hash_identical_across_runs(self, tmp_path: Path) -> None:
        r1 = _build_fixture_receipt(tmp_path / "a", "run-1")
        r2 = _build_fixture_receipt(tmp_path / "b", "run-1")
        spine1 = _make_spine(tmp_path / "s1")
        spine2 = _make_spine(tmp_path / "s2")
        h1 = record_receipt_on_spine(r1, spine=spine1, actor="dag-executor", model="claude", timestamp=0)
        h2 = record_receipt_on_spine(r2, spine=spine2, actor="dag-executor", model="claude", timestamp=0)
        assert h1 == h2

    def test_step_id_is_node_derived_not_uuid(self, tmp_path: Path) -> None:
        receipt = _build_fixture_receipt(tmp_path, "run-1")
        assert recovery_step_id(receipt) == "recovery-receipt:run-tests->fix-bugs"


# ---------------------------------------------------------------------------
# Verifiability (AC2)
# ---------------------------------------------------------------------------


class TestVerifiability:
    def test_receipt_hash_resolves_on_spine(self, tmp_path: Path) -> None:
        receipt = _build_fixture_receipt(tmp_path, "run-1")
        spine = _make_spine(tmp_path / "s")
        entry_hash = record_receipt_on_spine(receipt, spine=spine, timestamp=0)
        anchored = receipt.with_entry_hash(entry_hash)

        resolution = verify_receipt(spine, anchored)
        assert resolution.ok
        assert resolution.resolved
        assert resolution.chain_ok
        assert resolution.content_match is True

    def test_resolution_reports_missing_hash(self, tmp_path: Path) -> None:
        receipt = _build_fixture_receipt(tmp_path, "run-1")
        spine = _make_spine(tmp_path / "s")
        record_receipt_on_spine(receipt, spine=spine, timestamp=0)

        resolution = resolve_receipt_on_spine(spine, entry_hash="sha256:deadbeef")
        assert not resolution.ok
        assert not resolution.resolved
        assert any("not found" in e for e in resolution.errors)


# ---------------------------------------------------------------------------
# Tamper evidence (AC4)
# ---------------------------------------------------------------------------


class TestTamperEvidence:
    def test_mutating_a_field_breaks_content_bind(self, tmp_path: Path) -> None:
        receipt = _build_fixture_receipt(tmp_path, "run-1")
        spine = _make_spine(tmp_path / "s")
        entry_hash = record_receipt_on_spine(receipt, spine=spine, timestamp=0)

        # Mutate any field -> canonical bytes change -> content hash no longer
        # matches the anchored entry's content_hash.
        tampered = dataclasses.replace(receipt, source_status="done")
        resolution = resolve_receipt_on_spine(spine, entry_hash=entry_hash, receipt_content=tampered.canonical_bytes())
        assert resolution.resolved  # entry still exists
        assert resolution.content_match is False
        assert not resolution.ok

    def test_covering_the_spine_row_breaks_hmac_chain(self, tmp_path: Path) -> None:
        receipt = _build_fixture_receipt(tmp_path, "run-1")
        spine = _make_spine(tmp_path / "s")
        record_receipt_on_spine(receipt, spine=spine, timestamp=0)

        # An attacker rewrites the anchored content_hash to match a mutated
        # receipt. Without the HMAC key the row's tag no longer verifies.
        raw = spine.spine_path.read_text(encoding="utf-8")
        tampered = raw.replace(receipt.content_hash(), content_hash_of(b"forged"))
        assert tampered != raw
        spine.spine_path.write_text(tampered, encoding="utf-8")

        assert spine.verify().status is SpineStatus.TAMPERED

    def test_each_receipt_field_changes_the_content_hash(self, tmp_path: Path) -> None:
        receipt = _build_fixture_receipt(tmp_path, "run-1")
        base = receipt.content_hash()
        mutations = [
            {"failing_node_id": "other"},
            {"recovery_node_id": "other"},
            {"source_status": "done"},
            {"condition_context": {"status": "done"}},
            {"gate_report": ()},
            {"journal_tail": ()},
            {"v": RECOVERY_RECEIPT_VERSION + 1},
        ]
        for mutation in mutations:
            mutated = dataclasses.replace(receipt, **mutation)
            assert mutated.content_hash() != base, f"mutation did not change hash: {mutation}"

    def test_spine_entry_hash_excluded_from_content_address(self, tmp_path: Path) -> None:
        receipt = _build_fixture_receipt(tmp_path, "run-1")
        anchored = receipt.with_entry_hash("sha256:whatever")
        # The derived entry hash must not feed back into the content address.
        assert anchored.content_hash() == receipt.content_hash()
        assert anchored.canonical_bytes() == receipt.canonical_bytes()


# ---------------------------------------------------------------------------
# Isolation (AC6)
# ---------------------------------------------------------------------------


class TestIsolation:
    def test_artifact_path_is_repo_relative_and_accepted(self, tmp_path: Path) -> None:
        receipt = _build_fixture_receipt(tmp_path, "run-1")
        path = receipt.artifact_path()
        assert not path.startswith("/")
        assert ".." not in path.split("/")
        assert path.startswith(".sdd/lineage/receipts/")
        # Does not raise.
        _reject_unsafe_artifact_path(path)


# ---------------------------------------------------------------------------
# Preamble rendering (AC1 helper)
# ---------------------------------------------------------------------------


class TestPreamble:
    def test_preamble_carries_status_gates_and_journal(self, tmp_path: Path) -> None:
        receipt = _build_fixture_receipt(tmp_path, "run-1").with_entry_hash("sha256:abc123")
        preamble = receipt.render_preamble()
        assert "failed" in preamble
        assert "sha256:abc123" in preamble
        assert "tests" in preamble  # failing gate
        assert "task_failed" in preamble  # journal tail event
        assert receipt.content_hash() in preamble
