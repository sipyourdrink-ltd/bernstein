"""Tests for finished-run classification over the work ledger (#4465).

Each outcome class gets a dedicated fixture: a :class:`LedgerState` (or a
real on-disk :class:`WorkLedger`) paired with a :class:`RunWrapUp` derived
from the run's terminal ``run.closed`` entry. ``classify_run`` is pure and
covers the five classes in isolation; ``list_finished_runs`` exercises the
same classifier against real ledger directories, including a run with no
``run.closed`` entry at all (killed mid-flight).
"""

from __future__ import annotations

import time
from pathlib import Path

from bernstein.core.persistence.runs_report import (
    FailurePatternDraft,
    FinishedRun,
    RunOutcome,
    RunWrapUp,
    classify_run,
    detect_failure_patterns,
    list_finished_runs,
)
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_CLOSED,
    KIND_RUN_OPEN,
    KIND_TASK_COMPLETED,
    KIND_TASK_SCHEDULED,
    KIND_TASK_STARTED,
    LedgerState,
    TaskState,
    WorkLedger,
    run_ledger_dir,
)


def _open_run_state(run_id: str = "run-a") -> LedgerState:
    """A minimal closed-run state with no open tasks (the common case)."""
    state = LedgerState(run_id=run_id)
    state.run_open = True
    state.run_closed = True
    return state


class TestClassifyRun:
    """One test per outcome class -- the classifier's public contract."""

    def test_pr_opened(self) -> None:
        state = _open_run_state()
        wrapup = RunWrapUp(branch="fix/runs-report", pr_number=4471)
        outcome, evidence = classify_run(state, wrapup)
        assert outcome is RunOutcome.PR_OPENED
        assert "fix/runs-report" in evidence
        assert "4471" in evidence

    def test_gate_failed(self) -> None:
        state = _open_run_state()
        wrapup = RunWrapUp(gate_name="lint", failing_check="ruff check")
        outcome, evidence = classify_run(state, wrapup)
        assert outcome is RunOutcome.GATE_FAILED
        assert "lint" in evidence
        assert "ruff check" in evidence

    def test_no_changes(self) -> None:
        state = _open_run_state()
        wrapup = RunWrapUp(commits_over_base=0)
        outcome, evidence = classify_run(state, wrapup)
        assert outcome is RunOutcome.NO_CHANGES
        assert "0" in evidence

    def test_infra_error_from_adapter_death(self) -> None:
        state = _open_run_state()
        wrapup = RunWrapUp(error_kind="adapter", error_message="claude-cli exited 137")
        outcome, evidence = classify_run(state, wrapup)
        assert outcome is RunOutcome.INFRA_ERROR
        assert "claude-cli exited 137" in evidence

    def test_infra_error_from_transport_death(self) -> None:
        state = _open_run_state()
        wrapup = RunWrapUp(error_kind="transport", error_message="connection reset")
        outcome, evidence = classify_run(state, wrapup)
        assert outcome is RunOutcome.INFRA_ERROR
        assert "connection reset" in evidence

    def test_infra_error_when_wrapup_missing(self) -> None:
        state = _open_run_state()
        outcome, evidence = classify_run(state, None)
        assert outcome is RunOutcome.INFRA_ERROR
        assert evidence

    def test_wedged_when_tasks_remain_unspawnable(self) -> None:
        state = _open_run_state()
        state.tasks["t1"] = TaskState(task_id="t1", state="scheduled")
        state.tasks["t2"] = TaskState(task_id="t2", state="started")
        wrapup = RunWrapUp()
        outcome, evidence = classify_run(state, wrapup)
        assert outcome is RunOutcome.WEDGED
        assert "t1" in evidence
        assert "t2" in evidence

    def test_priority_infra_error_beats_gate_failed(self) -> None:
        """An adapter death takes precedence even if a gate name is also set."""
        state = _open_run_state()
        wrapup = RunWrapUp(error_kind="adapter", error_message="oom", gate_name="lint")
        outcome, _ = classify_run(state, wrapup)
        assert outcome is RunOutcome.INFRA_ERROR


