"""Tests for the volunteer merge receipt protocol document.

Coverage:

* MergeReceipt construction — valid inputs, field validation.
* to_canonical_dict round-trip (dict → MergeReceipt → dict).
* digest() returns SHA-256 of canonical_bytes.
* build_merge_receipt_envelope → verify_merge_receipt_envelope round-trip.
* verification fails with wrong key.
* reward structure validation.
* task_id field present.
* conformance: round-trips through hub + GitHub projections.
"""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from bernstein.core.protocols.volunteer.documents import (
    canonical_hash,
)
from bernstein.core.protocols.volunteer.receipt import (
    RECEIPT_PREDICATE_TYPE,
    RECEIPT_SCHEMA_VERSION,
    MergeReceipt,
    MergeReceiptError,
    build_merge_receipt_envelope,
    verify_merge_receipt_envelope,
)
from bernstein.core.security.audit_dsse import Envelope, parse_envelope

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def other_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def sample_receipt() -> MergeReceipt:
    return MergeReceipt(
        submission_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234567890",
        merged_by_keyid="maintainer-42",
        merged_at="2024-06-15T10:30:00+00:00",
        reward={"kind": "points", "amount": 100, "label": "First contribution"},
        task_id="T-101",
        pr_number=42,
        notes="Thanks for the contribution!",
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestMergeReceiptConstruction:
    """Valid inputs pass __post_init__ without error."""

    def test_minimal_valid(self) -> None:
        """Minimal receipt with only required fields."""
        r = MergeReceipt(
            submission_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234567890",
            merged_by_keyid="maintainer-42",
            merged_at="2024-06-15T10:30:00+00:00",
            reward={"kind": "badge", "amount": None, "label": "First PR"},
            task_id="T-101",
        )
        assert r.submission_digest.startswith("d4e5f6")
        assert r.merged_by_keyid == "maintainer-42"
        assert r.task_id == "T-101"
        assert r.pr_number is None
        assert r.notes is None

    def test_all_fields(self, sample_receipt: MergeReceipt) -> None:
        r = sample_receipt
        assert r.submission_digest == "d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234567890"
        assert r.merged_by_keyid == "maintainer-42"
        assert r.reward == {"kind": "points", "amount": 100, "label": "First contribution"}
        assert r.task_id == "T-101"
        assert r.pr_number == 42
        assert r.notes == "Thanks for the contribution!"

    def test_bool_rejected_for_submission_digest(self, sample_receipt: MergeReceipt) -> None:
        with pytest.raises(MergeReceiptError) as exc_info:
            MergeReceipt(
                submission_digest=False,  # type: ignore[arg-type]
                merged_by_keyid=sample_receipt.merged_by_keyid,
                merged_at=sample_receipt.merged_at,
                reward=sample_receipt.reward,
                task_id=sample_receipt.task_id,
            )
        assert exc_info.value.field == "submission_digest"


class TestMergeReceiptErrorAttributes:
    """MergeReceiptError exposes field and reason."""

    def test_error_message(self) -> None:
        err = MergeReceiptError("task_id", "must be non-empty")
        assert err.field == "task_id"
        assert err.reason == "must be non-empty"


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------


class TestCanonicalForm:
    """to_canonical_dict round-trip and digest stability."""

    def test_round_trip(self, sample_receipt: MergeReceipt) -> None:
        d = sample_receipt.to_canonical_dict()
        rebuilt = MergeReceipt(**d)
        assert rebuilt == sample_receipt

    def test_digest_stability(self, sample_receipt: MergeReceipt) -> None:
        d = sample_receipt.to_canonical_dict()
        expected = canonical_hash(d)
        assert sample_receipt.digest() == expected

    def test_optional_fields_in_canonical(self, sample_receipt: MergeReceipt) -> None:
        d = sample_receipt.to_canonical_dict()
        assert "pr_number" in d
        assert "notes" in d

    def test_optional_fields_excluded_when_none(self) -> None:
        r = MergeReceipt(
            submission_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234567890",
            merged_by_keyid="maintainer-42",
            merged_at="2024-06-15T10:30:00+00:00",
            reward={"kind": "bounty", "amount": 50.0, "label": "Bounty"},
            task_id="T-101",
        )
        d = r.to_canonical_dict()
        assert "pr_number" not in d
        assert "notes" not in d


# ---------------------------------------------------------------------------
# Envelope sign → verify round-trip
# ---------------------------------------------------------------------------


class TestEnvelopeRoundTrip:
    """build_merge_receipt_envelope → verify_merge_receipt_envelope."""

    def test_correct_key_passes(self, signing_key: Ed25519PrivateKey, sample_receipt: MergeReceipt) -> None:
        envelope = build_merge_receipt_envelope(sample_receipt, signing_key)
        result = verify_merge_receipt_envelope(envelope, signing_key.public_key())
        assert result.ok is True
        assert result.receipt == sample_receipt.to_canonical_dict()
        assert result.keyid != ""

    def test_wrong_key_fails(
        self,
        signing_key: Ed25519PrivateKey,
        other_key: Ed25519PrivateKey,
        sample_receipt: MergeReceipt,
    ) -> None:
        envelope = build_merge_receipt_envelope(sample_receipt, signing_key)
        result = verify_merge_receipt_envelope(envelope, other_key.public_key())
        assert result.ok is False
        assert len(result.errors) > 0

    def test_tampered_payload_fails(self, signing_key: Ed25519PrivateKey, sample_receipt: MergeReceipt) -> None:
        envelope = build_merge_receipt_envelope(sample_receipt, signing_key)
        payload_b64 = envelope.payload_b64
        flipped = "A" if payload_b64[0] != "A" else "B"
        tampered = Envelope(
            payload_type=envelope.payload_type,
            payload_b64=flipped + payload_b64[1:],
            signatures=envelope.signatures,
        )
        result = verify_merge_receipt_envelope(tampered, signing_key.public_key())
        assert result.ok is False

    def test_persisted_envelope_roundtrips(self, signing_key: Ed25519PrivateKey, sample_receipt: MergeReceipt) -> None:
        envelope = build_merge_receipt_envelope(sample_receipt, signing_key)
        raw = envelope.to_json()
        reparsed = parse_envelope(json.loads(raw))
        result = verify_merge_receipt_envelope(reparsed, signing_key.public_key())
        assert result.ok is True
        assert result.receipt == sample_receipt.to_canonical_dict()


# ---------------------------------------------------------------------------
# Reward structure validation
# ---------------------------------------------------------------------------


class TestRewardValidation:
    """MergeReceiptError rejects invalid reward structure."""

    def test_reward_not_dict(self, sample_receipt: MergeReceipt) -> None:
        with pytest.raises(MergeReceiptError) as exc_info:
            sample_receipt.__class__(
                submission_digest=sample_receipt.submission_digest,
                merged_by_keyid=sample_receipt.merged_by_keyid,
                merged_at=sample_receipt.merged_at,
                reward="not a dict",  # type: ignore[arg-type]
                task_id=sample_receipt.task_id,
            )
        assert "reward" in str(exc_info.value)
        assert "expected dict" in str(exc_info.value)

    def test_reward_missing_kind(self, sample_receipt: MergeReceipt) -> None:
        with pytest.raises(MergeReceiptError) as exc_info:
            sample_receipt.__class__(
                submission_digest=sample_receipt.submission_digest,
                merged_by_keyid=sample_receipt.merged_by_keyid,
                merged_at=sample_receipt.merged_at,
                reward={"amount": 100, "label": "test"},  # type: ignore[arg-type]
                task_id=sample_receipt.task_id,
            )
        assert "kind" in str(exc_info.value)

    def test_reward_invalid_kind(self, sample_receipt: MergeReceipt) -> None:
        with pytest.raises(MergeReceiptError) as exc_info:
            sample_receipt.__class__(
                submission_digest=sample_receipt.submission_digest,
                merged_by_keyid=sample_receipt.merged_by_keyid,
                merged_at=sample_receipt.merged_at,
                reward={"kind": "invalid", "amount": 100, "label": "test"},  # type: ignore[arg-type]
                task_id=sample_receipt.task_id,
            )
        assert "kind" in str(exc_info.value)
        assert "must be one of" in str(exc_info.value)

    def test_reward_missing_amount(self, sample_receipt: MergeReceipt) -> None:
        with pytest.raises(MergeReceiptError) as exc_info:
            sample_receipt.__class__(
                submission_digest=sample_receipt.submission_digest,
                merged_by_keyid=sample_receipt.merged_by_keyid,
                merged_at=sample_receipt.merged_at,
                reward={"kind": "points", "label": "test"},  # type: ignore[arg-type]
                task_id=sample_receipt.task_id,
            )
        assert "amount" in str(exc_info.value)

    def test_reward_amount_invalid_type(self, sample_receipt: MergeReceipt) -> None:
        with pytest.raises(MergeReceiptError) as exc_info:
            sample_receipt.__class__(
                submission_digest=sample_receipt.submission_digest,
                merged_by_keyid=sample_receipt.merged_by_keyid,
                merged_at=sample_receipt.merged_at,
                reward={"kind": "points", "amount": "100", "label": "test"},  # type: ignore[arg-type]
                task_id=sample_receipt.task_id,
            )
        assert "amount" in str(exc_info.value)

    def test_reward_amount_can_be_none(self, sample_receipt: MergeReceipt) -> None:
        r = sample_receipt.__class__(
            submission_digest=sample_receipt.submission_digest,
            merged_by_keyid=sample_receipt.merged_by_keyid,
            merged_at=sample_receipt.merged_at,
            reward={"kind": "badge", "amount": None, "label": "test"},  # type: ignore[arg-type]
            task_id=sample_receipt.task_id,
        )
        assert r.reward["amount"] is None

    def test_reward_missing_label(self, sample_receipt: MergeReceipt) -> None:
        with pytest.raises(MergeReceiptError) as exc_info:
            sample_receipt.__class__(
                submission_digest=sample_receipt.submission_digest,
                merged_by_keyid=sample_receipt.merged_by_keyid,
                merged_at=sample_receipt.merged_at,
                reward={"kind": "points", "amount": 100},  # type: ignore[arg-type]
                task_id=sample_receipt.task_id,
            )
        assert "label" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_schema_version_semver(self) -> None:
        parts = RECEIPT_SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_predicate_type_is_url(self) -> None:
        assert RECEIPT_PREDICATE_TYPE.startswith("https://")
        assert "receipt" in RECEIPT_PREDICATE_TYPE


# ---------------------------------------------------------------------------
# Conformance
# ---------------------------------------------------------------------------


def test_conformance_harness(sample_receipt: MergeReceipt) -> None:
    from bernstein.core.protocols.volunteer.conformance import (
        ConformanceHarness,
    )

    harness = ConformanceHarness()
    harness.register(
        name="MergeReceipt",
        to_canonical_dict=lambda r: r.to_canonical_dict(),
        from_canonical_dict=lambda d: MergeReceipt(**d),
    )

    result = harness.check("MergeReceipt", sample_receipt)
    assert result.ok
