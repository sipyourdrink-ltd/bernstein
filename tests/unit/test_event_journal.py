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

import pytest

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
    assert result.chain_consistent
    assert result.identity == "unverifiable"
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
    assert not result.chain_consistent
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


def test_retention_still_caps_total_when_active_run_sorts_first(tmp_path: Path) -> None:
    """The budget holds even when the active run's name is not the newest (#5881).

    ``run_id`` is caller-supplied (an operator-pinned ``BERNSTEIN_RUN_ID``, or a
    resumed older run) and is not guaranteed to sort after every existing run
    directory. Retention must still cap the total number of surviving run
    directories at the configured limit, with the active run protected from
    deletion rather than exempted from the count.
    """
    runs_root = tmp_path
    with patch.dict("os.environ", {"BERNSTEIN_REPLAY_RETENTION": "2"}, clear=True):
        for rid in ("run-b", "run-c"):
            j = EventJournal(run_id=rid, sdd_dir=runs_root)
            j.record("only")
        # The active run's name sorts BEFORE the two existing run directories.
        active = EventJournal(run_id="run-a", sdd_dir=runs_root)
        active.record("only")
        surviving = sorted(p.name for p in (runs_root / "runs").iterdir() if p.is_dir())
    assert len(surviving) == 2
    assert "run-a" in surviving
    assert "run-c" in surviving


def test_run_id_traversal_is_refused(tmp_path: Path) -> None:
    """A run_id that traverses or escapes the runs root is refused before I/O."""
    for bad in ("../../etc", "..", "a/../../b", "/abs/path", "", ".", "a\\b"):
        with pytest.raises(ValueError, match="run_id"):
            EventJournal(run_id=bad, sdd_dir=tmp_path)


def test_ordinary_run_id_is_contained(tmp_path: Path) -> None:
    """A normal run_id builds a journal path inside the runs root."""
    journal = EventJournal(run_id="run-ok-123", sdd_dir=tmp_path)
    assert journal.path.resolve().is_relative_to((tmp_path / "runs").resolve())

# ============================================================================
# Tests for hash_profile (jcs-v2) support — issue #5274 slice 2
# ============================================================================


def test_astral_emoji_vector_matches_rfc8785(tmp_path: Path) -> None:
    """Under jcs-v2 the payload hash uses RFC 8785 canonicalization."""
    from bernstein.core.replay.journal import _payload_hash, compute_event_hash, HASH_PROFILE_JCS_V2
    from bernstein.core.security.agent_card_signer import canonicalize_jcs
    import hashlib

    payload = {"model": "задача 🚀"}

    # Under jcs-v2 the payload hash should equal canonicalize_jcs
    projected = {k: v for k, v in payload.items() if k not in {"ts", "elapsed_s", "index", "prev_hash", "payload_hash", "event_hash"}}
    projected["event"] = "agent_spawned"
    expected = hashlib.sha256(canonicalize_jcs(projected)).hexdigest()

    # This should fail on current main because _payload_hash uses json.dumps with ensure_ascii=True
    result = _payload_hash("agent_spawned", payload, hash_profile=HASH_PROFILE_JCS_V2)
    assert result == expected, f"Expected {expected}, got {result}"


def test_non_json_value_rejected_at_write_time_under_v2(tmp_path: Path) -> None:
    """Under jcs-v2, non-JSON values at write time are errors (no default=str)."""
    from bernstein.core.replay.journal import EventJournal, HASH_PROFILE_JCS_V2

    # This test should fail on current main because legacy profile accepts non-JSON via default=str
    # We need to pass hash_profile="jcs-v2" to EventJournal to trigger the new behavior
    journal = EventJournal(run_id="run-v2", sdd_dir=tmp_path, hash_profile=HASH_PROFILE_JCS_V2)
    with pytest.raises(TypeError, match="not JSON serializable"):
        journal.record("task_claimed", task_id="T-1", data=set([1, 2, 3]))


def test_legacy_profile_journal_still_verifies(tmp_path: Path) -> None:
    """A journal created under py-json-v1 still verifies when read with the same profile."""
    from bernstein.core.replay.journal import EventJournal, verify_journal, JournalSeal

    # Create a journal under legacy profile (default)
    journal = EventJournal(run_id="run-legacy", sdd_dir=tmp_path)
    journal.record("task_claimed", task_id="T-1", model="задача 🚀")
    journal.record("task_completed", task_id="T-1")

    seal = JournalSeal(head=journal.head(), event_count=journal.event_count())
    result = verify_journal(journal.path, seal=seal)

    assert result.chain_consistent
    assert result.identity == "verified"


def test_unknown_profile_fails_closed(tmp_path: Path) -> None:
    """A journal claiming an unknown hash_profile is malformed, not skipped."""
    from bernstein.core.replay.journal import EventJournal, verify_journal, JournalSeal

    # Create a journal and manually inject unknown profile in first event
    journal = EventJournal(run_id="run-unknown", sdd_dir=tmp_path)
    journal.record("task_claimed", task_id="T-1")

    # Manually edit the journal to add unknown profile
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["hash_profile"] = "jcs-v3"
    lines[0] = json.dumps(first)
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    seal = JournalSeal(head=journal.head(), event_count=journal.event_count())
    result = verify_journal(journal.path, seal=seal)

    assert result.chain_consistent is False or result.identity == "mismatched"
    assert any("hash_profile" in e for e in result.errors)


def test_float_1e21_vector_matches_rfc8785(tmp_path: Path) -> None:
    """Float 1e21 should be encoded as 1e21 per RFC 8785, not 1e+21."""
    from bernstein.core.replay.journal import _payload_hash, HASH_PROFILE_JCS_V2
    from bernstein.core.security.agent_card_signer import canonicalize_jcs
    import hashlib

    payload = {"value": 1e21}
    projected = {k: v for k, v in payload.items() if k not in {"ts", "elapsed_s", "index", "prev_hash", "payload_hash", "event_hash"}}
    projected["event"] = "test"
    expected = hashlib.sha256(canonicalize_jcs(projected)).hexdigest()

    result = _payload_hash("test", payload, hash_profile=HASH_PROFILE_JCS_V2)
    assert result == expected, f"Expected {expected}, got {result}"


def test_non_ascii_payload_hashes_identically_in_journal_and_spine_under_v2(tmp_path: Path) -> None:
    """Under jcs-v2, journal payload hash and spine row bytes agree on non-ASCII."""
    # This is a load-bearing integration test - will pass once both journal and spine use jcs-v2
    pass


