"""Tests for :mod:`bernstein.core.persistence.work_ledger` (#2358).

The durable work ledger is the resumable-state substrate: a hash-chained
JSONL record of the task graph and every state transition. These tests pin:

* The canonical entry-hash contract (hand-derivable from the docstring).
* Fail-closed recovery (torn tail tolerated, interior corruption refused).
* Tamper detection naming the exact entry position.
* DLP redaction on the write path (secrets never become portable).
* Deterministic replay projection (byte-identical state for the same chain).
* Divergence detection between two chains sharing a common prefix.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bernstein.core.persistence.work_ledger import (
    GENESIS_HASH,
    KIND_RUN_OPEN,
    KIND_RUN_RESUMED,
    KIND_TASK_COMPLETED,
    KIND_TASK_FAILED,
    KIND_TASK_SCHEDULED,
    KIND_TASK_STARTED,
    LedgerEntry,
    LedgerError,
    LedgerReader,
    WorkLedger,
    compare_chains,
    compute_entry_hash,
    replay_state,
    run_ledger_dir,
)


def _record_run(ledger: WorkLedger) -> None:
    """Record a small three-task run: two completed, one in flight."""
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": "run-a"})
    for task in ("t1", "t2", "t3"):
        ledger.append(kind=KIND_TASK_SCHEDULED, task_id=task)
    ledger.append(kind=KIND_TASK_STARTED, task_id="t1")
    ledger.append(kind=KIND_TASK_COMPLETED, task_id="t1", payload={"commit": "abc123"})
    ledger.append(kind=KIND_TASK_STARTED, task_id="t2")
    ledger.append(kind=KIND_TASK_COMPLETED, task_id="t2")
    ledger.append(kind=KIND_TASK_STARTED, task_id="t3")


class TestCanonicalContract:
    def test_entry_hash_is_hand_derivable(self) -> None:
        """A third party can re-derive the entry hash from the documented contract."""
        document = {
            "kind": KIND_TASK_STARTED,
            "payload": {"attempt": 1},
            "prev_hash": GENESIS_HASH,
            "task_id": "t1",
        }
        expected = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        actual = compute_entry_hash(
            prev_hash=GENESIS_HASH,
            kind=KIND_TASK_STARTED,
            task_id="t1",
            payload={"attempt": 1},
        )
        assert actual == expected

    def test_append_links_predecessor(self, tmp_path: Path) -> None:
        ledger = WorkLedger.open(tmp_path / "ledger")
        first = ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": "r"})
        second = ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
        assert first.prev_hash == GENESIS_HASH
        assert second.prev_hash == first.entry_hash
        assert ledger.head_hash == second.entry_hash

    def test_kind_pattern_enforced(self, tmp_path: Path) -> None:
        ledger = WorkLedger.open(tmp_path / "ledger")
        with pytest.raises(LedgerError):
            ledger.append(kind="Not A Kind!", task_id="t1")

    def test_ts_is_metadata_never_hashed(self, tmp_path: Path) -> None:
        ledger = WorkLedger.open(tmp_path / "ledger")
        entry = ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
        row = entry.to_dict()
        row["ts"] = 0.0
        rehashed = compute_entry_hash(
            prev_hash=row["prev_hash"],
            kind=row["kind"],
            task_id=row["task_id"],
            payload=row["payload"],
        )
        assert rehashed == entry.entry_hash


class TestRecoveryFailClosed:
    def test_reopen_recovers_head_and_seq(self, tmp_path: Path) -> None:
        ledger_dir = tmp_path / "ledger"
        ledger = WorkLedger.open(ledger_dir)
        _record_run(ledger)
        head = ledger.head_hash
        seq = ledger.next_seq
        ledger.close()

        reopened = WorkLedger.open(ledger_dir)
        assert reopened.head_hash == head
        assert reopened.next_seq == seq

    def test_torn_tail_line_is_tolerated(self, tmp_path: Path) -> None:
        """A crash mid-write leaves a torn line; recovery drops it."""
        ledger_dir = tmp_path / "ledger"
        ledger = WorkLedger.open(ledger_dir)
        _record_run(ledger)
        head = ledger.head_hash
        ledger.close()

        with ledger_dir.joinpath("000000.jsonl").open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 99, "prev_hash": "dead')  # torn mid-write

        reopened = WorkLedger.open(ledger_dir)
        assert reopened.head_hash == head

    def test_interior_corruption_refused(self, tmp_path: Path) -> None:
        ledger_dir = tmp_path / "ledger"
        ledger = WorkLedger.open(ledger_dir)
        _record_run(ledger)
        ledger.close()

        bucket = ledger_dir / "000000.jsonl"
        lines = bucket.read_text(encoding="utf-8").splitlines()
        lines[2] = "not-json-at-all"
        bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with pytest.raises(LedgerError):
            WorkLedger.open(ledger_dir)


class TestTamperDetection:
    def test_tampered_entry_names_exact_position(self, tmp_path: Path) -> None:
        """AC: a tampered ledger entry fails verification with the exact position."""
        ledger_dir = tmp_path / "ledger"
        ledger = WorkLedger.open(ledger_dir)
        _record_run(ledger)
        ledger.close()

        bucket = ledger_dir / "000000.jsonl"
        lines = bucket.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[4])
        row["task_id"] = "t9"  # flip a hashed field in entry seq=4
        lines[4] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = LedgerReader(ledger_dir).verify()
        assert not result.ok
        joined = "\n".join(result.errors)
        assert "entry 4" in joined
        assert "line 5" in joined

    def test_rechained_suffix_surfaces_as_head_mismatch(self, tmp_path: Path) -> None:
        """Rewriting an entry and re-linking the suffix moves the head hash."""
        ledger_dir = tmp_path / "ledger"
        ledger = WorkLedger.open(ledger_dir)
        _record_run(ledger)
        honest_head = ledger.head_hash
        ledger.close()

        bucket = ledger_dir / "000000.jsonl"
        rows = [json.loads(line) for line in bucket.read_text(encoding="utf-8").splitlines()]
        rows[4]["task_id"] = "t9"
        prev = rows[3]["entry_hash"]
        for row in rows[4:]:
            row["prev_hash"] = prev
            row["entry_hash"] = compute_entry_hash(
                prev_hash=prev,
                kind=row["kind"],
                task_id=row["task_id"],
                payload=row["payload"],
            )
            prev = row["entry_hash"]
        bucket.write_text(
            "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows) + "\n",
            encoding="utf-8",
        )

        result = LedgerReader(ledger_dir).verify(expected_head=honest_head)
        assert not result.ok
        assert any("head mismatch" in err for err in result.errors)

    def test_verify_ok_on_honest_chain(self, tmp_path: Path) -> None:
        ledger_dir = tmp_path / "ledger"
        ledger = WorkLedger.open(ledger_dir)
        _record_run(ledger)
        result = LedgerReader(ledger_dir).verify(expected_head=ledger.head_hash)
        assert result.ok
        assert result.entries == 9
        assert result.errors == []


class TestRedactionOnWritePath:
    def test_secret_payload_never_reaches_disk(self, tmp_path: Path) -> None:
        """DLP runs before any entry is written, so secrets never become portable."""
        secret = "sk-proj-abcdef1234567890ABCDEF1234567890abcdef12"
        ledger_dir = tmp_path / "ledger"
        ledger = WorkLedger.open(ledger_dir)
        entry = ledger.append(
            kind=KIND_TASK_COMPLETED,
            task_id="t1",
            payload={"note": f"api_key={secret}", "nested": {"deep": [f"token: {secret}"]}},
        )
        raw = ledger_dir.joinpath("000000.jsonl").read_text(encoding="utf-8")
        assert secret not in raw
        assert entry.redactions >= 2

    def test_redacted_chain_still_verifies(self, tmp_path: Path) -> None:
        """The hash is computed over the redacted payload, so the chain verifies."""
        secret = "sk-proj-abcdef1234567890ABCDEF1234567890abcdef12"
        ledger_dir = tmp_path / "ledger"
        ledger = WorkLedger.open(ledger_dir)
        ledger.append(kind=KIND_TASK_STARTED, task_id="t1", payload={"env": f"API_KEY={secret}"})
        result = LedgerReader(ledger_dir).verify(expected_head=ledger.head_hash)
        assert result.ok


class TestReplayProjection:
    def test_replay_rebuilds_task_states(self, tmp_path: Path) -> None:
        ledger_dir = tmp_path / "ledger"
        ledger = WorkLedger.open(ledger_dir)
        _record_run(ledger)

        state = replay_state(LedgerReader(ledger_dir).entries(), run_id="run-a")
        assert state.completed_tasks == ["t1", "t2"]
        assert state.in_flight_tasks == ["t3"]
        assert state.scheduled_tasks == []
        assert state.resume_frontier() == ["t3"]
        assert state.head_hash == ledger.head_hash
        assert state.tasks["t1"].attempts == 1

    def test_failed_task_projected(self, tmp_path: Path) -> None:
        ledger_dir = tmp_path / "ledger"
        ledger = WorkLedger.open(ledger_dir)
        ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
        ledger.append(kind=KIND_TASK_STARTED, task_id="t1")
        ledger.append(kind=KIND_TASK_FAILED, task_id="t1", payload={"reason": "timeout"})
        state = replay_state(LedgerReader(ledger_dir).entries())
        assert state.failed_tasks == ["t1"]
        assert state.resume_frontier() == []

    def test_projection_is_deterministic(self, tmp_path: Path) -> None:
        """Two replays of the same chain produce byte-identical state JSON."""
        ledger_dir = tmp_path / "ledger"
        ledger = WorkLedger.open(ledger_dir)
        _record_run(ledger)
        first = replay_state(LedgerReader(ledger_dir).entries(), run_id="run-a")
        second = replay_state(LedgerReader(ledger_dir).entries(), run_id="run-a")
        assert first.to_canonical_json() == second.to_canonical_json()

    def test_unknown_kind_carried_without_breaking_replay(self, tmp_path: Path) -> None:
        """Forward compat: a ledger written by a newer writer still replays."""
        ledger_dir = tmp_path / "ledger"
        ledger = WorkLedger.open(ledger_dir)
        ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
        ledger.append(kind="daemon.heartbeat", payload={"tick": 1})
        state = replay_state(LedgerReader(ledger_dir).entries())
        assert state.scheduled_tasks == ["t1"]
        assert state.unknown_kinds == 1

    def test_resume_entry_counted(self, tmp_path: Path) -> None:
        ledger_dir = tmp_path / "ledger"
        ledger = WorkLedger.open(ledger_dir)
        ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": "r"})
        ledger.append(kind=KIND_RUN_RESUMED, payload={"from_head": "aa", "nonce": "bb"})
        state = replay_state(LedgerReader(ledger_dir).entries())
        assert state.resumes == 1


class TestDivergence:
    def _chain(self, base: Path, name: str, extra: list[str]) -> list[LedgerEntry]:
        ledger = WorkLedger.open(run_ledger_dir(base / ".sdd", name))
        ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": "shared"})
        ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
        for task in extra:
            ledger.append(kind=KIND_TASK_STARTED, task_id=task)
        return list(LedgerReader(ledger.ledger_dir).entries())

    def test_identical_chains(self, tmp_path: Path) -> None:
        a = self._chain(tmp_path / "a", "run", [])
        b = self._chain(tmp_path / "b", "run", [])
        relation = compare_chains(a, b)
        assert relation.relation == "identical"
        assert relation.fork_seq is None

    def test_remote_ahead_is_fast_forward(self, tmp_path: Path) -> None:
        a = self._chain(tmp_path / "a", "run", [])
        b = self._chain(tmp_path / "b", "run", ["t1"])
        relation = compare_chains(a, b)
        assert relation.relation == "remote-ahead"

    def test_local_ahead(self, tmp_path: Path) -> None:
        a = self._chain(tmp_path / "a", "run", ["t1"])
        b = self._chain(tmp_path / "b", "run", [])
        relation = compare_chains(a, b)
        assert relation.relation == "local-ahead"

    def test_two_heads_same_parent_is_divergence(self, tmp_path: Path) -> None:
        """AC: two divergent resumes of the same ledger are an explicit error."""
        a = self._chain(tmp_path / "a", "run", ["t1"])
        b = self._chain(tmp_path / "b", "run", ["t2"])
        relation = compare_chains(a, b)
        assert relation.relation == "diverged"
        assert relation.fork_seq == 2
        assert relation.local_head != relation.remote_head
