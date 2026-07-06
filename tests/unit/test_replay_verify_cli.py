"""CLI tests for ``bernstein replay --verify`` and ``--from-step`` (issue #2293)."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_verify_reports_byte_identity(tmp_path: Path) -> None:
    """--verify on an unmodified journal reports byte-identity (AC2)."""
    sdd_dir = tmp_path / ".sdd"
    _make_journal(sdd_dir, "run-1")

    result = CliRunner().invoke(replay_cmd, ["run-1", "--sdd-dir", str(sdd_dir), "--verify"])

    assert result.exit_code == 0
    assert "byte-identical" in result.output.lower() or "verified" in result.output.lower()


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
