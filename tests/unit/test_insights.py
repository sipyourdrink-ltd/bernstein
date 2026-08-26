"""Tests for insights persistence."""

from __future__ import annotations

import json
import time
from pathlib import Path

from bernstein.core.persistence.insights import (
    generate_failure_classes_insights,
    save_failure_classes_insights,
)
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_CLOSED,
    LedgerState,
    LedgerEntry,
    default_ledger_root,
    run_ledger_dir,
)


def _make_run_closed_entry(
    run_id: str,
    *,
    gate_name: str = "",
    failing_check: str = "",
    branch: str = "",
    pr_number: int | None = None,
    commits_over_base: int | None = None,
    error_kind: str = "",
    error_message: str = "",
    ts: float | None = None,
) -> LedgerEntry:
    """Helper to create a run.closed ledger entry."""
    from bernstein.core.persistence.runs_report import RunWrapUp

    wrapup = RunWrapUp(
        gate_name=gate_name,
        failing_check=failing_check,
        branch=branch,
        pr_number=pr_number,
        commits_over_base=commits_over_base,
        error_kind=error_kind,
        error_message=error_message,
    )
    # Create a minimal LedgerEntry - we only need the payload for our tests
    # The other fields are required but we can set them to dummy values
    return LedgerEntry(
        seq=0,
        prev_hash="0" * 64,
        kind=KIND_RUN_CLOSED,
        task_id="dummy-task-id",
        payload=wrapup.to_payload(),
        entry_hash="0" * 64,
        ts=ts if ts is not None else time.time(),
    )


def test_failure_classes_insights_empty(tmp_path: Path) -> None:
    """With no gate failures, the insight should be an empty list."""
    # Setup a ledger directory for a fake run
    run_id = "test-run-001"
    ledger_dir = run_ledger_dir(tmp_path, run_id)
    ledger_dir.mkdir(parents=True)
    # No run.closed entry
    state = LedgerState([])
    # Generate insights
    insights = generate_failure_classes_insights(tmp_path)
    assert insights.data["failure_classes"] == []


def test_failure_classes_insights_single_gate_failure(tmp_path: Path) -> None:
    """Single gate failure produces one class with count 1."""
    run_id = "test-run-001"
    ledger_dir = run_ledger_dir(tmp_path, run_id)
    ledger_dir.mkdir(parents=True)
    entry = _make_run_closed_entry(
        run_id,
        gate_name="lint",
        failing_check="trailing-whitespace",
    )
    (ledger_dir / "000000.jsonl").write_text(
        json.dumps(entry.to_dict()) + "\n", encoding="utf-8"
    )
    insights = generate_failure_classes_insights(tmp_path)
    classes = insights.data["failure_classes"]
    assert len(classes) == 1
    assert classes[0]["gate_name"] == "lint"
    assert classes[0]["failing_check"] == "trailing-whitespace"
    assert classes[0]["count"] == 1
    # timestamps should be set
    assert isinstance(classes[0]["first_seen"], float)
    assert isinstance(classes[0]["last_seen"], float)


def test_failure_classes_insights_multiple_same_gate_failure(tmp_path: Path) -> None:
    """Multiple runs failing the same gate and check are grouped."""
    run_id_a = "test-run-001"
    run_id_b = "test-run-002"
    ledger_dir_a = run_ledger_dir(tmp_path, run_id_a)
    ledger_dir_b = run_ledger_dir(tmp_path, run_id_b)
    ledger_dir_a.mkdir(parents=True)
    ledger_dir_b.mkdir(parents=True)
    ts_a = time.time()
    ts_b = ts_a + 10.0  # later
    entry_a = _make_run_closed_entry(
        run_id_a,
        gate_name="tests",
        failing_check="test_timeout",
        ts=ts_a,
    )
    entry_b = _make_run_closed_entry(
        run_id_b,
        gate_name="tests",
        failing_check="test_timeout",
        ts=ts_b,
    )
    (ledger_dir_a / "000000.jsonl").write_text(
        json.dumps(entry_a.to_dict()) + "\n", encoding="utf-8"
    )
    (ledger_dir_b / "000000.jsonl").write_text(
        json.dumps(entry_b.to_dict()) + "\n", encoding="utf-8"
    )
    insights = generate_failure_classes_insights(tmp_path)
    classes = insights.data["failure_classes"]
    assert len(classes) == 1
    assert classes[0]["gate_name"] == "tests"
    assert classes[0]["failing_check"] == "test_timeout"
    assert classes[0]["count"] == 2
    assert classes[0]["first_seen"] == ts_a
    assert classes[0]["last_seen"] == ts_b


