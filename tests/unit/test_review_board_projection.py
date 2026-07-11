"""Unit tests for the run-journal review-board projection (#2365).

The board is a pure projection of the per-run event journal: two
operators holding the same journal bytes must render byte-identical
board state, and the projection carries the journal head hash so
``audit verify`` semantics extend to what a reviewer saw.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.replay.review_board import (
    BOARD_COLUMNS,
    BOARD_SCHEMA_VERSION,
    EVENT_TASK_MERGED,
    board_hash,
    canonical_board_bytes,
    list_board_runs,
    project_board,
    project_run,
    record_task_merged,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_lifecycle_journal(sdd_dir: Path, run_id: str) -> EventJournal:
    """Record a representative task lifecycle into a fresh run journal."""
    journal = EventJournal(run_id, sdd_dir)
    journal.record("run_started", run_id=run_id, git_branch="main", git_sha="abc1234", config_hash="cfg")
    journal.record("task_claimed", task_id="t-1", agent_id="agent-a", model="model-x")
    journal.record("task_claimed", task_id="t-2", agent_id="agent-b", model="model-y")
    journal.record("task_verification_failed", task_id="t-2", failed_signals=["tests", "lint"])
    journal.record("task_retried", task_id="t-2")
    journal.record("task_completed", task_id="t-1", agent_id="agent-a", cost_usd=0.42)
    journal.record(EVENT_TASK_MERGED, task_id="t-1", agent_id="agent-a")
    journal.record("run_completed", run_id=run_id, ticks=7, fingerprint=journal.fingerprint())
    return journal


def _column_of(board: dict[str, Any], task_id: str) -> str:
    for column, cards in board["columns"].items():
        for card in cards:
            if card["task_id"] == task_id:
                return str(column)
    msg = f"task {task_id} not on board"
    raise AssertionError(msg)


# ---------------------------------------------------------------------------
# Column semantics
# ---------------------------------------------------------------------------


def test_board_columns_are_the_documented_five() -> None:
    """The projection exposes exactly the five review-queue columns."""
    assert BOARD_COLUMNS == ("queued", "running", "gated", "needs_review", "merged")


def test_full_lifecycle_lands_cards_in_expected_columns(tmp_path: Path) -> None:
    """claimed -> running; gate fail -> gated; retried -> queued; completed -> needs_review; merged -> merged."""
    journal = _write_lifecycle_journal(tmp_path / ".sdd", "run-1")
    board = project_board(load_events(journal.path))

    assert board["schema_version"] == BOARD_SCHEMA_VERSION
    assert set(board["columns"]) == set(BOARD_COLUMNS)
    assert _column_of(board, "t-1") == "merged"
    assert _column_of(board, "t-2") == "queued"

    merged_card = board["columns"]["merged"][0]
    assert merged_card["agent_id"] == "agent-a"
    assert merged_card["model"] == "model-x"
    assert merged_card["cost_usd"] == 0.42

    queued_card = board["columns"]["queued"][0]
    assert queued_card["attempts"] == 1
    assert queued_card["gate_failures"] == ["tests", "lint"]


def test_verification_failure_moves_card_to_gated(tmp_path: Path) -> None:
    """A gate failure without a retry leaves the card in the gated column."""
    journal = EventJournal("run-g", tmp_path / ".sdd")
    journal.record("task_claimed", task_id="t-9", agent_id="agent-z", model=None)
    journal.record("task_verification_failed", task_id="t-9", failed_signals=["janitor"])
    board = project_board(load_events(journal.path))
    assert _column_of(board, "t-9") == "gated"


def test_completed_without_merge_stays_in_needs_review(tmp_path: Path) -> None:
    """Verified-but-unmerged work waits in needs_review for the operator."""
    journal = EventJournal("run-nr", tmp_path / ".sdd")
    journal.record("task_claimed", task_id="t-3", agent_id="agent-c", model="m")
    journal.record("task_completed", task_id="t-3", agent_id="agent-c", cost_usd=0.1)
    board = project_board(load_events(journal.path))
    assert _column_of(board, "t-3") == "needs_review"


def test_unknown_event_types_are_ignored(tmp_path: Path) -> None:
    """Future event types must not break the fold (forward compatibility)."""
    journal = EventJournal("run-u", tmp_path / ".sdd")
    journal.record("task_claimed", task_id="t-4", agent_id="a", model=None)
    journal.record("some_future_event", task_id="t-4", extra="ignored")
    board = project_board(load_events(journal.path))
    assert _column_of(board, "t-4") == "running"


def test_run_metadata_projected_from_run_events(tmp_path: Path) -> None:
    """run_started/run_completed populate the board's run envelope."""
    journal = _write_lifecycle_journal(tmp_path / ".sdd", "run-meta")
    board = project_board(load_events(journal.path))
    assert board["run"]["run_id"] == "run-meta"
    assert board["run"]["git_branch"] == "main"
    assert board["run"]["git_sha"] == "abc1234"
    assert board["run"]["completed"] is True
    assert board["run"]["ticks"] == 7


