"""Integration tests for the cross-cycle consensus relay.

These cases exercise multi-cycle handoff against the on-disk store,
covering: cold-start replay, atomic-write durability, chain replay after a
process restart, env-driven path override, and markdown export round-tripping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.orchestration.consensus_relay import (
    GENESIS_PREV_HASH,
    RelayChainError,
    RelayDecision,
    RelayStore,
    compute_relay_hmac,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> RelayStore:
    return RelayStore(tmp_path / "relay", key=b"k" * 32)


def _seed_chain(s: RelayStore, n: int) -> None:
    for i in range(n):
        s.append(
            cycle_id=f"cycle-{i:03d}",
            phase="plan" if i % 2 == 0 else "implement",
            did_this_cycle=f"step {i}",
            decisions=(RelayDecision(title=f"d{i}", rationale="r", confidence=0.5),),
            open_questions=(f"q{i}",),
            blockers=(),
            next_action=f"next-{i}",
        )


# ---------------------------------------------------------------------------
# Integration cases (10+)
# ---------------------------------------------------------------------------


class TestMultiCycleHandoff:
    def test_cold_start_replay_after_three_cycles(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        _seed_chain(s, 3)
        # Simulate a cold start by constructing a brand-new store.
        cold = RelayStore(s.root, key=s.key)
        cold.verify()
        head = cold.head()
        assert head is not None
        assert head.cycle_id == "cycle-002"
        assert head.next_action == "next-2"

    def test_acknowledge_before_next_append(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        s.append(cycle_id="cycle-000", phase="plan", next_action="step 1")
        ack = s.acknowledge("cycle-000")
        assert ack.acknowledged is True
        s.append(cycle_id="cycle-001", phase="implement", next_action="step 2")
        s.verify()

    def test_atomic_write_survives_partial_failure_simulation(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        s.append(cycle_id="cycle-000", phase="plan", next_action="ok")
        # Drop a leftover .tmp file as if a previous writer was killed
        # between fdopen and rename. The chain must still verify.
        (s.root / "cycle-000.json.tmp.99999.deadbeef").write_text("garbage")
        s.verify()
        # And a follow-up append still succeeds and leaves no tmp behind.
        s.append(cycle_id="cycle-001", phase="implement", next_action="ok2")
        leftovers = [p.name for p in s.root.iterdir() if ".tmp." in p.name]
        # The simulated leftover stays put (we never run cleanup), but no
        # new .tmp file appears.
        assert all("99999" in name for name in leftovers)

    def test_env_path_round_trip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "env-store"
        monkeypatch.setenv("BERNSTEIN_ORCHESTRATION_RELAY_PATH", str(target))
        monkeypatch.delenv("BERNSTEIN_RELAY_KEY", raising=False)
        monkeypatch.setenv("BERNSTEIN_OPERATOR_ID", "alice")
        s1 = RelayStore()
        s1.append(cycle_id="c1", phase="plan", next_action="x")
        s2 = RelayStore()
        head = s2.head()
        assert head is not None and head.cycle_id == "c1"
        assert s1.root == target

    def test_env_key_round_trip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        key_hex = "ab" * 32
        monkeypatch.setenv("BERNSTEIN_RELAY_KEY", key_hex)
        s1 = RelayStore(tmp_path / "key-store")
        signed = s1.append(cycle_id="cycle-000", phase="plan", next_action="x")
        recomputed = compute_relay_hmac(signed, bytes.fromhex(key_hex))
        assert recomputed == signed.operator_hmac
        # A reader with the wrong key cannot verify.
        bad = RelayStore(s1.root, key=b"z" * 32)
        with pytest.raises(RelayChainError):
            bad.verify()

    def test_markdown_export_contains_full_handoff(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        s.append(
            cycle_id="cycle-final",
            phase="review",
            did_this_cycle="Finished the integration suite.",
            decisions=(RelayDecision(title="land it", rationale="green", confidence=0.92),),
            open_questions=("Will the hook fire on dry-run?",),
            blockers=("ci flaky on macos",),
            next_action="Cut a release candidate.",
            calibration={"budget": "12k"},
        )
        md = s.export_markdown()
        for needle in (
            "cycle-final",
            "review",
            "Finished the integration suite",
            "land it",
            "Will the hook fire",
            "ci flaky",
            "Cut a release candidate",
        ):
            assert needle in md

    def test_rotation_events_capture_all_cycles(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        _seed_chain(s, 4)
        records = [json.loads(line) for line in (s.root / "events.jsonl").read_text().splitlines() if line.strip()]
        assert [r["cycle_id"] for r in records] == [f"cycle-{i:03d}" for i in range(4)]
        assert all(r["event"] == "relay.rotated" for r in records)

    def test_genesis_prev_hash_is_constant(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        first = s.append(cycle_id="cycle-000", phase="plan", next_action="boot")
        assert first.prev_hash == GENESIS_PREV_HASH
