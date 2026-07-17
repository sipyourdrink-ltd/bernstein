"""Unit tests for transitive cache eviction over served-from edges (AC5)."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.persistence.cache_eviction import (
    ServedFromEdge,
    ServedFromLedger,
    TombstoneStore,
)


def _seed_ledger(ledger: ServedFromLedger) -> None:
    # key_root served run_a and derived key_mid; key_mid served run_b and key_leaf;
    # key_leaf served run_c. Eviction of key_root must cascade to all three keys
    # and recall runs a, b, c.
    ledger.record(ServedFromEdge(cache_key="key_root", consumer="run_a", output_hash="sha256:o1"))
    ledger.record(ServedFromEdge(cache_key="key_root", consumer="key_mid"))
    ledger.record(ServedFromEdge(cache_key="key_mid", consumer="run_b"))
    ledger.record(ServedFromEdge(cache_key="key_mid", consumer="key_leaf"))
    ledger.record(ServedFromEdge(cache_key="key_leaf", consumer="run_c"))


def test_evict_cascades_transitively(tmp_path: Path) -> None:
    ledger = ServedFromLedger(tmp_path / "served_from.jsonl")
    tombstones = TombstoneStore(tmp_path / "tombstones.jsonl")
    _seed_ledger(ledger)

    recall = tombstones.evict("key_root", "pr_reverted", ledger=ledger, ts=42)

    assert recall.root_key == "key_root"
    assert set(recall.tombstoned) == {"key_root", "key_mid", "key_leaf"}
    # Consuming runs (non-key consumers) form the forensic recall set.
    assert set(recall.consumers) == {"run_a", "run_b", "run_c"}


def test_tombstoned_keys_are_hard_misses(tmp_path: Path) -> None:
    ledger = ServedFromLedger(tmp_path / "served_from.jsonl")
    tombstones = TombstoneStore(tmp_path / "tombstones.jsonl")
    _seed_ledger(ledger)
    tombstones.evict("key_root", "bad", ledger=ledger)

    # AC5: an evicted key can never serve again, even when its drift verdict
    # would be fresh - is_tombstoned short circuits the lookup.
    assert tombstones.is_tombstoned("key_root") is True
    assert tombstones.is_tombstoned("key_leaf") is True
    assert tombstones.is_tombstoned("never_seen") is False


def test_eviction_recall_set_is_deterministic(tmp_path: Path) -> None:
    # Two operators evicting the same key against the same ledger produce the
    # byte-identical recall set and tombstone order.
    ledger1 = ServedFromLedger(tmp_path / "a.jsonl")
    ledger2 = ServedFromLedger(tmp_path / "b.jsonl")
    _seed_ledger(ledger1)
    _seed_ledger(ledger2)
    r1 = TombstoneStore(tmp_path / "t1.jsonl").evict("key_root", "x", ledger=ledger1)
    r2 = TombstoneStore(tmp_path / "t2.jsonl").evict("key_root", "x", ledger=ledger2)
    assert r1.to_dict() == r2.to_dict()


def test_eviction_persists_across_reopen(tmp_path: Path) -> None:
    ledger = ServedFromLedger(tmp_path / "served_from.jsonl")
    _seed_ledger(ledger)
    TombstoneStore(tmp_path / "tombstones.jsonl").evict("key_mid", "x", ledger=ledger)

    reopened = TombstoneStore(tmp_path / "tombstones.jsonl")
    assert reopened.is_tombstoned("key_mid")
    assert reopened.is_tombstoned("key_leaf")
    # key_root is upstream of key_mid, so it is NOT tombstoned by evicting mid.
    assert not reopened.is_tombstoned("key_root")


def test_cycle_in_served_from_graph_terminates(tmp_path: Path) -> None:
    ledger = ServedFromLedger(tmp_path / "served_from.jsonl")
    ledger.record(ServedFromEdge(cache_key="k1", consumer="k2"))
    ledger.record(ServedFromEdge(cache_key="k2", consumer="k1"))
    recall = TombstoneStore(tmp_path / "t.jsonl").evict("k1", "x", ledger=ledger)
    assert set(recall.tombstoned) == {"k1", "k2"}


def test_evict_isolated_key_recalls_nothing(tmp_path: Path) -> None:
    ledger = ServedFromLedger(tmp_path / "served_from.jsonl")
    recall = TombstoneStore(tmp_path / "t.jsonl").evict("lonely", "x", ledger=ledger)
    assert recall.tombstoned == ["lonely"]
    assert recall.consumers == []
