"""CLI tests for ``bernstein runs report`` (#4465).

The command is a thin renderer over
:func:`bernstein.core.persistence.runs_report.list_finished_runs`: these
tests drive it through :class:`click.testing.CliRunner` the way
``test_ledger_cmd.py`` drives ``ledger_group``, so a mis-wired decorator or
a broken ``--json`` contract is caught at the layer an operator hits it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.runs_cmd import runs_group
from bernstein.core.persistence.runs_report import RunWrapUp
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_CLOSED,
    KIND_RUN_OPEN,
    KIND_TASK_COMPLETED,
    KIND_TASK_SCHEDULED,
    KIND_TASK_STARTED,
    WorkLedger,
    run_ledger_dir,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return tmp_path


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
    ledger = WorkLedger.open(run_ledger_dir(root / ".sdd", run_id))
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": run_id})
    ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
    ledger.close()


class TestRunsReportJson:
    def test_json_rows_carry_the_stable_field_names(self, project: Path) -> None:
        _seed_closed_run(project, "run-pr", wrapup=RunWrapUp(branch="fix/thing", pr_number=99))
        result = CliRunner().invoke(runs_group, ["report", "--workdir", str(project), "--json"])
        assert result.exit_code == 0, result.output

        payload = json.loads(result.output)
        assert list(payload.keys()) == ["runs"]
        row = payload["runs"][0]
        assert set(row.keys()) == {"run_id", "branch", "outcome", "evidence", "started_at"}
        assert row["run_id"] == "run-pr"
        assert row["branch"] == "fix/thing"
        assert row["outcome"] == "pr-opened"
        assert "99" in row["evidence"]

    def test_empty_ledger_emits_empty_rows_not_an_error(self, project: Path) -> None:
        result = CliRunner().invoke(runs_group, ["report", "--workdir", str(project), "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"runs": []}

    def test_missing_wrapup_never_crashes_the_report(self, project: Path) -> None:
        _seed_killed_run(project, "run-killed")
        result = CliRunner().invoke(runs_group, ["report", "--workdir", str(project), "--json"])
        assert result.exit_code == 0, result.output
        row = json.loads(result.output)["runs"][0]
        assert row["outcome"] == "infra-error"


class TestRunsReportTable:
    def test_table_lists_run_branch_outcome_and_evidence(self, project: Path) -> None:
        _seed_closed_run(project, "run-gate", wrapup=RunWrapUp(gate_name="lint", failing_check="ruff check ."))
        result = CliRunner().invoke(runs_group, ["report", "--workdir", str(project)])
        assert result.exit_code == 0, result.output
        assert "run-gate" in result.output
        assert "gate-failed" in result.output
        assert "ruff check ." in result.output

    def test_no_runs_prints_a_clear_message(self, project: Path) -> None:
        result = CliRunner().invoke(runs_group, ["report", "--workdir", str(project)])
        assert result.exit_code == 0, result.output
        assert "no finished runs" in result.output.lower()


class TestRunsReportSince:
    def test_since_excludes_runs_started_before_the_window(self, project: Path) -> None:
        _seed_closed_run(project, "run-old", wrapup=RunWrapUp(commits_over_base=0))

        # Rewrite the seeded run's first entry to look like it started two
        # days ago, so a 1-hour `--since` window excludes it deterministically.
        bucket = run_ledger_dir(project / ".sdd", "run-old") / "000000.jsonl"
        lines = bucket.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["ts"] = time.time() - (2 * 86400)
        lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
        bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = CliRunner().invoke(runs_group, ["report", "--workdir", str(project), "--since", "1h", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"runs": []}

        result_all = CliRunner().invoke(runs_group, ["report", "--workdir", str(project), "--json"])
        assert len(json.loads(result_all.output)["runs"]) == 1
