"""Audit-chain events for the cache policy engine (issue #2551, AC7).

Every hit, miss, dedup claim, and eviction is an audit-chain event carrying the
policy hash and recipe hash, and the chain stays verifiable after the mirror.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_CACHE_DEDUP_CLAIM,
    EVENT_CACHE_EVICTION,
    EVENT_CACHE_HIT,
    EVENT_CACHE_MISS,
    AuditChainStore,
    record_cache_dedup_claim,
    record_cache_eviction,
    record_cache_hit,
    record_cache_miss,
)


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"0" * 32)


def test_cache_hit_event_carries_policy_and_recipe_hash(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    event = record_cache_hit(
        chain=chain,
        cache_key="cafe",
        policy_hash="sha256:policy",
        recipe_hash="sha256:recipe",
        entry_content_id="sha256:entry",
        verified=True,
    )
    assert event.event_type == EVENT_CACHE_HIT
    details = chain.query(event_type=EVENT_CACHE_HIT)[0].details
    assert details["policy_hash"] == "sha256:policy"
    assert details["recipe_hash"] == "sha256:recipe"
    assert details["entry_content_id"] == "sha256:entry"
    assert details["verified"] is True
    assert "prev_chain_digest" in details


def test_cache_miss_event(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    record_cache_miss(
        chain=chain,
        cache_key="cafe",
        policy_hash="sha256:policy",
        recipe_hash="sha256:recipe",
        reason="stale",
    )
    details = chain.query(event_type=EVENT_CACHE_MISS)[0].details
    assert details["reason"] == "stale"
    assert details["policy_hash"] == "sha256:policy"
    assert details["recipe_hash"] == "sha256:recipe"


def test_cache_dedup_claim_event(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    record_cache_dedup_claim(
        chain=chain,
        cache_key="cafe",
        winner="w-1",
        loser="w-2",
        claim_position=1,
        receipt_hmac="deadbeef",
        policy_hash="sha256:policy",
        recipe_hash="sha256:recipe",
    )
    details = chain.query(event_type=EVENT_CACHE_DEDUP_CLAIM)[0].details
    assert details["winner"] == "w-1"
    assert details["loser"] == "w-2"
    assert details["claim_position"] == 1
    assert details["receipt_hmac"] == "deadbeef"


def test_cache_eviction_event(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    record_cache_eviction(
        chain=chain,
        cache_key="cafe",
        reason="pr_reverted",
        tombstoned_count=3,
        recall_count=2,
    )
    details = chain.query(event_type=EVENT_CACHE_EVICTION)[0].details
    assert details["reason"] == "pr_reverted"
    assert details["tombstoned_count"] == 3
    assert details["recall_count"] == 2


def test_chain_verifies_after_cache_events(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    record_cache_hit(
        chain=chain,
        cache_key="a",
        policy_hash="sha256:p",
        recipe_hash="sha256:r",
        entry_content_id="sha256:e",
        verified=True,
    )
    record_cache_miss(chain=chain, cache_key="b", policy_hash="sha256:p", recipe_hash="sha256:r", reason="absent")
    record_cache_eviction(chain=chain, cache_key="a", reason="x", tombstoned_count=1, recall_count=0)
    ok, errors = chain.verify()
    assert ok, errors
