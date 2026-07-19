"""Unit tests for the fork-race selection receipt (#2613).

These pin the receipt's determinism and offline-verifiability contract:
canonical bytes are stable, the signed body carries no wall-clock/chain
field, verify cross-checks the derived winner + loser digests, a single
mutated byte fails verification, and a NaN score is refused at build time.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.sandbox.selection_receipt import (
    RaceCandidate,
    SelectionReceipt,
    SelectionReceiptError,
    build_selection_receipt,
    canonical_receipt_bytes,
    keyid_for,
    load_or_create_signing_key,
    read_receipt_file,
    receipt_from_dict,
    receipt_to_dict,
    sign_receipt,
    snapshot_digests,
    verify_receipt,
    write_receipt,
)

_D = "0123456789abcdef" * 4  # a 64-char hex-ish stand-in digest


def _cand(task_id: str, digest: str, tests: bool = True) -> RaceCandidate:
    # Keys are deliberately NOT in alphabetical insertion order so that the
    # canonical-ordering guard in RaceCandidate.to_dict is actually exercised
    # by every fixture that flows through this helper (#2706 item 3).
    return RaceCandidate(
        task_id=task_id,
        terminal_snapshot_digest=digest,
        score_vector={"reversibility": 1.0, "correctness": 1.0 if tests else 0.0, "cost": 0.0},
    )


def _build_signed(key: Ed25519PrivateKey):
    receipt = build_selection_receipt(
        base_snapshot_digest="a" * 64,
        candidates=[_cand("candidate-1", "b" * 64), _cand("candidate-0", "c" * 64, tests=False)],
        winner_task_id="candidate-1",
        ranker_profile={"method": "topsis", "criteria": []},
        public_key=key.public_key(),
    )
    return sign_receipt(receipt, private_key=key)


def test_build_orders_candidates_and_derives_losers() -> None:
    key = Ed25519PrivateKey.generate()
    signed = _build_signed(key)
    # candidates sorted by task_id
    assert [c["task_id"] for c in signed.candidates] == ["candidate-0", "candidate-1"]
    # winner digest derived from the winning candidate
    assert signed.winner_snapshot_digest == "b" * 64
    # loser is the single non-winner candidate's digest
    assert signed.loser_snapshot_digests == ("c" * 64,)


def test_signed_receipt_verifies() -> None:
    key = Ed25519PrivateKey.generate()
    signed = _build_signed(key)
    result = verify_receipt(signed)
    assert result.ok, result.errors


def test_verify_receipt_expected_keyid_binds_to_trusted_signer() -> None:
    """``expected_keyid`` rejects a validly-signed, self-consistent receipt
    that was signed by anyone other than the named trusted signer."""
    key = Ed25519PrivateKey.generate()
    signed = _build_signed(key)

    # The correct trusted keyid still accepts.
    assert verify_receipt(signed, expected_keyid=signed.keyid).ok

    # A receipt re-signed under a *different* key is self-consistent (its own
    # keyid matches its own embedded key) yet must be rejected against the
    # trusted keyid - this is the attack the check closes.
    attacker = Ed25519PrivateKey.generate()
    forged = _build_signed(attacker)
    assert verify_receipt(forged).ok  # self-consistent on its own
    result = verify_receipt(forged, expected_keyid=keyid_for(key.public_key()))
    assert not result.ok
    assert any("trusted signer" in e for e in result.errors)


def test_verify_receipt_expected_keyid_tolerates_none_keyid() -> None:
    """A receipt carrying a ``None`` keyid must report a mismatch, not TypeError."""
    import dataclasses

    key = Ed25519PrivateKey.generate()
    signed = _build_signed(key)
    forged = dataclasses.replace(signed, keyid=None)  # type: ignore[arg-type]
    result = verify_receipt(forged, expected_keyid="deadbeef")
    assert not result.ok
    assert any("trusted signer" in e for e in result.errors)


def test_build_rejects_duplicate_or_empty_task_ids() -> None:
    """Duplicate/empty task_ids would collapse in the candidate map, dropping a
    candidate's terminal snapshot from the signed receipt - reject at build."""
    key = Ed25519PrivateKey.generate()
    with pytest.raises(SelectionReceiptError, match="unique"):
        build_selection_receipt(
            base_snapshot_digest="a" * 64,
            candidates=[_cand("dup", "b" * 64), _cand("dup", "c" * 64)],
            winner_task_id="dup",
            ranker_profile={"method": "topsis", "criteria": []},
            public_key=key.public_key(),
        )
    with pytest.raises(SelectionReceiptError, match="non-empty"):
        build_selection_receipt(
            base_snapshot_digest="a" * 64,
            candidates=[_cand("", "b" * 64)],
            winner_task_id="",
            ranker_profile={"method": "topsis", "criteria": []},
            public_key=key.public_key(),
        )