class TestRunWrapUpPayloadRoundTrip:
    def test_round_trip(self) -> None:
        wrapup = RunWrapUp(
            branch="feat/x",
            pr_number=42,
            gate_name="tests",
            failing_check="pytest",
            commits_over_base=3,
            error_kind="adapter",
            error_message="boom",
        )
        restored = RunWrapUp.from_payload(wrapup.to_payload())
        assert restored == wrapup

    def test_empty_payload_round_trips_to_defaults(self) -> None:
        assert RunWrapUp.from_payload({}) == RunWrapUp()


def _seed_closed_run(root: Path, run_id: str, *, wrapup: RunWrapUp | None) -> None:
    ledger = WorkLedger.open(run_ledger_dir(root / ".sdd", run_id))
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": run_id})
    ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
    ledger.append(kind=KIND_TASK_STARTED, task_id="t1")
    ledger.append(kind=KIND_TASK_COMPLETED, task_id="t1")
    payload: dict[str, object] = {"run_id": run_id}
    if wrapup is not None:
        payload.update(wrapup.to_payload())
    ledger.append(kind=KIND_RUN_CLOSED, payload=payload)
    ledger.close()


def _seed_killed_run(root: Path, run_id: str) -> None:
    """A run with ledger activity but no ``run.closed`` -- a real crash shape."""
    ledger = WorkLedger.open(run_ledger_dir(root / ".sdd", run_id))
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": run_id})
    ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
    ledger.append(kind=KIND_TASK_STARTED, task_id="t1")
    ledger.close()


class TestListFinishedRuns:
    """End-to-end over real :class:`WorkLedger` directories on disk."""

    def test_classifies_a_real_pr_opened_run(self, tmp_path: Path) -> None:
        _seed_closed_run(tmp_path, "run-pr", wrapup=RunWrapUp(branch="fix/thing", pr_number=99))
        runs = list_finished_runs(tmp_path / ".sdd")
        assert len(runs) == 1
        assert runs[0].run_id == "run-pr"
        assert runs[0].outcome is RunOutcome.PR_OPENED
        assert runs[0].branch == "fix/thing"

    def test_killed_mid_flight_run_is_infra_error_not_a_crash(self, tmp_path: Path) -> None:
        _seed_killed_run(tmp_path, "run-killed")
        runs = list_finished_runs(tmp_path / ".sdd")
        assert len(runs) == 1
        assert runs[0].outcome is RunOutcome.INFRA_ERROR

    def test_empty_ledger_root_returns_no_rows(self, tmp_path: Path) -> None:
        assert list_finished_runs(tmp_path / ".sdd") == []

    def test_mixed_batch_classifies_each_run_independently(self, tmp_path: Path) -> None:
        _seed_closed_run(tmp_path, "run-pr", wrapup=RunWrapUp(branch="fix/thing", pr_number=1))
        _seed_closed_run(tmp_path, "run-nochange", wrapup=RunWrapUp(commits_over_base=0))
        _seed_closed_run(tmp_path, "run-gate", wrapup=RunWrapUp(gate_name="lint", failing_check="ruff check ."))
        _seed_killed_run(tmp_path, "run-killed")
        runs = {run.run_id: run.outcome for run in list_finished_runs(tmp_path / ".sdd")}
        assert runs == {
            "run-pr": RunOutcome.PR_OPENED,
            "run-nochange": RunOutcome.NO_CHANGES,
            "run-gate": RunOutcome.GATE_FAILED,
            "run-killed": RunOutcome.INFRA_ERROR,
        }

    def test_since_filters_by_run_start_time(self, tmp_path: Path) -> None:
        _seed_closed_run(tmp_path, "run-old", wrapup=RunWrapUp(commits_over_base=0))
        cutoff = time.time() + 3600  # one hour in the future: nothing qualifies
        runs = list_finished_runs(tmp_path / ".sdd", since=cutoff)
        assert runs == []

        runs_all = list_finished_runs(tmp_path / ".sdd", since=0.0)
        assert len(runs_all) == 1

    def test_malformed_run_directory_is_skipped_not_raised(self, tmp_path: Path) -> None:
        _seed_closed_run(tmp_path, "run-good", wrapup=RunWrapUp(branch="ok", pr_number=1))
        ledger_root = tmp_path / ".sdd" / "runtime" / "ledger"
        ledger_root.mkdir(parents=True, exist_ok=True)
        # A stray file (not a directory) alongside real run directories must
        # not take the whole report down.
        (ledger_root / "not-a-run.txt").write_text("noise", encoding="utf-8")
        runs = list_finished_runs(tmp_path / ".sdd")
        assert [r.run_id for r in runs] == ["run-good"]


