"""Unit tests for claim-based fleet dedup and duplicate-of receipts (AC3, AC4)."""

from __future__ import annotations

import concurrent.futures
import dataclasses
from pathlib import Path

from bernstein.core.persistence.cache_dedup import (
    CacheKeyArbiter,
    DuplicateOfReceipt,
    mint_duplicate_receipt,
    verify_duplicate_receipt,
)

_KEY = b"0" * 32


def _receipt() -> DuplicateOfReceipt:
    return mint_duplicate_receipt(
        cache_key="cafe",
        winner="worker-1",
        loser="worker-2",
        claim_position=1,
        winner_output_ref="sha256:winneroutput",
        ts=100,
        hmac_key=_KEY,
    )


def test_receipt_verifies_offline() -> None:
    receipt = _receipt()
    ok, field = verify_duplicate_receipt(receipt, hmac_key=_KEY)
    assert ok is True
    assert field is None


def test_receipt_verify_fails_under_wrong_key() -> None:
    ok, field = verify_duplicate_receipt(_receipt(), hmac_key=b"1" * 32)
    assert ok is False
    assert field == "hmac"


def test_mutating_winner_reference_is_named() -> None:
    authoritative = _receipt()
    forged = dataclasses.replace(authoritative, winner_output_ref="sha256:TAMPERED")
    ok, field = verify_duplicate_receipt(forged, hmac_key=_KEY, authoritative=authoritative)
    assert ok is False
    assert field == "winner_output_ref"


def test_mutating_cache_key_is_named() -> None:
    authoritative = _receipt()
    forged = dataclasses.replace(authoritative, cache_key="beef")
    ok, field = verify_duplicate_receipt(forged, hmac_key=_KEY, authoritative=authoritative)
    assert ok is False
    assert field == "cache_key"


def test_mutating_claim_position_is_named() -> None:
    authoritative = _receipt()
    forged = dataclasses.replace(authoritative, claim_position=99)
    ok, field = verify_duplicate_receipt(forged, hmac_key=_KEY, authoritative=authoritative)
    assert ok is False
    assert field == "claim_position"


def test_receipt_json_roundtrip() -> None:
    receipt = _receipt()
    restored = DuplicateOfReceipt.from_dict(receipt.to_dict())
    assert restored == receipt
    ok, _ = verify_duplicate_receipt(restored, hmac_key=_KEY)
    assert ok


def test_single_winner_among_contenders(tmp_path: Path) -> None:
    # AC4: exactly one spawn occurs among N contenders for the same key.
    backlog = tmp_path / "arbiter.json"
    arbiter = CacheKeyArbiter(backlog, "hotkey")
    outcomes = [arbiter.contend(f"worker-{i}") for i in range(6)]
    winners = [o for o in outcomes if o.won]
    losers = [o for o in outcomes if not o.won]
    assert len(winners) == 1
    assert len(losers) == 5
    assert all(o.winner == winners[0].claimer for o in losers)


def test_killed_winner_release_lets_successor_win(tmp_path: Path) -> None:
    backlog = tmp_path / "arbiter.json"
    arbiter = CacheKeyArbiter(backlog, "hotkey")
    first = arbiter.contend("worker-1")
    assert first.won
    second = arbiter.contend("worker-2")
    assert not second.won

    # Winner is killed mid-run: release the claim; exactly one successor wins.
    arbiter.release()
    third = arbiter.contend("worker-3")
    assert third.won


def test_concurrent_contention_yields_one_winner(tmp_path: Path) -> None:
    # Chaos-style: many threads race the same cache key through the atomic
    # claim; the file lock guarantees exactly one winner.
    backlog = tmp_path / "arbiter.json"
    arbiter = CacheKeyArbiter(backlog, "hotkey")
    # Pre-create the backlog so all threads race the claim, not the create.
    arbiter._ensure_backlog()

    def _try(i: int) -> bool:
        return arbiter.contend(f"w-{i}").won

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_try, range(16)))
    assert sum(1 for won in results if won) == 1
