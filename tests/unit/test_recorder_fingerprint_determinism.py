"""Determinism tests for the replay execution fingerprint (issue #1851).

The fingerprint is advertised as a cross-run identity ("determinism proof
across runs"). For that to hold it must hash only the deterministic decision
stream, not the wall-clock envelope (``ts`` / ``elapsed_s``) that advances
every run. These tests pin: identical decision streams hash equal even when
timing differs, and real divergence still changes the hash.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.persistence.recorder import compute_replay_fingerprint
from bernstein.core.replay.journal import EventJournal


def _write_replay(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_fingerprint_ignores_wall_clock_fields(tmp_path: Path) -> None:
    """Two logs identical except for ts/elapsed_s hash to the same value.

    This is the heart of issue #1851: a perfectly deterministic re-execution
    only differs in its timing envelope, so the fingerprint must match.
    """
    a = tmp_path / "a" / "replay.jsonl"
    b = tmp_path / "b" / "replay.jsonl"
    _write_replay(
        a,
        [
            {"ts": 100.0, "elapsed_s": 0.0, "event": "task_claimed", "task_id": "T-1"},
            {"ts": 100.5, "elapsed_s": 0.5, "event": "task_completed", "task_id": "T-1"},
        ],
    )
    _write_replay(
        b,
        [
            {"ts": 999.0, "elapsed_s": 0.0, "event": "task_claimed", "task_id": "T-1"},
            {"ts": 1003.25, "elapsed_s": 4.25, "event": "task_completed", "task_id": "T-1"},
        ],
    )

    assert compute_replay_fingerprint(a) == compute_replay_fingerprint(b)


def test_fingerprint_changes_on_decision_divergence(tmp_path: Path) -> None:
    """A different decision payload changes the fingerprint."""
    a = tmp_path / "a" / "replay.jsonl"
    b = tmp_path / "b" / "replay.jsonl"
    _write_replay(
        a,
        [{"ts": 1.0, "elapsed_s": 0.0, "event": "task_completed", "task_id": "T-1", "result": "ok"}],
    )
    _write_replay(
        b,
        [{"ts": 1.0, "elapsed_s": 0.0, "event": "task_completed", "task_id": "T-1", "result": "FAIL"}],
    )

    assert compute_replay_fingerprint(a) != compute_replay_fingerprint(b)


def test_fingerprint_changes_on_event_reorder(tmp_path: Path) -> None:
    """Reordering events changes the fingerprint (order is a decision)."""
    a = tmp_path / "a" / "replay.jsonl"
    b = tmp_path / "b" / "replay.jsonl"
    rows = [
        {"ts": 1.0, "elapsed_s": 0.0, "event": "task_claimed", "task_id": "T-1"},
        {"ts": 2.0, "elapsed_s": 1.0, "event": "task_claimed", "task_id": "T-2"},
    ]
    _write_replay(a, rows)
    _write_replay(b, list(reversed(rows)))

    assert compute_replay_fingerprint(a) != compute_replay_fingerprint(b)


def test_fingerprint_changes_on_event_type_change(tmp_path: Path) -> None:
    """A changed ``event`` type changes the fingerprint."""
    a = tmp_path / "a" / "replay.jsonl"
    b = tmp_path / "b" / "replay.jsonl"
    _write_replay(a, [{"ts": 1.0, "elapsed_s": 0.0, "event": "task_claimed", "task_id": "T-1"}])
    _write_replay(b, [{"ts": 1.0, "elapsed_s": 0.0, "event": "task_aborted", "task_id": "T-1"}])

    assert compute_replay_fingerprint(a) != compute_replay_fingerprint(b)


def test_fingerprint_insensitive_to_key_order(tmp_path: Path) -> None:
    """Canonicalisation makes incidental key order irrelevant."""
    a = tmp_path / "a" / "replay.jsonl"
    b = tmp_path / "b" / "replay.jsonl"
    _write_replay(a, [{"event": "task_completed", "task_id": "T-1", "result": "ok", "ts": 1.0}])
    _write_replay(b, [{"result": "ok", "ts": 1.0, "task_id": "T-1", "event": "task_completed"}])

    assert compute_replay_fingerprint(a) == compute_replay_fingerprint(b)


def test_journal_fingerprint_is_the_merkle_head(tmp_path: Path) -> None:
    """EventJournal.fingerprint returns the chain head (the run identity)."""
    rec = EventJournal(run_id="run-x", sdd_dir=tmp_path)
    rec.record("task_claimed", task_id="T-1")
    rec.record("task_completed", task_id="T-1")

    assert rec.fingerprint() == rec.head()
    assert rec.fingerprint() != ""


def test_journal_two_runs_same_decisions_same_head(tmp_path: Path) -> None:
    """Two journals with identical decisions but real wall-clock differences
    chain to the same head, so the head is a cross-run determinism proof."""
    rec_a = EventJournal(run_id="run-a", sdd_dir=tmp_path)
    rec_a.record("task_claimed", task_id="T-1", agent_id="backend")
    rec_a.record("task_completed", task_id="T-1", files=["src/a.py"])

    rec_b = EventJournal(run_id="run-b", sdd_dir=tmp_path)
    rec_b.record("task_claimed", task_id="T-1", agent_id="backend")
    rec_b.record("task_completed", task_id="T-1", files=["src/a.py"])

    assert rec_a.fingerprint() == rec_b.fingerprint()
    assert rec_a.fingerprint() != ""