# ---------------------------------------------------------------------------
# Determinism (AC: byte-identical reconstruction from journal replay)
# ---------------------------------------------------------------------------


def test_projection_is_byte_identical_across_replays(tmp_path: Path) -> None:
    """Two folds over the same journal produce byte-identical canonical state."""
    journal = _write_lifecycle_journal(tmp_path / ".sdd", "run-det")
    first = project_board(load_events(journal.path))
    second = project_board(load_events(journal.path))
    assert canonical_board_bytes(first) == canonical_board_bytes(second)
    assert board_hash(first) == board_hash(second)


def test_projection_is_byte_identical_across_operators(tmp_path: Path) -> None:
    """Two operators recording the same logical run render identical boards.

    The journals are written at different wall-clock times into different
    directories; the board state and its hash must not observe timing.
    """
    journal_a = _write_lifecycle_journal(tmp_path / "op-a" / ".sdd", "run-x")
    journal_b = _write_lifecycle_journal(tmp_path / "op-b" / ".sdd", "run-x")

    board_a = project_board(load_events(journal_a.path))
    board_b = project_board(load_events(journal_b.path))

    assert canonical_board_bytes(board_a) == canonical_board_bytes(board_b)
    assert board_hash(board_a) == board_hash(board_b)


def test_project_run_binds_projection_to_journal_head(tmp_path: Path) -> None:
    """The projection receipt carries the Merkle head + verification verdict."""
    sdd = tmp_path / ".sdd"
    journal = _write_lifecycle_journal(sdd, "run-head")

    projection = project_run(sdd, "run-head")
    assert projection is not None
    assert projection.run_id == "run-head"
    assert projection.journal_head == journal.head()
    assert projection.journal_verified is True
    assert projection.event_count == 8
    assert projection.projection_hash == board_hash(projection.board)

    payload = projection.to_dict()
    assert payload["journal_head"] == journal.head()
    assert payload["projection_hash"] == projection.projection_hash


def test_project_run_flags_tampered_journal(tmp_path: Path) -> None:
    """Editing a journal row flips journal_verified to False in the receipt."""
    sdd = tmp_path / ".sdd"
    journal = _write_lifecycle_journal(sdd, "run-tamper")

    lines = journal.path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["task_id"] = "t-evil"
    lines[1] = json.dumps(row, default=str)
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    projection = project_run(sdd, "run-tamper")
    assert projection is not None
    assert projection.journal_verified is False


def test_project_run_missing_journal_returns_none(tmp_path: Path) -> None:
    """A run id without a journal yields None (route turns this into 404)."""
    assert project_run(tmp_path / ".sdd", "no-such-run") is None


# ---------------------------------------------------------------------------
# Run listing (detached runs are plain on-disk journals)
# ---------------------------------------------------------------------------


def test_list_board_runs_returns_journal_backed_runs(tmp_path: Path) -> None:
    """Only run dirs with a journal file are listed, newest name first."""
    sdd = tmp_path / ".sdd"
    _write_lifecycle_journal(sdd, "run-2026-01")
    _write_lifecycle_journal(sdd, "run-2026-02")
    (sdd / "runs" / "empty-run").mkdir(parents=True)

    assert list_board_runs(sdd) == ["run-2026-02", "run-2026-01"]


