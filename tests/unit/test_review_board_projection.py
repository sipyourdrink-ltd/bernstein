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
    EVENT_TASK_DIFF_CAPTURED,
    EVENT_TASK_MERGED,
    EVENT_TASK_REVIEW_DECISION,
    REVIEW_DECISIONS,
    board_hash,
    canonical_board_bytes,
    diff_summary,
    list_board_runs,
    project_board,
    project_run,
    read_task_diff,
    record_review_decision,
    record_task_diff_captured,
    record_task_merged,
    store_task_diff,
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


# ---------------------------------------------------------------------------
# Diff capture: a content-addressed, journal-anchored review artifact (#2365)
# ---------------------------------------------------------------------------

_SAMPLE_DIFF = """diff --git a/src/a.py b/src/a.py
index 111..222 100644
--- a/src/a.py
+++ b/src/a.py
@@ -1,3 +1,4 @@
 keep
-old line
+new line
+extra line
diff --git a/src/b.py b/src/b.py
index 333..444 100644
--- a/src/b.py
+++ b/src/b.py
@@ -10,2 +10,2 @@ def f():
-    return 1
+    return 2
"""


def test_diff_summary_is_deterministic_and_counts_changes() -> None:
    """diff_summary is a pure function: same text -> identical hash and stats."""
    first = diff_summary(_SAMPLE_DIFF)
    second = diff_summary(_SAMPLE_DIFF)
    assert first == second
    assert first["sha256"].startswith("sha256:")
    assert first["added"] == 3
    assert first["removed"] == 2
    assert first["files_changed"] == 2
    assert first["files"] == ["src/a.py", "src/b.py"]


def test_diff_summary_hash_matches_sha256_of_utf8_bytes() -> None:
    import hashlib

    expected = "sha256:" + hashlib.sha256(_SAMPLE_DIFF.encode("utf-8")).hexdigest()
    assert diff_summary(_SAMPLE_DIFF)["sha256"] == expected


