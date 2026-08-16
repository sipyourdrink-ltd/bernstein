"""CLI tests for ``bernstein replay --verify`` and ``--from-step`` (issue #2293)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.cli.advanced_cmd import replay_cmd
from click.testing import CliRunner

from bernstein.core.replay.journal import JOURNAL_FILENAME, EventJournal


def _make_journal(sdd_dir: Path, run_id: str) -> EventJournal:
    journal = EventJournal(run_id=run_id, sdd_dir=sdd_dir)
    journal.record("run_started", run_id=run_id)
    journal.record("task_claimed", task_id="T-1")
    journal.record("task_completed", task_id="T-1")
    journal.record("run_completed", run_id=run_id)
    return journal


def test_verify_reports_chain_consistency_without_overclaiming_identity(tmp_path: Path) -> None:
    """An unsealed journal reports an intact chain and unverifiable identity."""
    sdd_dir = tmp_path / ".sdd"
    _make_journal(sdd_dir, "run-1")

    result = CliRunner().invoke(replay_cmd, ["run-1", "--sdd-dir", str(sdd_dir), "--verify"])

    assert result.exit_code == 0
    assert "chain intact" in result.output.lower()
    assert "identity=unverifiable" in result.output.lower()


def test_verify_reports_first_divergent_step(tmp_path: Path) -> None:
    """--verify flags the exact first divergent step and writes a report (AC2)."""
    sdd_dir = tmp_path / ".sdd"
    journal = _make_journal(sdd_dir, "run-2")

    rows = journal.path.read_text().splitlines()
    tampered = json.loads(rows[2])
    tampered["task_id"] = "T-INJECTED"  # non-deterministic tool result at step 2
    rows[2] = json.dumps(tampered)
    journal.path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    result = CliRunner().invoke(replay_cmd, ["run-2", "--sdd-dir", str(sdd_dir), "--verify"])

    assert result.exit_code == 1
    assert "2" in result.output
    report = sdd_dir / "runs" / "run-2" / "divergence_report.json"
    assert report.exists()
    payload = json.loads(report.read_text())
    assert payload["step_index"] == 2
    assert payload["expected_hash"]
    assert payload["actual_hash"]


def test_from_step_reconstructs_identical_state(tmp_path: Path) -> None:
    """--from-step N reconstructs identical state for two invocations (AC4)."""
    sdd_dir = tmp_path / ".sdd"
    _make_journal(sdd_dir, "run-3")

    runner = CliRunner()
    first = runner.invoke(replay_cmd, ["run-3", "--sdd-dir", str(sdd_dir), "--from-step", "3", "--as-json"])
    second = runner.invoke(replay_cmd, ["run-3", "--sdd-dir", str(sdd_dir), "--from-step", "3", "--as-json"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first.output == second.output
    assert JOURNAL_FILENAME  # imported to pin the canonical filename
    payload = json.loads(first.output)
    assert payload["step_count"] == 3
    assert payload["head_hash"]


def _torn_journal(sdd_dir: Path, run_id: str) -> None:
    """Append a crash-torn fragment to an existing journal's final line."""
    journal = EventJournal(run_id=run_id, sdd_dir=sdd_dir)
    with journal.path.open("a", encoding="utf-8") as f:
        f.write('{"event": "run_completed", "run_id": "run-4", "prev_ha')


def test_repair_truncates_the_torn_tail_and_makes_resume_possible(tmp_path: Path) -> None:
    """``bernstein replay repair <RUN_ID>`` fixes the unresumable case (#3910)."""
    sdd_dir = tmp_path / ".sdd"
    journal = _make_journal(sdd_dir, "run-4")
    _torn_journal(sdd_dir, "run-4")
    with pytest.raises(ValueError, match=r"torn write"):
        EventJournal.resume("run-4", sdd_dir)

    result = CliRunner().invoke(replay_cmd, ["repair", "run-4", "--sdd-dir", str(sdd_dir)])

    assert result.exit_code == 0
    assert "Repaired" in result.output
    resumed = EventJournal.resume("run-4", sdd_dir)
    assert resumed.head() == journal.head()


def test_repair_noop_reports_clean_journal(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    _make_journal(sdd_dir, "run-5")

    result = CliRunner().invoke(replay_cmd, ["repair", "run-5", "--sdd-dir", str(sdd_dir)])

    assert result.exit_code == 0
    assert "Nothing to repair" in result.output


def test_repair_refuses_a_middle_discard_without_touching_the_file(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    _make_journal(sdd_dir, "run-6")
    journal = EventJournal("run-6", sdd_dir)
    lines = journal.path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines.insert(2, "not json\n")
    journal.path.write_text("".join(lines), encoding="utf-8")
    poisoned = journal.path.read_bytes()

    result = CliRunner().invoke(replay_cmd, ["repair", "run-6", "--sdd-dir", str(sdd_dir)])

    assert result.exit_code == 2
    assert "Repair refused" in result.output
    assert "corruption" in result.output
    assert journal.path.read_bytes() == poisoned
