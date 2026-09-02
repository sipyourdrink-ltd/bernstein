"""CLI tests for ``bernstein replay <run> --re-derive`` (issue #4213)."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.cli.advanced_cmd import replay_cmd
from click.testing import CliRunner

from bernstein.core.replay.journal import EventJournal


def _recorded_run(sdd_dir: Path, run_id: str = "run-rd") -> EventJournal:
    """Record a two-task run whose coordination obeys the dependency edge."""
    journal = EventJournal(run_id=run_id, sdd_dir=sdd_dir)
    journal.record("run_started", run_id=run_id, max_agents=2)
    journal.record(
        "plan.graph.full",
        goal="ship it",
        nodes=[
            {"id": "T-1", "role": "backend", "title": "first", "depends_on": []},
            {"id": "T-2", "role": "backend", "title": "second", "depends_on": ["T-1"]},
        ],
        task_count=2,
    )
    journal.record("task_claimed", task_id="T-1", agent_id="a-1")
    journal.record("task_completed", task_id="T-1", agent_id="a-1", cost_usd=0.1)
    journal.record("task_claimed", task_id="T-2", agent_id="a-2")
    journal.record("task_completed", task_id="T-2", agent_id="a-2", cost_usd=0.2)
    journal.record("run_completed", run_id=run_id, ticks=2, outcome="completed")
    return journal


def _demote_first_completion(journal_path: Path) -> None:
    """Turn the recorded ``task_completed`` for ``T-1`` into a verification failure."""
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[3])
    row["event"] = "task_verification_failed"
    lines[3] = json.dumps(row)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# 10 ----------------------------------------------------------------------


def test_rederive_exits_zero_when_the_recorded_run_is_derivable(tmp_path: Path) -> None:
    """An untouched recorded run re-derives to its own head and exits 0."""
    sdd_dir = tmp_path / ".sdd"
    _recorded_run(sdd_dir)

    result = CliRunner().invoke(replay_cmd, ["run-rd", "--sdd-dir", str(sdd_dir), "--re-derive"])

    assert result.exit_code == 0
    assert "re-derived" in result.output.lower()


# 11 ----------------------------------------------------------------------


def test_rederive_names_the_first_divergent_step_as_json_and_exits_nonzero(tmp_path: Path) -> None:
    """A modified recorded outcome is reported as a named step, not a bare hash diff."""
    sdd_dir = tmp_path / ".sdd"
    journal = _recorded_run(sdd_dir)
    _demote_first_completion(journal.path)

    result = CliRunner().invoke(
        replay_cmd,
        ["run-rd", "--sdd-dir", str(sdd_dir), "--re-derive", "--as-json"],
    )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["reason_code"] == "underivable_step"
    assert payload["rule"] == "dependency_not_completed"
    assert payload["step_index"] == 4
    assert "T-2" in payload["reason"]


# 12 ----------------------------------------------------------------------


def test_rederive_leaves_no_sandbox_behind_and_does_not_touch_the_run_dir(tmp_path: Path) -> None:
    """The fresh journal lives in a temporary sandbox that is removed afterwards."""
    sdd_dir = tmp_path / ".sdd"
    journal = _recorded_run(sdd_dir)
    before = sorted(p.name for p in journal.path.parent.iterdir())

    result = CliRunner().invoke(replay_cmd, ["run-rd", "--sdd-dir", str(sdd_dir), "--re-derive"])

    assert result.exit_code == 0
    assert sorted(p.name for p in journal.path.parent.iterdir()) == before


# 13 ----------------------------------------------------------------------


def test_rederive_reports_a_missing_journal_rather_than_a_traceback(tmp_path: Path) -> None:
    """An unknown run id refuses by name on the channel the caller selected."""
    sdd_dir = tmp_path / ".sdd"
    (sdd_dir / "runs").mkdir(parents=True)

    result = CliRunner().invoke(
        replay_cmd,
        ["run-missing", "--sdd-dir", str(sdd_dir), "--re-derive", "--as-json"],
    )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["reason_code"] == "journal_not_found"