def test_verify_rejects_duplicate_candidate_task_ids() -> None:
    """A deserialised receipt whose candidate list has a collision must fail
    verification rather than silently collapsing on the task_id map."""
    key = Ed25519PrivateKey.generate()
    signed = _build_signed(key)
    tampered = receipt_to_dict(signed)
    tampered["candidates"] = [dict(tampered["candidates"][0]), dict(tampered["candidates"][0])]
    result = verify_receipt(receipt_from_dict(tampered))
    assert not result.ok
    assert any("not unique" in e for e in result.errors)


def test_receipt_is_byte_identical_across_builds_with_same_key() -> None:
    key = Ed25519PrivateKey.generate()
    a = _build_signed(key)
    b = _build_signed(key)
    assert canonical_receipt_bytes(a) == canonical_receipt_bytes(b)
    assert a.signature_b64 == b.signature_b64
    assert receipt_to_dict(a) == receipt_to_dict(b)


def test_dict_roundtrip_preserves_verification() -> None:
    key = Ed25519PrivateKey.generate()
    signed = _build_signed(key)
    restored = receipt_from_dict(receipt_to_dict(signed))
    assert verify_receipt(restored).ok


def test_single_byte_mutation_fails_verification() -> None:
    key = Ed25519PrivateKey.generate()
    signed = _build_signed(key)
    tampered = receipt_to_dict(signed)
    tampered["winner_task_id"] = "candidate-0"  # lie about the winner
    result = verify_receipt(receipt_from_dict(tampered))
    assert not result.ok


def test_winner_digest_tamper_is_caught() -> None:
    key = Ed25519PrivateKey.generate()
    signed = _build_signed(key)
    tampered = receipt_to_dict(signed)
    tampered["winner_snapshot_digest"] = "d" * 64
    result = verify_receipt(receipt_from_dict(tampered))
    assert not result.ok


def test_unsigned_receipt_does_not_verify() -> None:
    key = Ed25519PrivateKey.generate()
    receipt = build_selection_receipt(
        base_snapshot_digest="a" * 64,
        candidates=[_cand("candidate-0", "b" * 64)],
        winner_task_id="candidate-0",
        ranker_profile={},
        public_key=key.public_key(),
    )
    assert not verify_receipt(receipt).ok


def test_nan_score_is_refused_at_build() -> None:
    key = Ed25519PrivateKey.generate()
    with pytest.raises((ValueError, SelectionReceiptError)):
        build_selection_receipt(
            base_snapshot_digest="a" * 64,
            candidates=[RaceCandidate("candidate-0", "b" * 64, {"correctness": float("nan")})],
            winner_task_id="candidate-0",
            ranker_profile={},
            public_key=key.public_key(),
        )


def test_unknown_winner_is_rejected() -> None:
    key = Ed25519PrivateKey.generate()
    with pytest.raises(SelectionReceiptError):
        build_selection_receipt(
            base_snapshot_digest="a" * 64,
            candidates=[_cand("candidate-0", "b" * 64)],
            winner_task_id="candidate-9",
            ranker_profile={},
            public_key=key.public_key(),
        )


def test_snapshot_digests_covers_base_and_all_candidates() -> None:
    key = Ed25519PrivateKey.generate()
    signed = _build_signed(key)
    digs = snapshot_digests(signed)
    assert "a" * 64 in digs  # base
    assert "b" * 64 in digs  # winner
    assert "c" * 64 in digs  # loser


def test_write_and_read_roundtrip(tmp_path) -> None:
    key = Ed25519PrivateKey.generate()
    signed = _build_signed(key)
    path = tmp_path / "receipt.json"
    write_receipt(path, signed)
    loaded = read_receipt_file(path)
    assert loaded is not None
    assert verify_receipt(loaded).ok


def test_signing_key_is_created_and_reused(tmp_path) -> None:
    key_path = tmp_path / "keys" / "selection.key"
    k1 = load_or_create_signing_key(key_path)
    k2 = load_or_create_signing_key(key_path)
    # Same key material reused -> same public bytes.
    assert k1.public_key().public_bytes_raw() == k2.public_key().public_bytes_raw()


