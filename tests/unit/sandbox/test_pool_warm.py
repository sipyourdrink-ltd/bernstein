"""Tests for warm claim-ahead hash-equality keying (#2547).

Covers: a warm slot only attaches when its provisioned manifest hash equals the
dispatch's effective hash; a mismatch quarantines the slot (with a receipt) and
the caller falls back to cold provisioning.
"""

from __future__ import annotations

from bernstein.core.sandbox.pool_warm import (
    WarmClaimOutcome,
    WarmSlotKey,
    evaluate_warm_claim,
    slot_matches,
)


def _slot(effective: str) -> WarmSlotKey:
    return WarmSlotKey(slot_id="slot-1", pool_hash="a" * 64, effective_manifest_hash=effective)


class TestWarmClaim:
    def test_equal_hash_attaches(self):
        slot = _slot("d" * 64)
        assert slot_matches(slot, "d" * 64)
        assert evaluate_warm_claim(slot, "d" * 64) is WarmClaimOutcome.ATTACH

    def test_divergent_hash_quarantines(self):
        slot = _slot("d" * 64)
        assert not slot_matches(slot, "e" * 64)
        assert evaluate_warm_claim(slot, "e" * 64) is WarmClaimOutcome.QUARANTINE

    def test_empty_provisioned_hash_never_attaches(self):
        slot = _slot("")
        assert not slot_matches(slot, "")
        assert evaluate_warm_claim(slot, "") is WarmClaimOutcome.QUARANTINE

    def test_single_byte_divergence_quarantines(self):
        base = "d" * 63
        slot = _slot(base + "0")
        assert evaluate_warm_claim(slot, base + "1") is WarmClaimOutcome.QUARANTINE
