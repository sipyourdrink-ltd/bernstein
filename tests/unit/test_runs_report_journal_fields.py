"""Run-journal fields on the durable run record (#5127).

``bernstein runs report`` reads the work ledger, so the work ledger is the
durable run journal. These tests pin the three properties the journal was
missing:

* the classified record carries what an operator needs to triage a run --
  attempt count, end instant, elapsed, executing host, parent run id and
  per-step timings -- not just ``(run_id, branch, outcome, evidence,
  started_at)``;
* the enumerated journal fields (``state``, ``error_kind``) and the
  reserved ``run.``/``task.`` kind families are validated where they are
  *written*, so a typo cannot silently reclassify a run at read time;
* an in-progress run is answerable as such, instead of being reported as
  an infrastructure death because it has no wrap-up yet.

Fixtures build real :class:`WorkLedger` directories on disk, the idiom of
``tests/unit/test_runs_report.py``'s ``TestListFinishedRuns``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from bernstein.core.persistence.runs_report import (
    RunOutcome,
    RunWrapUp,
    list_finished_runs,
    list_non_terminal_runs,
)
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_CLOSED,
    KIND_RUN_OPEN,
    KIND_TASK_COMPLETED,
    KIND_TASK_FAILED,
    KIND_TASK_SCHEDULED,
    KIND_TASK_STARTED,
    LedgerError,
    RunErrorKind,
    RunJournalState,
    WorkLedger,
    run_ledger_dir,
)


def _seed_retried_run(root: Path, run_id: str = "run-retried") -> None:
    """Two failed attempts then a success for one task -- three attempts."""
    ledger = WorkLedger.open(run_ledger_dir(root / ".sdd", run_id))
    ledger.append(
        kind=KIND_RUN_OPEN,
        payload={
            "run_id": run_id,
            "state": RunJournalState.OPEN.value,
            "host": "builder-7",
            "parent_run_id": "run-parent",
        },
    )
    ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
    for _ in range(2):
        ledger.append(kind=KIND_TASK_STARTED, task_id="t1")
        ledger.append(kind=KIND_TASK_FAILED, task_id="t1")
    ledger.append(kind=KIND_TASK_STARTED, task_id="t1")
    ledger.append(kind=KIND_TASK_COMPLETED, task_id="t1")
    ledger.append(
        kind=KIND_RUN_CLOSED,
        payload={
            "run_id": run_id,
            "state": RunJournalState.CLOSED.value,
            **RunWrapUp(branch="fix/thing", pr_number=7).to_payload(),
        },
    )
    ledger.close()


def _seed_in_progress_run(root: Path, run_id: str = "run-live") -> None:
    """A run with entries and no ``run.closed`` -- still executing."""
    ledger = WorkLedger.open(run_ledger_dir(root / ".sdd", run_id))
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": run_id, "state": RunJournalState.OPEN.value})
    ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
    ledger.append(kind=KIND_TASK_STARTED, task_id="t1")
    ledger.close()


class TestJournalFields:
    """Slice 1: the durable record carries the triage facts."""

    def test_finished_run_carries_attempt_count_and_per_step_timings(self, tmp_path: Path) -> None:
        _seed_retried_run(tmp_path)
        runs = list_finished_runs(tmp_path / ".sdd")
        assert len(runs) == 1
        run = runs[0]

        assert run.outcome is RunOutcome.PR_OPENED
        # Three ``task.started`` transitions for t1: two failed replays plus
        # the successful one.
        assert run.attempt_count == 3

        assert [step.task_id for step in run.steps] == ["t1"]
        step = run.steps[0]
        assert step.started_at > 0.0
        assert step.ended_at >= step.started_at
        assert step.elapsed_seconds == pytest.approx(step.ended_at - step.started_at)
        # scheduled + 3 started + 2 failed + 1 completed
        assert step.entries == 7

    def test_finished_run_carries_end_instant_elapsed_host_and_parent_run_id(self, tmp_path: Path) -> None:
        _seed_retried_run(tmp_path)
        run = list_finished_runs(tmp_path / ".sdd")[0]

        assert run.state is RunJournalState.CLOSED
        assert run.ended_at >= run.started_at
        assert run.elapsed_seconds == pytest.approx(run.ended_at - run.started_at)
        assert run.host == "builder-7"
        assert run.parent_run_id == "run-parent"

    def test_json_row_keeps_the_existing_names_and_adds_the_journal_fields(self, tmp_path: Path) -> None:
        _seed_retried_run(tmp_path)
        row = list_finished_runs(tmp_path / ".sdd")[0].to_dict()
        assert set(row) == {
            "run_id",
            "branch",
            "outcome",
            "evidence",
            "started_at",
            "state",
            "attempt_count",
            "ended_at",
            "elapsed_seconds",
            "host",
            "parent_run_id",
            "steps",
        }
        assert row["state"] == "closed"
        assert row["steps"][0]["task_id"] == "t1"


class TestWriteTimeValidation:
    """Slice 2: the enumerated fields are closed at the write boundary."""

    def test_malformed_state_value_is_rejected_at_write_not_read(self, tmp_path: Path) -> None:
        ledger = WorkLedger.open(run_ledger_dir(tmp_path / ".sdd", "run-bad-state"))
        with pytest.raises(LedgerError, match="state"):
            ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": "run-bad-state", "state": "opened"})
        # Nothing was persisted: the chain is still empty.
        assert ledger.next_seq == 0
        ledger.close()

    def test_malformed_error_kind_cannot_silently_reclassify_the_run(self, tmp_path: Path) -> None:
        ledger = WorkLedger.open(run_ledger_dir(tmp_path / ".sdd", "run-bad-error"))
        ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": "run-bad-error"})
        with pytest.raises(LedgerError, match="error_kind"):
            # "adaptor" reads as a plain wrap-up to the classifier, which
            # would report the run as no-changes instead of infra-error.
            ledger.append(
                kind=KIND_RUN_CLOSED,
                payload={"run_id": "run-bad-error", "error_kind": "adaptor", "error_message": "oom"},
            )
        ledger.close()

    def test_known_enum_values_still_append(self, tmp_path: Path) -> None:
        ledger = WorkLedger.open(run_ledger_dir(tmp_path / ".sdd", "run-ok"))
        ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": "run-ok", "state": RunJournalState.OPEN.value})
        ledger.append(
            kind=KIND_RUN_CLOSED,
            payload={
                "run_id": "run-ok",
                "state": RunJournalState.CLOSED.value,
                "error_kind": RunErrorKind.TRANSPORT.value,
            },
        )
        ledger.close()
        assert list_finished_runs(tmp_path / ".sdd")[0].outcome is RunOutcome.INFRA_ERROR

    def test_typo_of_a_reserved_kind_is_rejected_at_write(self, tmp_path: Path) -> None:
        ledger = WorkLedger.open(run_ledger_dir(tmp_path / ".sdd", "run-typo"))
        with pytest.raises(LedgerError, match="reserved"):
            ledger.append(kind="task.competed", task_id="t1")
        ledger.close()

    def test_other_kind_families_keep_the_regex_contract(self, tmp_path: Path) -> None:
        """Admission and mission ledgers own their own vocabularies."""
        ledger = WorkLedger.open(run_ledger_dir(tmp_path / ".sdd", "run-other"))
        entry = ledger.append(kind="admission.grant", task_id="t1", payload={"state": "whatever"})
        ledger.close()
        assert entry.kind == "admission.grant"


class TestNonTerminalRuns:
    """Slice 4 (query half): in-progress is answerable as in-progress."""

    def test_non_terminal_run_query_excludes_finished_runs(self, tmp_path: Path) -> None:
        _seed_retried_run(tmp_path, "run-done")
        _seed_in_progress_run(tmp_path, "run-live")

        # The finished-run report still classifies both (its scope is
        # unchanged); the new query answers only the second.
        assert {run.run_id for run in list_finished_runs(tmp_path / ".sdd")} == {"run-done", "run-live"}
        assert [run.run_id for run in list_non_terminal_runs(tmp_path / ".sdd")] == ["run-live"]

    def test_non_terminal_run_carries_its_age(self, tmp_path: Path) -> None:
        _seed_in_progress_run(tmp_path, "run-live")
        now = time.time() + 600.0
        run = list_non_terminal_runs(tmp_path / ".sdd", now=now)[0]
        assert run.age_seconds == pytest.approx(now - run.started_at)
        assert run.age_seconds >= 600.0
        assert run.last_entry_at >= run.started_at

    def test_oldest_non_terminal_run_sorts_first(self, tmp_path: Path) -> None:
        _seed_in_progress_run(tmp_path, "run-old")
        time.sleep(0.01)
        _seed_in_progress_run(tmp_path, "run-new")
        assert [run.run_id for run in list_non_terminal_runs(tmp_path / ".sdd")] == ["run-old", "run-new"]

    def test_empty_ledger_root_has_no_non_terminal_runs(self, tmp_path: Path) -> None:
        assert list_non_terminal_runs(tmp_path / ".sdd") == []