def test_failure_classes_insights_different_gates_and_checks(tmp_path: Path) -> None:
    """Different gates and checks produce separate classes."""
    run_id_a = "test-run-001"
    run_id_b = "test-run-002"
    run_id_c = "test-run-003"
    ledger_dir_a = run_ledger_dir(tmp_path, run_id_a)
    ledger_dir_b = run_ledger_dir(tmp_path, run_id_b)
    ledger_dir_c = run_ledger_dir(tmp_path, run_id_c)
    for d in (ledger_dir_a, ledger_dir_b, ledger_dir_c):
        d.mkdir(parents=True)
    # Run A: lint, trailing-whitespace
    entry_a = _make_run_closed_entry(run_id_a, gate_name="lint", failing_check="trailing-whitespace", ts=time.time())
    # Run B: lint, line-too-long
    entry_b = _make_run_closed_entry(run_id_b, gate_name="lint", failing_check="line-too-long", ts=time.time())
    # Run C: tests, test_timeout
    entry_c = _make_run_closed_entry(run_id_c, gate_name="tests", failing_check="test_timeout", ts=time.time())
    (ledger_dir_a / "000000.jsonl").write_text(
        json.dumps(entry_a.to_dict()) + "\n", encoding="utf-8"
    )
    (ledger_dir_b / "000000.jsonl").write_text(
        json.dumps(entry_b.to_dict()) + "\n", encoding="utf-8"
    )
    (ledger_dir_c / "000000.jsonl").write_text(
        json.dumps(entry_c.to_dict()) + "\n", encoding="utf-8"
    )
    insights = generate_failure_classes_insights(tmp_path)
    classes = insights.data["failure_classes"]
    # We expect three classes, sorted by count (all count=1) then by gate_name? Actually by count descending.
    # Since all counts are 1, the order is not guaranteed by count, but we can check the set.
    expected = {
        ("lint", "trailing-whitespace"),
        ("lint", "line-too-long"),
        ("tests", "test_timeout"),
    }
    actual = {(c["gate_name"], c["failing_check"]) for c in classes}
    assert actual == expected
    for c in classes:
        assert c["count"] == 1


def test_save_failure_classes_insights_creates_file(tmp_path: Path) -> None:
    """Saving the insight creates the insights.json file."""
    run_id = "test-run-001"
    ledger_dir = run_ledger_dir(tmp_path, run_id)
    ledger_dir.mkdir(parents=True)
    entry = _make_run_closed_entry(
        run_id,
        gate_name="lint",
        failing_check="trailing-whitespace",
    )
    (ledger_dir / "000000.jsonl").write_text(
        json.dumps(entry.to_dict()) + "\n", encoding="utf-8"
    )
    # Initially, no insights file
    assert not (tmp_path / ".sdd" / "runtime" / "insights.json").exists()
    save_failure_classes_insights(tmp_path)
    insights_path = tmp_path / ".sdd" / "runtime" / "insights.json"
    assert insights_path.exists()
    # Load and check content
    from bernstein.core.persistence.insights import load_insights
    loaded = load_insights(tmp_path)
    assert loaded is not None
    assert loaded.data["failure_classes"][0]["gate_name"] == "lint"
    assert loaded.data["failure_classes"][0]["failing_check"] == "trailing-whitespace"
    assert loaded.data["failure_classes"][0]["count"] == 1