def test_store_and_read_task_diff_roundtrip(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    (sdd / "runs" / "run-d").mkdir(parents=True)
    summary = store_task_diff(sdd, "run-d", "t-1", _SAMPLE_DIFF)
    assert summary is not None
    assert summary["sha256"] == diff_summary(_SAMPLE_DIFF)["sha256"]

    result = read_task_diff(sdd, "run-d", "t-1")
    assert result is not None
    text, read_summary = result
    assert text == _SAMPLE_DIFF
    assert read_summary["sha256"] == summary["sha256"]


def test_store_task_diff_overwrites_with_latest_capture(tmp_path: Path) -> None:
    """A re-captured task (retry) stores the latest diff bytes."""
    sdd = tmp_path / ".sdd"
    (sdd / "runs" / "run-d").mkdir(parents=True)
    store_task_diff(sdd, "run-d", "t-1", _SAMPLE_DIFF)
    later = _SAMPLE_DIFF + "diff --git a/c.py b/c.py\n+++ b/c.py\n@@ -0,0 +1 @@\n+added\n"
    store_task_diff(sdd, "run-d", "t-1", later)
    result = read_task_diff(sdd, "run-d", "t-1")
    assert result is not None
    assert result[0] == later


def test_read_task_diff_absent_returns_none(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    (sdd / "runs" / "run-d").mkdir(parents=True)
    assert read_task_diff(sdd, "run-d", "t-missing") is None


def test_store_task_diff_rejects_path_traversal_task_id(tmp_path: Path) -> None:
    """A task id that escapes the diffs dir is refused (realpath containment)."""
    sdd = tmp_path / ".sdd"
    (sdd / "runs" / "run-d").mkdir(parents=True)
    assert store_task_diff(sdd, "run-d", "../../evil", _SAMPLE_DIFF) is None
    assert read_task_diff(sdd, "run-d", "../../evil") is None
    assert not (tmp_path / "evil.diff").exists()


def test_fold_diff_captured_attaches_summary_without_moving_column() -> None:
    summary = diff_summary(_SAMPLE_DIFF)
    events = [
        {"event": "task_claimed", "task_id": "t-1", "agent_id": "a", "model": "m"},
        {"event": "task_completed", "task_id": "t-1"},
        {
            "event": EVENT_TASK_DIFF_CAPTURED,
            "task_id": "t-1",
            "diff_sha256": summary["sha256"],
            "diff_added": summary["added"],
            "diff_removed": summary["removed"],
            "diff_files": summary["files_changed"],
        },
    ]
    board = project_board(events)
    card = board["columns"]["needs_review"][0]
    assert card["diff"]["sha256"] == summary["sha256"]
    assert card["diff"]["added"] == 3
    assert card["diff"]["removed"] == 2
    assert card["diff"]["files"] == 2


def test_fold_review_decision_merge_moves_card_to_merged() -> None:
    events = [
        {"event": "task_claimed", "task_id": "t-1"},
        {"event": "task_completed", "task_id": "t-1"},
        {
            "event": EVENT_TASK_REVIEW_DECISION,
            "task_id": "t-1",
            "decision": "merge",
            "principal": "operator-olga",
            "scope": "operator",
        },
    ]
    board = project_board(events)
    assert board["columns"]["needs_review"] == []
    card = board["columns"]["merged"][0]
    assert card["review"]["decision"] == "merge"
    assert card["review"]["principal"] == "operator-olga"


def test_fold_review_decision_approve_annotates_without_moving() -> None:
    events = [
        {"event": "task_claimed", "task_id": "t-1"},
        {"event": "task_completed", "task_id": "t-1"},
        {
            "event": EVENT_TASK_REVIEW_DECISION,
            "task_id": "t-1",
            "decision": "approve",
            "principal": "alice",
            "scope": "operator",
        },
    ]
    board = project_board(events)
    card = board["columns"]["needs_review"][0]
    assert card["review"]["decision"] == "approve"
    assert card["review"]["principal"] == "alice"


def test_fold_review_decision_request_changes_annotates() -> None:
    events = [
        {"event": "task_claimed", "task_id": "t-1"},
        {"event": "task_completed", "task_id": "t-1"},
        {
            "event": EVENT_TASK_REVIEW_DECISION,
            "task_id": "t-1",
            "decision": "request_changes",
            "principal": "bob",
            "scope": "operator",
        },
    ]
    board = project_board(events)
    card = board["columns"]["needs_review"][0]
    assert card["review"]["decision"] == "request_changes"


def test_fold_ignores_unknown_review_decision() -> None:
    """An unknown decision value is ignored (forward compatibility)."""
    events = [
        {"event": "task_claimed", "task_id": "t-1"},
        {"event": "task_completed", "task_id": "t-1"},
        {"event": EVENT_TASK_REVIEW_DECISION, "task_id": "t-1", "decision": "nuke", "principal": "x"},
    ]
    board = project_board(events)
    card = board["columns"]["needs_review"][0]
    assert card["review"] is None
    assert "merge" not in REVIEW_DECISIONS or card["column"] == "needs_review"


def test_projection_byte_identical_with_diff_and_review_events() -> None:
    """Determinism holds across the new event vocabulary."""
    summary = diff_summary(_SAMPLE_DIFF)
    events = [
        {"event": "run_started", "run_id": "r", "git_branch": "main", "git_sha": "abc"},
        {"event": "task_claimed", "task_id": "t-1", "agent_id": "a", "model": "m"},
        {"event": "task_completed", "task_id": "t-1"},
        {
            "event": EVENT_TASK_DIFF_CAPTURED,
            "task_id": "t-1",
            "diff_sha256": summary["sha256"],
            "diff_added": summary["added"],
            "diff_removed": summary["removed"],
            "diff_files": summary["files_changed"],
        },
        {
            "event": EVENT_TASK_REVIEW_DECISION,
            "task_id": "t-1",
            "decision": "approve",
            "principal": "alice",
            "scope": "operator",
        },
    ]
    first = project_board(events)
    second = project_board(list(events))
    assert canonical_board_bytes(first) == canonical_board_bytes(second)
    assert board_hash(first) == board_hash(second)


def test_record_review_decision_appends_journal_event(tmp_path: Path) -> None:
    journal = EventJournal("run-rd", tmp_path / ".sdd")
    journal.record("task_completed", task_id="t-1")
    record_review_decision(journal, task_id="t-1", decision="approve", principal="alice", scope="operator")
    events = load_events(journal.path)
    row = next(e for e in events if e.get("event") == EVENT_TASK_REVIEW_DECISION)
    assert row["task_id"] == "t-1"
    assert row["decision"] == "approve"
    assert row["principal"] == "alice"
    assert row["scope"] == "operator"
    assert journal.verify().ok


def test_record_task_diff_captured_appends_event(tmp_path: Path) -> None:
    journal = EventJournal("run-dc", tmp_path / ".sdd")
    summary = diff_summary(_SAMPLE_DIFF)
    record_task_diff_captured(journal, task_id="t-1", summary=summary)
    events = load_events(journal.path)
    row = next(e for e in events if e.get("event") == EVENT_TASK_DIFF_CAPTURED)
    assert row["diff_sha256"] == summary["sha256"]
    assert row["diff_added"] == summary["added"]
    assert journal.verify().ok


def test_record_helpers_tolerate_missing_recorder() -> None:
    record_review_decision(None, task_id="t", decision="approve", principal="a", scope="operator")
    record_task_diff_captured(None, task_id="t", summary=diff_summary(_SAMPLE_DIFF))


def test_reap_and_cleanup_captures_review_diff(tmp_path: Path, monkeypatch: Any) -> None:
    """The reap seam captures the worktree diff as a chained review artifact."""
    from types import SimpleNamespace

    from bernstein.core.tasks import task_lifecycle

    monkeypatch.setattr(task_lifecycle, "_close_completed_task", lambda *_a, **_k: None)
    monkeypatch.setattr(task_lifecycle, "seal_evidence_on_completion", lambda *_a, **_k: None)
    monkeypatch.setattr(task_lifecycle, "_get_git_diff_text_in_worktree", lambda _wt: _SAMPLE_DIFF)

    recorder = EventJournal("run-diffseam", tmp_path / ".sdd")
    merge_result = SimpleNamespace(success=True, conflicting_files=[])
    spawner = SimpleNamespace(
        reap_completed_agent=lambda *_a, **_k: merge_result,
        cleanup_worktree=lambda _sid: None,
        get_worktree_path=lambda _sid: tmp_path / "wt",
    )
    orch = SimpleNamespace(
        _spawner=spawner,
        _workdir=tmp_path,
        _config=SimpleNamespace(ab_test=False),
        _recorder=recorder,
    )
    session = SimpleNamespace(id="sess-1", status="dead", exit_code=0, task_ids=["t-9"])
    task = SimpleNamespace(id="t-9", metadata={})

    task_lifecycle._reap_and_cleanup_session(
        orch, task, session, None, janitor_passed=True, skip_merge=False, _completion_data=None, cache_diff_lines=0
    )

    events = load_events(recorder.path)
    captured = [e for e in events if e.get("event") == EVENT_TASK_DIFF_CAPTURED]
    assert len(captured) == 1
    assert captured[0]["diff_sha256"] == diff_summary(_SAMPLE_DIFF)["sha256"]
    stored = read_task_diff(tmp_path / ".sdd", "run-diffseam", "t-9")
    assert stored is not None
    assert stored[0] == _SAMPLE_DIFF
    assert recorder.verify().ok
