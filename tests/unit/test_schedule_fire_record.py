"""Unit tests for recording and verifying a fire as a projection (#2302).

Each recurring-goal fire records ``{schedule_id, fire_time,
last_state_hash, graph_hash}`` into the run event journal (AC3) and seals
the canonical graph bytes into the lineage spine; ``schedule verify``
replays a past fire and confirms the graph hash reproduces (AC4). These
tests isolate all state under ``tmp_path`` and pin the audit HMAC key so
nothing depends on the host XDG state dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.orchestration.schedule_fire_record import (
    JOURNAL_EVENT,
    fire_run_id,
    load_fire_records,
    record_fire,
    verify_all_fires,
    verify_fire,
)
from bernstein.core.replay.journal import load_events


@pytest.fixture
def sdd_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated ``.sdd`` root with a pinned audit key (no host state)."""
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"deterministic-fire-record-key-32b")
    key_path.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
    sdd = tmp_path / "sdd"
    sdd.mkdir()
    return sdd


class TestRecordFire:
    def test_journal_row_carries_required_fields(self, sdd_dir: Path) -> None:
        """AC3: each fire records graph_hash (and the projection inputs)
        in the journal.
        """
        rec = record_fire(
            sdd_dir=sdd_dir,
            schedule_id="sched_a",
            fire_time=1_700_000_000,
            goal="digest",
            recurrence="0 9 * * *",
        )
        run_dir = sdd_dir / "runs" / fire_run_id("sched_a", 1_700_000_000)
        events = [e for e in load_events(run_dir / "journal.jsonl") if e.get("event") == JOURNAL_EVENT]
        assert len(events) == 1
        row = events[0]
        assert row["schedule_id"] == "sched_a"
        assert row["fire_time"] == 1_700_000_000
        assert row["graph_hash"] == rec.graph_hash
        assert row["last_state_hash"] == "genesis"

    def test_spine_entry_sealed(self, sdd_dir: Path) -> None:
        rec = record_fire(
            sdd_dir=sdd_dir,
            schedule_id="sched_a",
            fire_time=1_700_000_000,
            goal="digest",
        )
        assert rec.spine_entry_hash.startswith("sha256:")
        spine = sdd_dir / "lineage" / fire_run_id("sched_a", 1_700_000_000) / "spine.jsonl"
        assert spine.exists()

    def test_two_operators_identical_state_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two operators with identical state produce identical graph
        hashes and byte-identical sealed graph bytes.
        """
        key_path = tmp_path / "k"
        key_path.write_bytes(b"deterministic-fire-record-key-32b")
        key_path.chmod(0o600)
        monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))

        a_root = tmp_path / "a"
        b_root = tmp_path / "b"
        a_root.mkdir()
        b_root.mkdir()
        rec_a = record_fire(sdd_dir=a_root, schedule_id="s", fire_time=42, goal="g", recurrence="FREQ=DAILY")
        rec_b = record_fire(sdd_dir=b_root, schedule_id="s", fire_time=42, goal="g", recurrence="FREQ=DAILY")
        assert rec_a.graph_hash == rec_b.graph_hash
        # The sealed graph content is anchored by hash in the spine entry;
        # identical state produces the byte-identical spine entry hash.
        assert rec_a.spine_entry_hash == rec_b.spine_entry_hash

    def test_trigger_fire_records_input_hash(self, sdd_dir: Path) -> None:
        """AC5: a webhook-triggered fire records the trigger event input
        hash in the projection and the journal row.
        """
        rec = record_fire(
            sdd_dir=sdd_dir,
            schedule_id="sched_hook",
            fire_time=1_700_000_500,
            goal="on push",
            trigger_event=b'{"ref": "refs/heads/main"}',
        )
        assert rec.trigger_input_hash.startswith("sha256:")
        run_dir = sdd_dir / "runs" / fire_run_id("sched_hook", 1_700_000_500)
        row = next(e for e in load_events(run_dir / "journal.jsonl") if e.get("event") == JOURNAL_EVENT)
        assert row["trigger_input_hash"] == rec.trigger_input_hash


class TestVerifyFire:
    def test_replayed_fire_matches(self, sdd_dir: Path) -> None:
        """AC4: schedule verify replays a past fire and confirms graph
        hash equality.
        """
        rec = record_fire(
            sdd_dir=sdd_dir,
            schedule_id="sched_a",
            fire_time=1_700_000_000,
            goal="digest",
            recurrence="0 9 * * *",
        )
        results = verify_all_fires(sdd_dir)
        assert len(results) == 1
        assert results[0].match
        assert results[0].recorded_graph_hash == rec.graph_hash
        assert results[0].recomputed_graph_hash == rec.graph_hash

    def test_tampered_graph_hash_detected(self, sdd_dir: Path) -> None:
        """A journal row whose graph_hash was edited to a wrong value must
        be reported as a mismatch, not printed as intact.
        """
        record_fire(sdd_dir=sdd_dir, schedule_id="sched_a", fire_time=1_700_000_000, goal="digest")
        rows = load_fire_records(sdd_dir)
        result = verify_fire(
            schedule_id=rows[0]["schedule_id"],
            fire_time=int(rows[0]["fire_time"]),
            recorded_graph_hash="deadbeef" * 8,
            goal=str(rows[0].get("goal", "")),
            scenario_id=str(rows[0].get("scenario_id", "")),
            recurrence=str(rows[0].get("recurrence", "")),
        )
        assert not result.match
        assert "mismatch" in result.reason

    def test_trigger_fire_replays_from_recorded_hash(self, sdd_dir: Path) -> None:
        """A trigger fire replays using the recorded trigger_input_hash
        (raw event bytes are not retained) and still matches.
        """
        record_fire(
            sdd_dir=sdd_dir,
            schedule_id="sched_hook",
            fire_time=1_700_000_500,
            goal="on push",
            trigger_event=b"payload",
        )
        results = verify_all_fires(sdd_dir)
        assert len(results) == 1
        assert results[0].match

    def test_no_fires_empty(self, sdd_dir: Path) -> None:
        assert verify_all_fires(sdd_dir) == []
