"""Tests for insights persistence."""

from __future__ import annotations

import json
import time
from pathlib import Path

from bernstein.core.persistence.insights import (
    generate_failure_classes_insights,
    generate_flaky_tests_insights,
    save_failure_classes_insights,
    save_flaky_tests_insights,
)
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_CLOSED,
    KIND_TASK_COMPLETED,
    KIND_TASK_FAILED,
    LedgerEntry,
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
    # Setup a ledger a ledger directory for a fake run
    run_id = "test-run-001"
    ledger_dir = run_ledger_dir(tmp_path, run_id)
    ledger_dir.mkdir(parents=True)
    # No run.closed entry
    # Generate insights
    insights = generate_failure_classes_insights(tmp_path)
    assert insights.data["failure_classes"] == []


def test_flaky_tests_insights_empty(tmp_path: Path) -> None:
    """With no flaky tests, the insight should be an empty list."""
    run_id = "test-run-001"
    ledger_dir = run_ledger_dir(tmp_path, run_id)
    ledger_dir.mkdir(parents=True)
    # No task.completed or task.failed entries
    insights = generate_flaky_tests_insights(tmp_path)
    assert insights.data["flaky_tests"] == []


def test_flaky_tests_insights_single_test_only_passed(tmp_path: Path) -> None:
    """A test that only passed should not be considered flaky."""
    run_id = "test-run-001"
    ledger_dir = run_ledger_dir(tmp_path, run_id)
    ledger_dir.mkdir(parents=True)
    # Add a task.completed entry for a test
    entry = LedgerEntry(
        seq=0,
        prev_hash="0" * 64,
        kind=KIND_TASK_COMPLETED,
        task_id="test_example",
        payload={},
        entry_hash="0" * 64,
        ts=time.time(),
    )
    (ledger_dir / "000000.jsonl").write_text(json.dumps(entry.to_dict()) + "\n", encoding="utf-8")
    insights = generate_flaky_tests_insights(tmp_path)
    assert insights.data["flaky_tests"] == []


def test_flaky_tests_insights_single_test_only_failed(tmp_path: Path) -> None:
    """A test that only failed should not be considered flaky."""
    run_id = "test-run-001"
    ledger_dir = run_ledger_dir(tmp_path, run_id)
    ledger_dir.mkdir(parents=True)
    # Add a task.failed entry for a test
    entry = LedgerEntry(
        seq=0,
        prev_hash="0" * 64,
        kind=KIND_TASK_FAILED,
        task_id="test_example",
        payload={},
        entry_hash="0" * 64,
        ts=time.time(),
    )
    (ledger_dir / "000000.jsonl").write_text(json.dumps(entry.to_dict()) + "\n", encoding="utf-8")
    insights = generate_flaky_tests_insights(tmp_path)
    assert insights.data["flaky_tests"] == []


def test_flaky_tests_insights_test_failed_then_passed(tmp_path: Path) -> None:
    """A test that failed then passed should be considered flaky."""
    run_id = "test-run-001"
    ledger_dir = run_ledger_dir(tmp_path, run_id)
    ledger_dir.mkdir(parents=True)
    ts_first = time.time()
    ts_second = ts_first + 10.0
    # First: test failed
    entry_failed = LedgerEntry(
        seq=0,
        prev_hash="0" * 64,
        kind=KIND_TASK_FAILED,
        task_id="test_example",
        payload={},
        entry_hash="0" * 64,
        ts=ts_first,
    )
    # Second: test passed
    entry_passed = LedgerEntry(
        seq=1,
        prev_hash="0" * 64,
        kind=KIND_TASK_COMPLETED,
        task_id="test_example",
        payload={},
        entry_hash="0" * 64,
        ts=ts_second,
    )
    (ledger_dir / "000000.jsonl").write_text(
        json.dumps(entry_failed.to_dict()) + "\n" + json.dumps(entry_passed.to_dict()) + "\n", encoding="utf-8"
    )
    insights = generate_flaky_tests_insights(tmp_path)
    flaky_tests = insights.data["flaky_tests"]
    assert len(flaky_tests) == 1
    assert flaky_tests[0]["test_name"] == "test_example"
    assert flaky_tests[0]["flaky_count"] == 1  # One transition (failed -> passed)
    assert flaky_tests[0]["first_seen"] == ts_first
    assert flaky_tests[0]["last_seen"] == ts_second
    assert flaky_tests[0]["patterns"] == ["failed", "passed"]


