"""Tests for signed worker enrolment and claim receipts (#2547).

Covers the verifiability criterion: a claim is signed by the enrolled worker's
key id and is provable offline; a receipt made under a rotated install identity
names a key id absent from the current directory and fails deterministically.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from bernstein.core.identity.http_signing import build_key_directory
from bernstein.core.sandbox.pool_enrolment import (
    build_claim_receipt,
    build_enrolment_receipt,
    verify_claim_receipt,
    verify_enrolment_receipt,
)
from bernstein.core.security.agent_card_keystore import AgentCardKeystore

POOL_HASH = "a" * 64


@pytest.fixture
def keystore(tmp_path):
    return AgentCardKeystore(tmp_path / "keys")


class TestEnrolment:
    def test_signed_receipt_verifies(self, keystore):
        r = build_enrolment_receipt(keystore=keystore, pool_hash=POOL_HASH, worker_name="node-1", created=1700000000)
        assert verify_enrolment_receipt(r)
        assert verify_enrolment_receipt(r, key_directory=build_key_directory(keystore))

    def test_keyid_is_install_identity_thumbprint(self, keystore):
        from bernstein.core.identity.http_signing import install_identity_keyid

        _priv, pub = keystore.load_or_generate()
        r = build_enrolment_receipt(keystore=keystore, pool_hash=POOL_HASH, worker_name="n", created=1)
        assert r.keyid == install_identity_keyid(pub)

    def test_tampered_pool_hash_fails(self, keystore):
        r = build_enrolment_receipt(keystore=keystore, pool_hash=POOL_HASH, worker_name="n", created=1)
        forged = replace(r, pool_hash="b" * 64)  # signature no longer covers this body
        assert not verify_enrolment_receipt(forged)

    def test_rotated_identity_fails_against_current_directory(self, keystore):
        r = build_enrolment_receipt(keystore=keystore, pool_hash=POOL_HASH, worker_name="n", created=1)
        keystore.rotate()
        # The current directory (post-rotation, no archived) no longer lists the
        # key id the old receipt was signed under.
        current = build_key_directory(keystore, include_archived=False)
        assert not verify_enrolment_receipt(r, key_directory=current)

    def test_swapped_keyid_fails(self, keystore):
        r = build_enrolment_receipt(keystore=keystore, pool_hash=POOL_HASH, worker_name="n", created=1)
        forged = replace(r, keyid="not-the-embedded-thumbprint")
        assert not verify_enrolment_receipt(forged)

    def test_roundtrip_dict(self, keystore):
        from bernstein.core.sandbox.pool_enrolment import EnrolmentReceipt

        r = build_enrolment_receipt(keystore=keystore, pool_hash=POOL_HASH, worker_name="n", created=1)
        restored = EnrolmentReceipt.from_dict(r.to_dict())
        assert verify_enrolment_receipt(restored)
        assert restored.enrolment_hash() == r.enrolment_hash()


class TestClaim:
    def test_claim_signed_by_enrolled_key_verifies(self, keystore):
        enrol = build_enrolment_receipt(keystore=keystore, pool_hash=POOL_HASH, worker_name="n", created=1)
        claim = build_claim_receipt(
            keystore=keystore, pool_hash=POOL_HASH, task_id="t-1", placement_hash="c" * 64, created=2
        )
        assert verify_claim_receipt(claim, enrolled_keyid=enrol.keyid)

    def test_claim_from_other_key_rejected(self, keystore, tmp_path):
        enrol = build_enrolment_receipt(keystore=keystore, pool_hash=POOL_HASH, worker_name="n", created=1)
        other = AgentCardKeystore(tmp_path / "other-keys")
        claim = build_claim_receipt(
            keystore=other, pool_hash=POOL_HASH, task_id="t-1", placement_hash="c" * 64, created=2
        )
        # Claim is validly signed, but not by the enrolled worker key.
        assert verify_claim_receipt(claim)
        assert not verify_claim_receipt(claim, enrolled_keyid=enrol.keyid)

    def test_tampered_claim_fails(self, keystore):
        claim = build_claim_receipt(
            keystore=keystore, pool_hash=POOL_HASH, task_id="t-1", placement_hash="c" * 64, created=2
        )
        forged = replace(claim, task_id="t-2")
        assert not verify_claim_receipt(forged)
