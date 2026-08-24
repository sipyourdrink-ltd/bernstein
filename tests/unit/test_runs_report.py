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
    RunOutcome,
    RunWrapUp,
    classify_run,
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