def test_flaky_tests_insights_test_passed_then_failed_then_passed(tmp_path: Path) -> None:
    """A test with multiple transitions should count all transitions."""
    run_id = "test-run-001"
    ledger_dir = run_ledger_dir(tmp_path, run_id)
    ledger_dir.mkdir(parents=True)
    ts_first = time.time()
    ts_second = ts_first + 10.0
    ts_third = ts_second + 10.0
    # First: test passed
    entry_passed1 = LedgerEntry(
        seq=0,
        prev_hash="0" * 64,
        kind=KIND_TASK_COMPLETED,
        task_id="test_example",
        payload={},
        entry_hash="0" * 64,
        ts=ts_first,
    )
    # Second: test failed
    entry_failed = LedgerEntry(
        seq=1,
        prev_hash="0" * 64,
        kind=KIND_TASK_FAILED,
        task_id="test_example",
        payload={},
        entry_hash="0" * 64,
        ts=ts_second,
    )
    # Third: test passed again
    entry_passed2 = LedgerEntry(
        seq=2,
        prev_hash="0" * 64,
        kind=KIND_TASK_COMPLETED,
        task_id="test_example",
        payload={},
        entry_hash="0" * 64,
        ts=ts_third,
    )
    (ledger_dir / "000000.jsonl").write_text(
        json.dumps(entry_passed1.to_dict())
        + "\n"
        + json.dumps(entry_failed.to_dict())
        + "\n"
        + json.dumps(entry_passed2.to_dict())
        + "\n",
        encoding="utf-8",
    )
    insights = generate_flaky_tests_insights(tmp_path)
    flaky_tests = insights.data["flaky_tests"]
    assert len(flaky_tests) == 1
    assert flaky_tests[0]["test_name"] == "test_example"
    assert flaky_tests[0]["flaky_count"] == 2  # Two transitions (passed->failed, failed->passed)
    assert flaky_tests[0]["first_seen"] == ts_first
    assert flaky_tests[0]["last_seen"] == ts_third
    assert flaky_tests[0]["patterns"] == ["passed", "failed", "passed"]


def test_flaky_tests_insights_non_test_tasks_ignored(tmp_path: Path) -> None:
    """Non-test tasks (not starting with 'test_') should be ignored."""
    run_id = "test-run-001"
    ledger_dir = run_ledger_dir(tmp_path, run_id)
    ledger_dir.mkdir(parents=True)
    ts_first = time.time()
    ts_second = ts_first + 10.0
    # First: non-test task failed
    entry_failed = LedgerEntry(
        seq=0,
        prev_hash="0" * 64,
        kind=KIND_TASK_FAILED,
        task_id="build_step",
        payload={},
        entry_hash="0" * 64,
        ts=ts_first,
    )
    # Second: non-test task passed
    entry_passed = LedgerEntry(
        seq=1,
        prev_hash="0" * 64,
        kind=KIND_TASK_COMPLETED,
        task_id="build_step",
        payload={},
        entry_hash="0" * 64,
        ts=ts_second,
    )
    (ledger_dir / "000000.jsonl").write_text(
        json.dumps(entry_failed.to_dict()) + "\n" + json.dumps(entry_passed.to_dict()) + "\n", encoding="utf-8"
    )
    insights = generate_flaky_tests_insights(tmp_path)
    assert insights.data["flaky_tests"] == []


