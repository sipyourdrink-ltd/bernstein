"""Tests for the claim-time fleet-variable config plane (#2550).

Each ``var set`` is an audit-chain event carrying the old and new value
hashes and the per-name write ordinal; each claim-time read pins
``(name, value_hash, chain_position)`` into the reading task's lineage
spine; replay resolves reads from the pinned hash - never the live value -
so a run replayed after N more mutations produces byte-identical reads, and
divergence between two workers is explained offline from the chain alone.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.fleet.variables import (
    FleetVariableStore,
    replay_reads,
    value_hash_of,
)
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.security.audit_chain import EVENT_FLEET_VAR_SET, AuditChainStore

_KEY = b"k" * 32


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=_KEY)


def _store(tmp_path: Path, chain: AuditChainStore) -> FleetVariableStore:
    return FleetVariableStore(tmp_path / "fleet" / "variables", chain=chain)


def _spine(tmp_path: Path, run_id: str = "run-a") -> LineageSpine:
    return LineageSpine(tmp_path / "lineage", run_id=run_id, hmac_key=_KEY)


def test_var_set_is_a_chain_event_with_value_hashes(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    store = _store(tmp_path, chain)

    write = store.set("threshold", 5, actor="alice")

    assert write.name == "threshold"
    assert write.old_value_hash == ""
    assert write.new_value_hash == value_hash_of(5)
    assert write.chain_position == 0

    events = chain.query(event_type=EVENT_FLEET_VAR_SET)
    assert len(events) == 1
    assert events[0].details["name"] == "threshold"
    assert events[0].details["new_value_hash"] == value_hash_of(5)
    assert events[0].actor == "alice"

    ok, errors = chain.verify()
    assert ok, errors


def test_second_write_carries_prior_hash_and_increments_position(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    store = _store(tmp_path, chain)

    first = store.set("threshold", 5)
    second = store.set("threshold", 9)

    assert second.old_value_hash == first.new_value_hash
    assert second.new_value_hash == value_hash_of(9)
    assert second.chain_position == 1
    assert store.get("threshold") == 9

    ok, errors = chain.verify()
    assert ok, errors


def test_tamper_with_historical_write_flips_verify(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    store = _store(tmp_path, chain)
    store.set("threshold", 5)
    store.set("threshold", 9)

    # Mutate a persisted historical audit record in place.
    day_file = next((tmp_path / "audit").glob("*.jsonl"))
    raw = day_file.read_text(encoding="utf-8")
    tampered = raw.replace('"chain_position": 0', '"chain_position": 7')
    assert tampered != raw
    day_file.write_text(tampered, encoding="utf-8")

    ok, errors = chain.verify()
    assert not ok
    assert errors


def test_replay_resolves_pinned_value_through_n_mutations(tmp_path: Path) -> None:
    # Determinism axis: a live-value config store cannot replay through
    # mutation. Here the read is pinned, so N later writes cannot change
    # what replay resolves.
    chain = _chain(tmp_path)
    store = _store(tmp_path, chain)
    spine = _spine(tmp_path)

    store.set("endpoint", {"host": "a", "port": 1})
    value_at_read, pin = store.read_for_task("endpoint", spine)
    assert value_at_read == {"host": "a", "port": 1}
    assert pin.chain_position == 0

    # Mutate the live value N more times.
    for i in range(5):
        store.set("endpoint", {"host": "b", "port": 100 + i})
    assert store.get("endpoint") == {"host": "b", "port": 104}

    # Replay resolves from the pinned hash, not the live value.
    replayed = replay_reads(spine, store)
    assert len(replayed) == 1
    assert replayed[0].name == "endpoint"
    assert replayed[0].value == {"host": "a", "port": 1}
    assert replayed[0].value_hash == pin.value_hash

    assert spine.verify().ok


def test_two_workers_divergence_explained_offline_from_chain(tmp_path: Path) -> None:
    # Verifiability + observability: with no server running, the chain alone
    # explains why two workers read different values - the write that landed
    # between their two pinned chain positions.
    chain = _chain(tmp_path)
    store = _store(tmp_path, chain)
    spine_a = _spine(tmp_path, run_id="worker-a")
    spine_b = _spine(tmp_path, run_id="worker-b")

    store.set("rollout", 10)  # position 0
    _, pin_a = store.read_for_task("rollout", spine_a)  # worker A pins pos 0
    store.set("rollout", 20)  # position 1
    store.set("rollout", 30)  # position 2
    _, pin_b = store.read_for_task("rollout", spine_b)  # worker B pins pos 2

    assert pin_a.chain_position == 0
    assert pin_b.chain_position == 2

    # Rebuild a fresh, server-less store from the same on-disk chain + blobs.
    offline_chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    offline_store = FleetVariableStore(tmp_path / "fleet" / "variables", chain=offline_chain)

    between = offline_store.explain_divergence("rollout", pin_a.chain_position, pin_b.chain_position)
    # The writes that landed strictly after A's position, up to B's.
    positions = [w.chain_position for w in between]
    assert positions == [1, 2]
    assert between[-1].new_value_hash == value_hash_of(30)


def test_history_lists_writes_in_order(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    store = _store(tmp_path, chain)
    store.set("k", 1)
    store.set("k", 2)
    store.set("other", 99)
    store.set("k", 3)

    hist = store.history("k")
    assert [w.chain_position for w in hist] == [0, 1, 2]
    assert [w.new_value_hash for w in hist] == [
        value_hash_of(1),
        value_hash_of(2),
        value_hash_of(3),
    ]


def test_list_names(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    store = _store(tmp_path, chain)
    store.set("b", 1)
    store.set("a", 2)
    assert store.list_names() == ["a", "b"]