def test_list_board_runs_empty_when_no_runs(tmp_path: Path) -> None:
    assert list_board_runs(tmp_path / ".sdd") == []


# ---------------------------------------------------------------------------
# Merge receipt recording (the journal event the merged column projects from)
# ---------------------------------------------------------------------------


def test_record_task_merged_appends_journal_event(tmp_path: Path) -> None:
    """The helper appends a chained task_merged event to the run journal."""
    journal = EventJournal("run-m", tmp_path / ".sdd")
    record_task_merged(journal, task_id="t-7", agent_id="agent-m")

    events = load_events(journal.path)
    assert len(events) == 1
    assert events[0]["event"] == EVENT_TASK_MERGED
    assert events[0]["task_id"] == "t-7"
    assert events[0]["agent_id"] == "agent-m"
    assert journal.verify().ok


def test_record_task_merged_tolerates_missing_recorder(tmp_path: Path) -> None:
    """Callers without a live recorder (tests, detached tools) are a no-op."""
    record_task_merged(None, task_id="t-8", agent_id=None)


def test_reap_and_cleanup_records_task_merged(tmp_path: Path, monkeypatch: Any) -> None:
    """The lifecycle merge seam records task_merged after a successful merge."""
    from types import SimpleNamespace

    from bernstein.core.tasks import task_lifecycle

    monkeypatch.setattr(task_lifecycle, "_close_completed_task", lambda *_a, **_k: None)
    monkeypatch.setattr(task_lifecycle, "seal_evidence_on_completion", lambda *_a, **_k: None)

    recorder = EventJournal("run-seam", tmp_path / ".sdd")
    merge_result = SimpleNamespace(success=True, conflicting_files=[])
    spawner = SimpleNamespace(
        reap_completed_agent=lambda *_a, **_k: merge_result,
        cleanup_worktree=lambda _sid: None,
    )
    orch = SimpleNamespace(
        _spawner=spawner,
        _workdir=tmp_path,
        _config=SimpleNamespace(ab_test=False),
        _recorder=recorder,
    )
    session = SimpleNamespace(id="sess-1", status="dead", exit_code=0, task_ids=["t-42"])
    task = SimpleNamespace(id="t-42", metadata={})

    task_lifecycle._reap_and_cleanup_session(
        orch,
        task,
        session,
        None,
        janitor_passed=True,
        skip_merge=False,
        _completion_data=None,
        cache_diff_lines=0,
    )

    events = load_events(recorder.path)
    merged = [e for e in events if e.get("event") == EVENT_TASK_MERGED]
    assert len(merged) == 1
    assert merged[0]["task_id"] == "t-42"
    assert merged[0]["agent_id"] == "sess-1"


def test_reap_and_cleanup_skips_merge_receipt_when_merge_skipped(tmp_path: Path, monkeypatch: Any) -> None:
    """skip_merge (PR mode) must not fabricate a merged receipt."""
    from types import SimpleNamespace

    from bernstein.core.tasks import task_lifecycle

    monkeypatch.setattr(task_lifecycle, "_close_completed_task", lambda *_a, **_k: None)
    monkeypatch.setattr(task_lifecycle, "seal_evidence_on_completion", lambda *_a, **_k: None)

    recorder = EventJournal("run-skip", tmp_path / ".sdd")
    spawner = SimpleNamespace(
        reap_completed_agent=lambda *_a, **_k: None,
        cleanup_worktree=lambda _sid: None,
    )
    orch = SimpleNamespace(
        _spawner=spawner,
        _workdir=tmp_path,
        _config=SimpleNamespace(ab_test=False),
        _recorder=recorder,
    )
    session = SimpleNamespace(id="sess-2", status="dead", exit_code=0, task_ids=["t-43"])
    task = SimpleNamespace(id="t-43", metadata={})

    task_lifecycle._reap_and_cleanup_session(
        orch,
        task,
        session,
        None,
        janitor_passed=True,
        skip_merge=True,
        _completion_data=None,
        cache_diff_lines=0,
    )

    events = load_events(recorder.path)
    assert [e for e in events if e.get("event") == EVENT_TASK_MERGED] == []