class TestDetectFailurePatterns:
    """Tests for :func:`detect_failure_patterns`."""

    def test_excludes_non_failure_outcomes(self) -> None:
        """PR_OPENED and NO_CHANGES runs are not failure patterns."""
        runs = [
            FinishedRun("run-pr", "fix/a", RunOutcome.PR_OPENED, "fix/a (PR #1)", 1000.0),
            FinishedRun("run-nochange", "main", RunOutcome.NO_CHANGES, "0 commits over base", 1010.0),
        ]
        drafts = detect_failure_patterns(runs)
        assert drafts == []

    def test_groups_identical_gate_failures(self) -> None:
        """Same gate failure evidence creates one pattern with occurrence count."""
        runs = [
            FinishedRun("run-1", "fix/a", RunOutcome.GATE_FAILED, "lint: ruff check", 1000.0),
            FinishedRun("run-2", "fix/b", RunOutcome.GATE_FAILED, "lint: ruff check", 1010.0),
        ]
        drafts = detect_failure_patterns(runs)
        assert len(drafts) == 1
        assert drafts[0].occurrence_count == 2
        assert "run-1" in drafts[0].contributing_run_ids
        assert "run-2" in drafts[0].contributing_run_ids

    def test_different_evidence_creates_separate_patterns(self) -> None:
        """Different evidence strings produce different fingerprints."""
        runs = [
            FinishedRun("run-1", "fix/a", RunOutcome.GATE_FAILED, "lint: ruff check", 1000.0),
            FinishedRun("run-2", "fix/b", RunOutcome.GATE_FAILED, "tests: pytest", 1010.0),
        ]
        drafts = detect_failure_patterns(runs)
        assert len(drafts) == 2
        assert drafts[0].fingerprint != drafts[1].fingerprint
        assert {d.occurrence_count for d in drafts} == {1}

    def test_groups_infra_errors_by_evidence(self) -> None:
        """Infrastructure errors are grouped by their error message."""
        runs = [
            FinishedRun("run-1", "fix/a", RunOutcome.INFRA_ERROR, "adapter: oom", 1000.0),
            FinishedRun("run-2", "fix/b", RunOutcome.INFRA_ERROR, "adapter: oom", 1010.0),
            FinishedRun("run-3", "fix/c", RunOutcome.INFRA_ERROR, "transport: timeout", 1020.0),
        ]
        drafts = detect_failure_patterns(runs)
        assert len(drafts) == 2
        assert sorted([d.occurrence_count for d in drafts]) == [1, 2]

    def test_groups_wedged_tasks(self) -> None:
        """Wedged tasks with same task list create one pattern."""
        runs = [
            FinishedRun("run-1", "fix/a", RunOutcome.WEDGED, "2 unspawnable task(s): t1, t2", 1000.0),
            FinishedRun("run-2", "fix/b", RunOutcome.WEDGED, "2 unspawnable task(s): t1, t2", 1010.0),
        ]
        drafts = detect_failure_patterns(runs)
        assert len(drafts) == 1
        assert drafts[0].occurrence_count == 2

    def test_most_recent_run_id_is_latest_by_timestamp(self) -> None:
        """most_recent_run_id should point to the run with highest started_at."""
        runs = [
            FinishedRun("run-1", "fix/a", RunOutcome.GATE_FAILED, "lint: ruff", 1000.0),
            FinishedRun("run-2", "fix/b", RunOutcome.GATE_FAILED, "lint: ruff", 1020.0),
            FinishedRun("run-3", "fix/c", RunOutcome.GATE_FAILED, "lint: ruff", 1010.0),
        ]
        drafts = detect_failure_patterns(runs)
        assert len(drafts) == 1
        assert drafts[0].most_recent_run_id == "run-2"

    def test_fingerprint_is_deterministic(self) -> None:
        """Same evidence always produces same fingerprint."""
        runs1 = [
            FinishedRun("run-1", "fix/a", RunOutcome.GATE_FAILED, "lint: ruff check", 1000.0),
        ]
        runs2 = [
            FinishedRun("run-x", "fix/z", RunOutcome.GATE_FAILED, "lint: ruff check", 2000.0),
        ]
        drafts1 = detect_failure_patterns(runs1)
        drafts2 = detect_failure_patterns(runs2)
        assert drafts1[0].fingerprint == drafts2[0].fingerprint

    def test_sorted_by_occurrence_count_descending(self) -> None:
        """Drafts are sorted by occurrence_count descending, then fingerprint."""
        runs = [
            FinishedRun("run-1", "fix/a", RunOutcome.GATE_FAILED, "lint: ruff", 1000.0),
            FinishedRun("run-2", "fix/b", RunOutcome.GATE_FAILED, "lint: ruff", 1010.0),
            FinishedRun("run-3", "fix/c", RunOutcome.INFRA_ERROR, "adapter: oom", 1020.0),
            FinishedRun("run-4", "fix/d", RunOutcome.INFRA_ERROR, "adapter: oom", 1030.0),
            FinishedRun("run-5", "fix/e", RunOutcome.INFRA_ERROR, "adapter: oom", 1040.0),
        ]
        drafts = detect_failure_patterns(runs)
        assert [d.occurrence_count for d in drafts] == [3, 2]

    def test_contributing_run_ids_ordered_by_started_at(self) -> None:
        """contributing_run_ids are ordered by started_at descending."""
        runs = [
            FinishedRun("run-1", "fix/a", RunOutcome.GATE_FAILED, "lint: ruff", 1000.0),
            FinishedRun("run-2", "fix/b", RunOutcome.GATE_FAILED, "lint: ruff", 1020.0),
            FinishedRun("run-3", "fix/c", RunOutcome.GATE_FAILED, "lint: ruff", 1010.0),
        ]
        drafts = detect_failure_patterns(runs)
        assert drafts[0].contributing_run_ids == ["run-2", "run-3", "run-1"]

    def test_empty_input_returns_empty(self) -> None:
        """Empty run list produces no patterns."""
        assert detect_failure_patterns([]) == []

    def test_title_and_body_include_evidence(self) -> None:
        """Pattern draft title and body are human-readable."""
        runs = [
            FinishedRun("run-1", "fix/a", RunOutcome.GATE_FAILED, "lint: ruff check", 1000.0),
        ]
        drafts = detect_failure_patterns(runs)
        draft = drafts[0]
        assert "GATE_FAILED" in draft.title or "gate-failed" in draft.title.lower()
        assert "lint: ruff check" in draft.title
        assert "lint: ruff check" in draft.body
        assert "gate-failed" in draft.body
        assert "fix/a" in draft.body

    def test_pattern_draft_has_all_required_fields(self) -> None:
        """FailurePatternDraft instances have all required fields."""
        runs = [
            FinishedRun("run-1", "fix/a", RunOutcome.GATE_FAILED, "lint: ruff", 1000.0),
        ]
        drafts = detect_failure_patterns(runs)
        draft = drafts[0]
        assert isinstance(draft, FailurePatternDraft)
        assert draft.title
        assert draft.body
        assert draft.fingerprint
        assert draft.occurrence_count == 1
        assert draft.most_recent_run_id == "run-1"
        assert draft.contributing_run_ids == ["run-1"]


def _open_run_state(run_id: str = "run-a") -> LedgerState:
    """A minimal closed-run state with no open tasks (the common case)."""
    state = LedgerState(run_id=run_id)
    state.run_open = True
    state.run_closed = True
    return state
