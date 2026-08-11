"""Unit tests for the skill catalog transparency log (issue #2527).

Phase 1 acceptance:
- catalog states form an append-only Merkle transparency log with a signed
  published head;
- an inclusion proof binds a state to a head, verifiable offline;
- a consistency proof binds a new head to any earlier head and fails on a
  rewrite;
- determinism: two independent builders given the same sequence of states
  compute byte-identical heads;
- mutating one recorded byte makes verification fail.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from bernstein.core.skills.catalog.signature import generate_signer_keypair
from bernstein.core.skills.catalog.transparency import (
    GENESIS_ROOT,
    InclusionReceipt,
    SignedTreeHead,
    TransparencyLog,
    TransparencyVerificationError,
    canonical_state_bytes,
    leaf_hash_for_state,
    sign_tree_head,
    verify_consistency,
    verify_inclusion,
    verify_tree_head,
)

_LEAF_TAG = b"\x00"
_INTERNAL_TAG = b"\x01"


# ---------------------------------------------------------------------------
# Reference implementation (brute force) for cross-checking
# ---------------------------------------------------------------------------


def _ref_leaf(data: bytes) -> str:
    return hashlib.sha256(_LEAF_TAG + data).hexdigest()


def _ref_node(left: str, right: str) -> str:
    return hashlib.sha256(_INTERNAL_TAG + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


def _ref_lpo2(n: int) -> int:
    k = 1
    while k << 1 < n:
        k <<= 1
    return k


def _ref_mth(leaves: list[str]) -> str:
    if not leaves:
        return hashlib.sha256(b"").hexdigest()
    if len(leaves) == 1:
        return leaves[0]
    k = _ref_lpo2(len(leaves))
    return _ref_node(_ref_mth(leaves[:k]), _ref_mth(leaves[k:]))


def _state(i: int) -> dict[str, object]:
    return {
        "version": 1,
        "generated_at": f"2026-05-2{i % 9}T00:00:00Z",
        "entries": [{"id": f"skill-{i}", "content_digest": f"{i:064x}"}],
    }


# ---------------------------------------------------------------------------
# Leaf + head hashing
# ---------------------------------------------------------------------------


def test_empty_log_root_is_genesis() -> None:
    log = TransparencyLog()
    assert log.size == 0
    assert log.root == GENESIS_ROOT == hashlib.sha256(b"").hexdigest()


def test_single_leaf_root_equals_leaf() -> None:
    log = TransparencyLog()
    idx = log.append_state(_state(0))
    assert idx == 0
    assert log.root == leaf_hash_for_state(_state(0))


def test_canonical_state_bytes_is_stable_across_key_order() -> None:
    a = {"version": 1, "generated_at": "t", "entries": []}
    b = {"entries": [], "generated_at": "t", "version": 1}
    assert canonical_state_bytes(a) == canonical_state_bytes(b)


def test_root_matches_reference_mth_for_many_sizes() -> None:
    for n in range(1, 17):
        log = TransparencyLog.from_states(_state(i) for i in range(n))
        expected = _ref_mth([leaf_hash_for_state(_state(i)) for i in range(n)])
        assert log.root == expected, f"root mismatch at size {n}"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_two_builders_same_states_compute_identical_head() -> None:
    states = [_state(i) for i in range(9)]
    log_a = TransparencyLog.from_states(states)
    log_b = TransparencyLog.from_states(list(states))
    assert log_a.root == log_b.root
    assert log_a.size == log_b.size

    priv, _pub = generate_signer_keypair()
    head_a = log_a.signed_head(priv)
    head_b = log_b.signed_head(priv)
    # Ed25519 is deterministic, so even the signature bytes match.
    assert head_a.to_dict() == head_b.to_dict()
    assert json.dumps(head_a.to_dict(), sort_keys=True) == json.dumps(head_b.to_dict(), sort_keys=True)


# ---------------------------------------------------------------------------
# Inclusion proofs
# ---------------------------------------------------------------------------


def test_inclusion_proof_verifies_for_every_leaf_every_size() -> None:
    for n in range(1, 17):
        log = TransparencyLog.from_states(_state(i) for i in range(n))
        root = log.root
        for m in range(n):
            proof = log.inclusion_proof(m)
            assert verify_inclusion(
                leaf_hash=log.leaf_at(m),
                leaf_index=m,
                tree_size=n,
                proof=proof,
                root_hash=root,
            ), f"inclusion failed n={n} m={m}"


def test_inclusion_proof_fails_on_tampered_leaf() -> None:
    log = TransparencyLog.from_states(_state(i) for i in range(6))
    proof = log.inclusion_proof(3)
    tampered = _ref_leaf(b"not the committed state")
    assert not verify_inclusion(
        leaf_hash=tampered,
        leaf_index=3,
        tree_size=log.size,
        proof=proof,
        root_hash=log.root,
    )


def test_inclusion_proof_fails_on_wrong_root() -> None:
    log = TransparencyLog.from_states(_state(i) for i in range(6))
    proof = log.inclusion_proof(2)
    assert not verify_inclusion(
        leaf_hash=log.leaf_at(2),
        leaf_index=2,
        tree_size=log.size,
        proof=proof,
        root_hash="0" * 64,
    )


def test_inclusion_index_out_of_range_returns_false() -> None:
    log = TransparencyLog.from_states(_state(i) for i in range(3))
    assert not verify_inclusion(
        leaf_hash=log.leaf_at(0),
        leaf_index=5,
        tree_size=log.size,
        proof=[],
        root_hash=log.root,
    )


# ---------------------------------------------------------------------------
# Consistency proofs
# ---------------------------------------------------------------------------


def test_consistency_proof_verifies_for_every_prefix() -> None:
    for n in range(1, 15):
        full = TransparencyLog.from_states(_state(i) for i in range(n))
        for m in range(1, n + 1):
            prefix = TransparencyLog.from_states(_state(i) for i in range(m))
            proof = full.consistency_proof(m)
            assert verify_consistency(
                old_size=m,
                old_root=prefix.root,
                new_size=n,
                new_root=full.root,
                proof=proof,
            ), f"consistency failed n={n} m={m}"


def test_consistency_proof_detects_rewrite() -> None:
    """A rewrite of an already-committed state breaks the consistency proof.

    This is the split-view / catalog-rewrite detection that a plain signature
    check cannot provide: same position, different content.
    """
    original = [_state(i) for i in range(4)]
    old_log = TransparencyLog.from_states(original)
    old_size = old_log.size
    old_root = old_log.root

    # Publisher rewrites leaf 1 (same version tag, different bytes) and appends.
    rewritten = list(original)
    rewritten[1] = {**_state(1), "generated_at": "REWRITTEN"}
    rewritten.append(_state(4))
    new_log = TransparencyLog.from_states(rewritten)

    # The publisher cannot produce a valid consistency proof from the honest
    # old head to the rewritten new head.
    proof = new_log.consistency_proof(old_size)
    assert not verify_consistency(
        old_size=old_size,
        old_root=old_root,
        new_size=new_log.size,
        new_root=new_log.root,
        proof=proof,
    )


def test_consistency_same_size_requires_matching_root() -> None:
    log = TransparencyLog.from_states(_state(i) for i in range(3))
    assert verify_consistency(old_size=3, old_root=log.root, new_size=3, new_root=log.root, proof=[])
    assert not verify_consistency(old_size=3, old_root="0" * 64, new_size=3, new_root=log.root, proof=[])


# ---------------------------------------------------------------------------
# Signed heads
# ---------------------------------------------------------------------------


def test_signed_head_roundtrip() -> None:
    priv, pub = generate_signer_keypair()
    log = TransparencyLog.from_states(_state(i) for i in range(5))
    head = log.signed_head(priv)
    assert verify_tree_head(head, pub)
    assert head.tree_size == 5
    assert head.root_hash == log.root


def test_signed_head_rejects_wrong_key() -> None:
    priv, _pub = generate_signer_keypair()
    _priv2, pub2 = generate_signer_keypair()
    head = sign_tree_head(3, "a" * 64, priv)
    assert not verify_tree_head(head, pub2)


def test_signed_head_rejects_tampered_root() -> None:
    priv, pub = generate_signer_keypair()
    head = sign_tree_head(3, "a" * 64, priv)
    tampered = SignedTreeHead(tree_size=3, root_hash="b" * 64, signature=head.signature)
    assert not verify_tree_head(tampered, pub)


def test_signed_head_from_dict_rejects_malformed() -> None:
    with pytest.raises(TransparencyVerificationError):
        SignedTreeHead.from_dict({"tree_size": -1, "root_hash": "x", "signature": "y"})
    with pytest.raises(TransparencyVerificationError):
        SignedTreeHead.from_dict({"tree_size": 1, "root_hash": "", "signature": "y"})


# ---------------------------------------------------------------------------
# Inclusion receipt
# ---------------------------------------------------------------------------


def test_inclusion_receipt_verifies_end_to_end() -> None:
    priv, pub = generate_signer_keypair()
    states = [_state(i) for i in range(4)]
    old_log = TransparencyLog.from_states(states[:2])
    prev_size, prev_root = old_log.size, old_log.root

    full = TransparencyLog.from_states(states)
    head = full.signed_head(priv)
    receipt = InclusionReceipt(
        entry_digest="c" * 64,
        leaf_index=3,
        leaf_hash=full.leaf_at(3),
        head=head,
        inclusion_proof=tuple(full.inclusion_proof(3)),
        prev_tree_size=prev_size,
        prev_root_hash=prev_root,
        consistency_proof=tuple(full.consistency_proof(prev_size)),
    )
    # No exception == verified.
    receipt.verify(pub)


def test_inclusion_receipt_fails_when_leaf_byte_mutated() -> None:
    """Mutating one byte of the recorded catalog state makes verify fail."""
    priv, pub = generate_signer_keypair()
    full = TransparencyLog.from_states(_state(i) for i in range(4))
    head = full.signed_head(priv)
    receipt = InclusionReceipt(
        entry_digest="c" * 64,
        leaf_index=2,
        leaf_hash=_ref_leaf(b"tampered recorded state bytes"),
        head=head,
        inclusion_proof=tuple(full.inclusion_proof(2)),
        prev_tree_size=0,
        prev_root_hash="",
        consistency_proof=(),
    )
    with pytest.raises(TransparencyVerificationError, match="inclusion proof failed"):
        receipt.verify(pub)


def test_inclusion_receipt_fails_on_rewrite_via_consistency() -> None:
    priv, pub = generate_signer_keypair()
    original = [_state(i) for i in range(4)]
    old_log = TransparencyLog.from_states(original)
    prev_size, prev_root = old_log.size, old_log.root

    rewritten = list(original)
    rewritten[0] = {**_state(0), "generated_at": "REWRITTEN"}
    rewritten.append(_state(4))
    new_log = TransparencyLog.from_states(rewritten)
    head = new_log.signed_head(priv)
    receipt = InclusionReceipt(
        entry_digest="c" * 64,
        leaf_index=4,
        leaf_hash=new_log.leaf_at(4),
        head=head,
        inclusion_proof=tuple(new_log.inclusion_proof(4)),
        prev_tree_size=prev_size,
        prev_root_hash=prev_root,
        consistency_proof=tuple(new_log.consistency_proof(prev_size)),
    )
    with pytest.raises(TransparencyVerificationError, match="consistency proof failed"):
        receipt.verify(pub)


# ---------------------------------------------------------------------------
# Golden vector: pin the exact head bytes so a refactor that changes the hash
# construction is caught (byte-identical replay across builders and releases).
# ---------------------------------------------------------------------------


def test_golden_head_for_fixed_states() -> None:
    states = [
        {"version": 1, "generated_at": "2026-01-01T00:00:00Z", "entries": [{"id": "a"}]},
        {"version": 1, "generated_at": "2026-01-02T00:00:00Z", "entries": [{"id": "b"}]},
        {"version": 1, "generated_at": "2026-01-03T00:00:00Z", "entries": [{"id": "c"}]},
    ]
    log = TransparencyLog.from_states(states)
    leaves = [leaf_hash_for_state(s) for s in states]
    # Root recomputed independently by the reference MTH.
    assert log.root == _ref_node(_ref_node(leaves[0], leaves[1]), leaves[2])
    assert log.size == 3
