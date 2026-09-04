"""Tests for the govern-discover observation store (#5083).

Each test is named for the property it protects; the load-bearing one is
``test_two_overlapping_passes_converge_to_no_duplicates``: two passes over
the same fixture must leave one record per entity, or existence stops
meaning "re-observed recently".
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from bernstein.core.govern.observation import ObservationEnvelope
from bernstein.core.govern.observation_store import ObservationStore, RecordState
from bernstein.core.security.path_containment import PathContainmentError

FIRST_SEEN = "2026-09-03T09:00:00Z"  # 3h before SWEEP_AT
SECOND_SEEN = "2026-09-03T10:30:00Z"  # 1.5h before SWEEP_AT
SWEEP_AT = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _envelope(*, observed_at: str = FIRST_SEEN) -> ObservationEnvelope:
    return ObservationEnvelope(
        entity_id="entity:" + "a1b2c3d4" * 4,
        entity_class="host",
        payload={"hostname": "web-1"},
        observed_at=observed_at,
        evidence_ref="discover-pass-7",
        errors={"mcp.config": "permission denied reading ~/.cursor/mcp.json"},
    )


def test_two_overlapping_passes_converge_to_no_duplicates(tmp_path: Path) -> None:
    # Load-bearing (#5083): the static Inventory appends one entry per
    # observation; the store upserts under the stable entity id instead.
    store = ObservationStore(tmp_path)
    assert store.upsert(_envelope(observed_at=FIRST_SEEN)) == "created"
    assert store.upsert(_envelope(observed_at=SECOND_SEEN)) == "refreshed"
    repeat = ObservationEnvelope.from_dict(_envelope(observed_at=SECOND_SEEN).to_dict())
    before = store.entity_path(repeat.entity_id).read_bytes()
    assert store.upsert(repeat) == "refreshed"

    assert store.entity_ids() == (repeat.entity_id,)
    record = store.load(repeat.entity_id)
    assert record.envelope == repeat
    assert record.state is RecordState.LIVE
    # A repeated pass is not churn: same bytes on disk, no journal noise.
    assert store.entity_path(repeat.entity_id).read_bytes() == before
    assert store.journal() == ()


@pytest.mark.parametrize(
    ("observed_at", "ttl_seconds", "moved", "state"),
    [
        (SECOND_SEEN, 7200, 0, RecordState.LIVE),  # 1.5h old under a 2h TTL: fresh
        (FIRST_SEEN, 3600, 1, RecordState.TOMBSTONED),  # 3h old under a 1h TTL: stale
        ("not a timestamp", 3600, 1, RecordState.TOMBSTONED),  # unparseable: fail closed
    ],
)
def test_sweep_partitions_on_age(
    tmp_path: Path, observed_at: str, ttl_seconds: float, moved: int, state: RecordState
) -> None:
    store = ObservationStore(tmp_path)
    entity_id = _envelope().entity_id
    store.upsert(_envelope(observed_at=observed_at))

    assert store.sweep(ttl_seconds=ttl_seconds, now=SWEEP_AT) == moved
    assert store.load(entity_id).state is state


def test_sweep_moves_stale_entity_to_tombstone_and_journals_it(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path)
    entity_id = _envelope().entity_id
    store.upsert(_envelope(observed_at=FIRST_SEEN))

    assert store.sweep(ttl_seconds=3600, now=SWEEP_AT) == 1

    (entry,) = store.journal()
    assert entry.entity_id == entity_id
    assert entry.transition == "tombstone"
    assert FIRST_SEEN in entry.reason
    # A second sweep moving nothing journals nothing.
    assert store.sweep(ttl_seconds=3600, now=SWEEP_AT) == 0
    assert len(store.journal()) == 1


def test_tombstone_never_hard_deletes(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path)
    entity_id = _envelope().entity_id
    store.upsert(_envelope(observed_at=FIRST_SEEN))
    store.sweep(ttl_seconds=3600, now=SWEEP_AT)

    record = store.load(entity_id)

    assert record.state is RecordState.TOMBSTONED
    assert record.envelope.payload["hostname"] == "web-1"
    assert store.entity_path(entity_id).is_file()


def test_reappearing_entity_is_restored_and_journaled(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path)
    entity_id = _envelope().entity_id
    store.upsert(_envelope(observed_at=FIRST_SEEN))
    store.sweep(ttl_seconds=3600, now=SWEEP_AT)

    assert store.upsert(_envelope(observed_at="2026-09-03T12:30:00Z")) == "restored"

    record = store.load(entity_id)
    assert record.state is RecordState.LIVE
    assert record.envelope.observed_at == "2026-09-03T12:30:00Z"
    assert [e.transition for e in store.journal()] == ["tombstone", "restore"]


def test_journal_refuses_a_planted_symlink(tmp_path: Path) -> None:
    # The containment barrier the repo pins in
    # test_path_containment.test_no_unrouted_journal_path_construction: a
    # journal.jsonl symlinked outside the store root must not be read.
    store = ObservationStore(tmp_path)
    elsewhere = tmp_path.parent / "observation-journal-elsewhere"
    elsewhere.mkdir(exist_ok=True)
    planted = elsewhere / "journal.jsonl"
    planted.write_text(
        '{"entity_id":"entity:deadbeef","reason":"planted","swept_at":"2026-01-01T00:00:00Z","transition":"tombstone"}\n',
        encoding="utf-8",
    )
    try:
        (tmp_path / "journal.jsonl").symlink_to(planted)
    except OSError:  # pragma: no cover - platform dependent
        pytest.skip("cannot create symlinks on this platform")

    with pytest.raises(PathContainmentError):
        store.journal()
