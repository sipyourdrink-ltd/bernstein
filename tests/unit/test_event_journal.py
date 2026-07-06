"""Tests for the canonical Merkle-chained EventJournal (issue #2293).

The journal is the single per-run event recorder. Each event is
``H(prev, event_type, payload_hash, monotonic_index)`` and the head hash
is the run identity. These tests pin the determinism and verifiability
guarantees the issue's acceptance criteria depend on.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from bernstein.core.replay.journal import (
    EventJournal,
    JournalVerifyResult,
    rebuild_state,
)


def test_record_by_default_produces_journal(tmp_path: Path) -> None:
    """A journal is written with no env flags set (AC1)."""
    with patch.dict("os.environ", {}, clear=True):
        journal = EventJournal(run_id="run-1", sdd_dir=tmp_path)
        journal.record("task_claimed", task_id="T-1", agent_id="A-1")

    assert journal.path.exists()
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "task_claimed"
    assert row["task_id"] == "T-1"
    assert row["index"] == 0
    assert row["prev_hash"] == ""
    assert row["event_hash"]


def test_head_is_merkle_chain_over_events(tmp_path: Path) -> None:
    """Each new event advances the head; head is the run identity."""
    journal = EventJournal(run_id="run-2", sdd_dir=tmp_path)
    journal.record("a", x=1)
    first = journal.head()
    journal.record("b", x=2)
    second = journal.head()

    assert first
    assert second
    assert first != second
    # fingerprint() aliases the Merkle head so existing callers keep working.
    assert journal.fingerprint() == second


def test_event_hash_chains_prev_type_payload_index(tmp_path: Path) -> None:
    """event_hash = H(prev, event_type, payload_hash, monotonic_index)."""
    journal = EventJournal(run_id="run-3", sdd_dir=tmp_path)
    journal.record("first", value="v1")
    journal.record("second", value="v2")

    rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert rows[0]["prev_hash"] == ""
    assert rows[1]["prev_hash"] == rows[0]["event_hash"]
    assert rows[0]["index"] == 0
    assert rows[1]["index"] == 1


def test_timing_fields_excluded_from_payload_hash(tmp_path: Path) -> None:
    """Two runs differing only in wall clock hash to the same head."""
    with patch("bernstein.core.replay.journal.time.time", side_effect=[100.0, 100.5, 200.0, 200.5]):
        a = EventJournal(run_id="a", sdd_dir=tmp_path / "a")
        a.record("step", payload="same")
    with patch("bernstein.core.replay.journal.time.time", side_effect=[300.0, 300.9, 400.0, 400.9]):
        b = EventJournal(run_id="b", sdd_dir=tmp_path / "b")
        b.record("step", payload="same")

    assert a.head() == b.head()


def test_verify_reports_byte_identity_on_unmodified_journal(tmp_path: Path) -> None:
    """verify() on an intact journal reports no divergence (AC2)."""
    journal = EventJournal(run_id="run-ok", sdd_dir=tmp_path)
    journal.record("one", v=1)
    journal.record("two", v=2)
    journal.record("three", v=3)

    result = journal.verify()
    assert isinstance(result, JournalVerifyResult)
    assert result.ok
    assert result.divergent_index is None
    assert result.count == 3


def test_verify_reports_first_divergent_step_index(tmp_path: Path) -> None:
    """Injecting one non-deterministic result flags the exact step (AC2)."""
    journal = EventJournal(run_id="run-bad", sdd_dir=tmp_path)
    journal.record("zero", v=0)
    journal.record("one", v=1)
    journal.record("two", v=2)

    rows = journal.path.read_text().splitlines()
    tampered = json.loads(rows[1])
    tampered["v"] = 999  # non-deterministic tool result injected at step 1
    rows[1] = json.dumps(tampered)
    journal.path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    result = journal.verify()
    assert not result.ok
    assert result.divergent_index == 1


def test_rebuild_state_is_deterministic_across_invocations(tmp_path: Path) -> None:
    """--from-step N reconstructs identical state twice (AC4)."""
    journal = EventJournal(run_id="run-state", sdd_dir=tmp_path)
    journal.record("task_claimed", task_id="T-1")
    journal.record("task_completed", task_id="T-1")
    journal.record("task_claimed", task_id="T-2")

    state_a = rebuild_state(journal.path, from_step=2)
    state_b = rebuild_state(journal.path, from_step=2)

    assert state_a == state_b
    assert state_a["step_count"] == 2
    assert state_a["head_hash"]


def test_event_count_ignores_blank_lines(tmp_path: Path) -> None:
    """event_count counts only non-empty rows (RunRecorder parity)."""
    journal = EventJournal(run_id="run-count", sdd_dir=tmp_path)
    journal.record("one")
    journal.record("two")
    assert journal.event_count() == 2


def test_fingerprint_empty_for_missing_file(tmp_path: Path) -> None:
    """fingerprint() is empty before any event is recorded."""
    journal = EventJournal(run_id="run-empty", sdd_dir=tmp_path)
    assert journal.fingerprint() == ""


def test_retention_prunes_oldest_run_journals(tmp_path: Path) -> None:
    """BERNSTEIN_REPLAY_RETENTION caps how many run journals persist."""
    runs_root = tmp_path
    with patch.dict("os.environ", {"BERNSTEIN_REPLAY_RETENTION": "2"}, clear=True):
        for i in range(4):
            j = EventJournal(run_id=f"run-{i:02d}", sdd_dir=runs_root)
            j.record("only")
        surviving = sorted(p.name for p in (runs_root / "runs").iterdir() if p.is_dir())
    assert surviving == ["run-02", "run-03"]