def test_flaky_tests_insights_multiple_tests(tmp_path: Path) -> None:
    """Multiple tests with flaky behavior should all be detected."""
    run_id = "test-run-001"
    ledger_dir = run_ledger_dir(tmp_path, run_id)
    ledger_dir.mkdir(parents=True)
    ts = time.time()
    # Test 1: failed then passed
    entry_t1_failed = LedgerEntry(
        seq=0,
        prev_hash="0" * 64,
        kind=KIND_TASK_FAILED,
        task_id="test_unit_foo",
        payload={},
        entry_hash="0" * 64,
        ts=ts,
    )
    entry_t1_passed = LedgerEntry(
        seq=1,
        prev_hash="0" * 64,
        kind=KIND_TASK_COMPLETED,
        task_id="test_unit_foo",
        payload={},
        entry_hash="0" * 64,
        ts=ts + 1.0,
    )
    # Test 2: passed then failed then passed (2 transitions)
    entry_t2_passed1 = LedgerEntry(
        seq=2,
        prev_hash="0" * 64,
        kind=KIND_TASK_COMPLETED,
        task_id="test_integration_bar",
        payload={},
        entry_hash="0" * 64,
        ts=ts + 2.0,
    )
    entry_t2_failed = LedgerEntry(
        seq=3,
        prev_hash="0" * 64,
        kind=KIND_TASK_FAILED,
        task_id="test_integration_bar",
        payload={},
        entry_hash="0" * 64,
        ts=ts + 3.0,
    )
    entry_t2_passed2 = LedgerEntry(
        seq=4,
        prev_hash="0" * 64,
        kind=KIND_TASK_COMPLETED,
        task_id="test_integration_bar",
        payload={},
        entry_hash="0" * 64,
        ts=ts + 4.0,
    )
    (ledger_dir / "000000.jsonl").write_text(
        json.dumps(entry_t1_failed.to_dict())
        + "\n"
        + json.dumps(entry_t1_passed.to_dict())
        + "\n"
        + json.dumps(entry_t2_passed1.to_dict())
        + "\n"
        + json.dumps(entry_t2_failed.to_dict())
        + "\n"
        + json.dumps(entry_t2_passed2.to_dict())
        + "\n",
        encoding="utf-8",
    )
    insights = generate_flaky_tests_insights(tmp_path)
    flaky_tests = insights.data["flaky_tests"]
    assert len(flaky_tests) == 2
    # Check that both tests are present
    test_names = {t["test_name"] for t in flaky_tests}
    assert test_names == {"test_unit_foo", "test_integration_bar"}
    # Check transition counts
    for t in flaky_tests:
        if t["test_name"] == "test_unit_foo":
            assert t["flaky_count"] == 1  # failed -> passed
            assert t["patterns"] == ["failed", "passed"]
        else:  # test_integration_bar
            assert t["flaky_count"] == 2  # passed -> failed -> passed
            assert t["patterns"] == ["passed", "failed", "passed"]


def test_save_flaky_tests_insights_creates_file(tmp_path: Path) -> None:
    """Saving the flaky tests insight creates the insights.json file."""
    run_id = "test-run-001"
    ledger_dir = run_ledger_dir(tmp_path, run_id)
    ledger_dir.mkdir(parents=True)
    ts_first = time.time()
    ts_second = ts_first + 10.0
    # First: test failed
    entry_failed = LedgerEntry(
        seq=0,
        prev_hash="0" * 64,
        kind=KIND_TASK_FAILED,
        task_id="test_example",
        payload={},
        entry_hash="0" * 64,
        ts=ts_first,
    )
    # Second: test passed
    entry_passed = LedgerEntry(
        seq=1,
        prev_hash="0" * 64,
        kind=KIND_TASK_COMPLETED,
        task_id="test_example",
        payload={},
        entry_hash="0" * 64,
        ts=ts_second,
    )
    (ledger_dir / "000000.jsonl").write_text(
        json.dumps(entry_failed.to_dict()) + "\n" + json.dumps(entry_passed.to_dict()) + "\n", encoding="utf-8"
    )
    # Initially, no insights file
    assert not (tmp_path / ".sdd" / "runtime" / "insights.json").exists()
    save_flaky_tests_insights(tmp_path)
    insights_path = tmp_path / ".sdd" / "runtime" / "insights.json"
    assert insights_path.exists()
    # Load and check content
    from bernstein.core.persistence.insights import load_insights

    loaded = load_insights(tmp_path)
    assert loaded is not None
    assert len(loaded.data["flaky_tests"]) == 1
    assert loaded.data["flaky_tests"][0]["test_name"] == "test_example"
    assert loaded.data["flaky_tests"][0]["flaky_count"] == 1
    assert loaded.data["flaky_tests"][0]["first_seen"] == ts_first
    assert loaded.data["flaky_tests"][0]["last_seen"] == ts_second


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
    (ledger_dir / "000000.jsonl").write_text(json.dumps(entry.to_dict()) + "\n", encoding="utf-8")
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
    (ledger_dir_a / "000000.jsonl").write_text(json.dumps(entry_a.to_dict()) + "\n", encoding="utf-8")
    (ledger_dir_b / "000000.jsonl").write_text(json.dumps(entry_b.to_dict()) + "\n", encoding="utf-8")
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
    (ledger_dir_a / "000000.jsonl").write_text(json.dumps(entry_a.to_dict()) + "\n", encoding="utf-8")
    (ledger_dir_b / "000000.jsonl").write_text(json.dumps(entry_b.to_dict()) + "\n", encoding="utf-8")
    (ledger_dir_c / "000000.jsonl").write_text(json.dumps(entry_c.to_dict()) + "\n", encoding="utf-8")
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
    (ledger_dir / "000000.jsonl").write_text(json.dumps(entry.to_dict()) + "\n", encoding="utf-8")
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