# ---------------------------------------------------------------------------
# #2706 item 2: the Ed25519 signature as the SOLE line of defence
# ---------------------------------------------------------------------------


def test_only_the_signature_is_wrong_and_verification_fails() -> None:
    """A receipt that is internally consistent in every respect *except* the
    Ed25519 signature must fail verification, and the signature check must be
    the only thing that fails.

    This is the case #2706 item 2 says was missing: every other verify path
    (payload-digest recompute, keyid/embedded-key agreement, winner and loser
    cross-checks) passes, so removing the signature check entirely would leave
    the rest of the suite green. Here the signature is the last remaining
    guard, so the whole selection-receipt guarantee rides on it alone.
    """
    key = Ed25519PrivateKey.generate()
    signed = _build_signed(key)
    # Baseline: the genuine receipt verifies. The only delta below is the
    # signature bytes, so any failure must be attributable to them.
    assert verify_receipt(signed).ok, verify_receipt(signed).errors

    # Corrupt exactly one byte of the otherwise-genuine signature. It stays the
    # correct length and valid base64; the embedded public key, keyid, and
    # payload_digest are all left untouched and still describe `key`.
    data = receipt_to_dict(signed)
    raw = bytearray(base64.b64decode(data["signature_b64"]))
    raw[0] ^= 0x01
    data["signature_b64"] = base64.b64encode(bytes(raw)).decode("ascii")
    forged = receipt_from_dict(data)

    result = verify_receipt(forged)
    assert not result.ok
    # Sole line of defence: the signature is the *only* failing check. If any
    # earlier check also tripped, this receipt would not isolate the signature
    # as the guarantee, and the test would not pin item 2.
    assert result.errors == ("Ed25519 signature does not verify",)


# ---------------------------------------------------------------------------
# #2706 item 3: score_vector keys are canonically sorted (non-alpha fixture)
# ---------------------------------------------------------------------------


def test_to_dict_sorts_non_alphabetical_score_vector_keys() -> None:
    """``RaceCandidate.to_dict`` must emit the score-vector keys in sorted
    order regardless of the caller's insertion order.

    The fixture's keys are built in NON-alphabetical order on purpose. This is
    the revert-sensitive guard for the ``sorted()`` in ``to_dict`` (#2706 item
    3): drop that ``sorted()`` and the emitted order follows insertion order,
    failing this assertion. A digest/canonical-bytes comparison cannot catch
    the regression - ``json.dumps(sort_keys=True)`` re-sorts nested keys during
    serialisation and masks it - which is exactly why the pre-existing fixtures
    were vacuous.
    """
    unsorted = {"reversibility": 1.0, "correctness": 0.5, "cost": 0.25}
    # Guard the fixture itself: if a future edit tidies these into alphabetical
    # order, this test stops exercising the sort and must fail loudly here.
    assert list(unsorted) != sorted(unsorted)

    cand = RaceCandidate(
        task_id="candidate-0",
        terminal_snapshot_digest="b" * 64,
        score_vector=unsorted,
    )
    emitted = list(cand.to_dict()["score_vector"].keys())
    assert emitted == ["correctness", "cost", "reversibility"]


def test_canonical_digest_is_independent_of_score_vector_key_order() -> None:
    """A receipt built from a non-alphabetical score_vector is byte-identical
    to its sorted twin: two operators who assembled the same score vector in
    different key orders sign the same canonical envelope and payload digest.

    (Order-independence here is defence-in-depth from both the ``to_dict``
    sort and ``json.dumps(sort_keys=True)``; the single-line revert guard for
    the ``to_dict`` sort lives in the test above.)
    """
    key = Ed25519PrivateKey.generate()

    def _build(score_vector: dict[str, float]) -> SelectionReceipt:
        return build_selection_receipt(
            base_snapshot_digest="a" * 64,
            candidates=[RaceCandidate("candidate-0", "b" * 64, score_vector)],
            winner_task_id="candidate-0",
            ranker_profile={"method": "topsis", "criteria": []},
            public_key=key.public_key(),
        )

    unsorted = _build({"reversibility": 1.0, "correctness": 0.5, "cost": 0.25})
    sorted_twin = _build({"correctness": 0.5, "cost": 0.25, "reversibility": 1.0})

    assert canonical_receipt_bytes(unsorted) == canonical_receipt_bytes(sorted_twin)
    assert unsorted.payload_digest == sorted_twin.payload_digest
