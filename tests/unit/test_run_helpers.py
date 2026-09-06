"""Run-helper classification and CAS capture (#5322 PR1)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from bernstein.core.persistence.cas_store import CASStore
from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.worktrees.run_helpers import (
    JOURNAL_EVENT_RUN_HELPER_CAPTURED,
    RunHelper,
    capture_run_helpers,
    classify_run_helpers,
)


def test_unexecuted_scaffolding_is_not_a_helper() -> None:
    """Created-but-never-executed files are scaffolding, not helpers."""

    events = [
        {"index": 0, "event": "file_create", "path": "scratch/setup.py"},
        {"index": 1, "event": "file_create", "path": "notes.md"},
        # no file_execute for either path
    ]
    assert classify_run_helpers(events) == []


def test_executed_helper_gets_origin_step_and_hash(tmp_path: Path) -> None:
    """An executed agent-created file gets origin_step + CAS content hash."""

    worktree = tmp_path / "wt"
    worktree.mkdir()
    body = b"raise SystemExit(1)\n"
    (worktree / "repro.py").write_bytes(body)

    events = [
        {"index": 3, "event": "file_create", "path": "repro.py"},
        {"index": 7, "event": "file_execute", "path": "repro.py", "exit_code": 1},
        {"index": 8, "event": "file_execute", "path": "repro.py", "exit_code": 1},
    ]
    helpers = classify_run_helpers(events)
    assert len(helpers) == 1
    helper = helpers[0]
    assert helper.path == "repro.py"
    assert helper.origin_step == 3
    assert helper.execution_count == 2
    assert helper.exit_codes == (1, 1)
    assert helper.content_hash is None

    cas = CASStore(tmp_path / "cas")
    sdd = tmp_path / ".sdd"
    journal = EventJournal(run_id="run-helper-1", sdd_dir=sdd)
    captured = capture_run_helpers(worktree, helpers, cas, journal=journal)
    assert len(captured) == 1
    assert captured[0].origin_step == 3
    expected = hashlib.sha256(body).hexdigest()
    assert captured[0].content_hash == expected
    assert cas.has(expected)

    rows = load_events(journal.path).events
    naming = [r for r in rows if r.get("event") == JOURNAL_EVENT_RUN_HELPER_CAPTURED]
    assert len(naming) == 1
    assert naming[0]["path"] == "repro.py"
    assert naming[0]["origin_step"] == 3
    assert naming[0]["content_hash"] == expected


def test_nonzero_exit_still_counts_as_helper() -> None:
    """Non-zero exit codes are recorded but do not exclude the helper."""

    events = [
        {"index": 1, "event": "file_create", "path": "harness.py"},
        {"index": 2, "event": "file_execute", "path": "harness.py", "exit_code": 2},
    ]
    helpers = classify_run_helpers(events)
    assert helpers == [
        RunHelper(
            path="harness.py",
            origin_step=1,
            content_hash=None,
            execution_count=1,
            exit_codes=(2,),
        )
    ]


def test_execute_without_create_is_not_a_helper() -> None:
    events = [
        {"index": 0, "event": "file_execute", "path": "vendor/bin", "exit_code": 0},
    ]
    assert classify_run_helpers(events) == []
