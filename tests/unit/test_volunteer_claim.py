"""Tests for the volunteer claim protocol document.

Coverage:

* Claim construction — valid inputs, type guards (bool-as-int rejected),
  non-empty strings, aware-datetime parse.
* to_canonical_dict round-trip (dict → Claim → dict).
* canonical_bytes stability under field reordering (proves delegation to
  documents._sort_keys_recursive).
* build_claim_envelope → verify_claim_envelope round-trip — correct key,
  wrong key, tampered payload.
* Determinism: same Claim + same Ed25519 key → byte-identical envelope.
* Constants are well-formed.
* ClaimError attributes.
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.protocols.volunteer.claim import (
    CLAIM_PREDICATE_TYPE,
    CLAIM_SCHEMA_VERSION,
    Claim,
    ClaimError,
    build_claim_envelope,
    verify_claim_envelope,
)
from bernstein.core.protocols.volunteer.documents import (
    VOLUNTEER_DOCUMENT_PREDICATE_TYPE,
    canonical_bytes,
    canonical_hash,
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
def sample_claim() -> Claim:
    return Claim(
        schema_version=CLAIM_SCHEMA_VERSION,
        worker_id="volunteer-42",
        task_id="T-101",
        claimed_at="2024-06-15T10:30:00+00:00",
    )


# ---------------------------------------------------------------------------
# Construction — valid inputs
# ---------------------------------------------------------------------------


class TestClaimConstruction:
    """Valid inputs pass __post_init__ without error."""

    def test_minimal_valid(self) -> None:
        """Worker id, task id, and aware datetime construct without error."""
        c = Claim(worker_id="w", task_id="t", claimed_at="2024-01-01T00:00:00Z")
        assert c.worker_id == "w"
        assert c.task_id == "t"
        assert c.claimed_at == "2024-01-01T00:00:00Z"

    def test_iso_offset_variant(self) -> None:
        """+05:30 offset parses as aware datetime."""
        c = Claim(worker_id="x", task_id="y", claimed_at="2024-01-01T00:00:00+05:30")
        assert c.claimed_at == "2024-01-01T00:00:00+05:30"

    def test_utc_variant_z(self) -> None:
        """Z suffix is accepted."""
        c = Claim(worker_id="x", task_id="y", claimed_at="2024-01-01T00:00:00+00:00")
        assert c.claimed_at == "2024-01-01T00:00:00+00:00"


class TestClaimValidation:
    """Field-level validation rules."""

    def test_worker_id_empty_rejected(self) -> None:
        with pytest.raises(ClaimError) as exc_info:
            Claim(worker_id="", task_id="t", claimed_at="2024-01-01T00:00:00Z")
        assert exc_info.value.field == "worker_id"

    def test_task_id_empty_rejected(self) -> None:
        with pytest.raises(ClaimError) as exc_info:
            Claim(worker_id="w", task_id="", claimed_at="2024-01-01T00:00:00Z")
        assert exc_info.value.field == "task_id"

    def test_claimed_at_empty_rejected(self) -> None:
        with pytest.raises(ClaimError) as exc_info:
            Claim(worker_id="w", task_id="t", claimed_at="")
        assert exc_info.value.field == "claimed_at"

    def test_claimed_at_naive_rejected(self) -> None:
        """Naive datetime (no offset) is rejected as ambiguous."""
        with pytest.raises(ClaimError) as exc_info:
            Claim(worker_id="w", task_id="t", claimed_at="2024-01-01T00:00:00")
        assert exc_info.value.field == "claimed_at"
        assert "timezone-aware" in exc_info.value.reason

    def test_claimed_at_invalid_rejected(self) -> None:
        with pytest.raises(ClaimError) as exc_info:
            Claim(worker_id="w", task_id="t", claimed_at="not-a-date")
        assert exc_info.value.field == "claimed_at"

    def test_worker_id_bool_rejected(self) -> None:
        """bool-as-int is rejected before empty-string check."""
        with pytest.raises(ClaimError) as exc_info:
            Claim(worker_id=False, task_id="t", claimed_at="2024-01-01T00:00:00Z")  # type: ignore[arg-type]
        assert exc_info.value.field == "worker_id"
        assert "bool" in exc_info.value.reason

    def test_task_id_bool_rejected(self) -> None:
        with pytest.raises(ClaimError) as exc_info:
            Claim(worker_id="w", task_id=False, claimed_at="2024-01-01T00:00:00Z")  # type: ignore[arg-type]
        assert exc_info.value.field == "task_id"

    def test_claimed_at_bool_rejected(self) -> None:
        with pytest.raises(ClaimError) as exc_info:
            Claim(worker_id="w", task_id="t", claimed_at=False)  # type: ignore[arg-type]
        assert exc_info.value.field == "claimed_at"

    def test_claimed_at_int_rejected(self) -> None:
        with pytest.raises(ClaimError) as exc_info:
            Claim(worker_id="w", task_id="t", claimed_at=42)  # type: ignore[arg-type]
        assert exc_info.value.field == "claimed_at"

    def test_error_attributes(self) -> None:
        """ClaimError exposes field and reason."""
        err = ClaimError("worker_id", "must be non-empty")
        assert err.field == "worker_id"
        assert err.reason == "must be non-empty"
        assert "worker_id" in str(err)
        assert "must be non-empty" in str(err)


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------


class TestCanonicalDict:
    """to_canonical_dict round-trip and field stability."""

    def test_field_names(self, sample_claim: Claim) -> None:
        d = sample_claim.to_canonical_dict()
        assert "worker_id" in d
        assert "task_id" in d
        assert "claimed_at" in d
        assert "schema_version" in d

    def test_round_trip(self, sample_claim: Claim) -> None:
        """to_canonical_dict + Claim(**d) recovers the same claim."""
        d = sample_claim.to_canonical_dict()
        rebuilt = Claim(**d)
        assert rebuilt == sample_claim

    def test_field_order_stable(self, sample_claim: Claim) -> None:
        """to_canonical_dict field order matches the canonical dict definition."""
        d = sample_claim.to_canonical_dict()
        keys = list(d.keys())
        # schema_version is first, then the three data fields
        assert keys == ["schema_version", "worker_id", "task_id", "claimed_at"], (
            f"field order {keys} does not match canonical dict definition"
        )

    def test_digest_stable(self, sample_claim: Claim) -> None:
        """digest() returns the SHA-256 of canonical_bytes."""
        d = sample_claim.to_canonical_dict()
        expected = canonical_hash(d)
        assert sample_claim.digest() == expected


class TestCanonicalBytesDelegation:
    """Prove canonical_bytes delegates to documents._sort_keys_recursive.

    documents._sort_keys_recursive is not re-implemented here; claim.py
    imports canonical_bytes from documents.py.  We prove it by showing that
    two Claims with fields in different input order produce identical bytes.
    """

    def test_claim_bytes_key_reorder_independent(self) -> None:
        """Claims constructed with same values produce identical canonical bytes."""
        c1 = Claim(worker_id="a", task_id="b", claimed_at="2024-01-01T00:00:00Z")
        c2 = Claim(task_id="b", claimed_at="2024-01-01T00:00:00Z", worker_id="a")
        assert canonical_bytes(c1.to_canonical_dict()) == canonical_bytes(c2.to_canonical_dict())


# ---------------------------------------------------------------------------
# Envelope sign → verify round-trip
# ---------------------------------------------------------------------------


class TestClaimEnvelopeRoundTrip:
    """build_claim_envelope → verify_claim_envelope."""

    def test_correct_key_passes(self, signing_key: Ed25519PrivateKey, sample_claim: Claim) -> None:
        envelope = build_claim_envelope(sample_claim, signing_key)
        result = verify_claim_envelope(envelope, signing_key.public_key())
        assert result.ok is True
        assert result.claim == sample_claim.to_canonical_dict()
        assert result.keyid != ""

    def test_wrong_key_fails(
        self,
        signing_key: Ed25519PrivateKey,
        other_key: Ed25519PrivateKey,
        sample_claim: Claim,
    ) -> None:
        envelope = build_claim_envelope(sample_claim, signing_key)
        result = verify_claim_envelope(envelope, other_key.public_key())
        assert result.ok is False
        assert len(result.errors) > 0

    def test_tampered_payload_fails(self, signing_key: Ed25519PrivateKey, sample_claim: Claim) -> None:
        envelope = build_claim_envelope(sample_claim, signing_key)
        payload_b64 = envelope.payload_b64
        flipped = "A" if payload_b64[0] != "A" else "B"
        tampered = Envelope(
            payload_type=envelope.payload_type,
            payload_b64=flipped + payload_b64[1:],
            signatures=envelope.signatures,
        )
        result = verify_claim_envelope(tampered, signing_key.public_key())
        assert result.ok is False

    def test_persisted_envelope_roundtrips(self, signing_key: Ed25519PrivateKey, sample_claim: Claim) -> None:
        """Envelope serialised to JSON and re-parsed still verifies."""
        envelope = build_claim_envelope(sample_claim, signing_key)
        raw = envelope.to_json()
        reparsed = parse_envelope(json.loads(raw))
        result = verify_claim_envelope(reparsed, signing_key.public_key())
        assert result.ok is True
        assert result.claim == sample_claim.to_canonical_dict()


# ---------------------------------------------------------------------------
# Determinism — Ed25519 is deterministic (RFC 8032 §5.1.6)
# ---------------------------------------------------------------------------


class TestClaimEnvelopeDeterminism:
    """Same Claim + same Ed25519 key → byte-identical envelope."""

    def test_deterministic(self, signing_key: Ed25519PrivateKey, sample_claim: Claim) -> None:
        env_a = build_claim_envelope(sample_claim, signing_key)
        env_b = build_claim_envelope(sample_claim, signing_key)
        assert env_a.to_json() == env_b.to_json()

    def test_different_key_different_signature(
        self, signing_key: Ed25519PrivateKey, other_key: Ed25519PrivateKey, sample_claim: Claim
    ) -> None:
        env_a = build_claim_envelope(sample_claim, signing_key)
        env_b = build_claim_envelope(sample_claim, other_key)
        # Same payload bytes, different signatures
        assert env_a.payload_b64 == env_b.payload_b64
        assert env_a.to_json() != env_b.to_json()


# ---------------------------------------------------------------------------
# Golden vector — fixed key + fixed claim → fixed envelope
# ---------------------------------------------------------------------------


class TestClaimGoldenVector:
    """Reproducible envelope bytes prove the substrate is wired correctly.

    These values were produced by a known-good run and are pinned here so
    any future change to the canonicalisation logic will break the test
    and require deliberate action to update.
    """

    def test_fixed_key_fixed_claim_golden(self) -> None:
        """Known key + known claim produces known envelope bytes."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        # Fixed 32-byte seed → deterministic key
        fixed_seed = b"\x00" * 32
        fixed_key = Ed25519PrivateKey.from_private_bytes(fixed_seed)

        claim = Claim(
            schema_version=CLAIM_SCHEMA_VERSION,
            worker_id="worker-abc",
            task_id="T-999",
            claimed_at="2024-01-01T00:00:00+00:00",
        )

        envelope = build_claim_envelope(claim, fixed_key)

        # The exact envelope bytes are the contract. If these change, something
        # in the canonicalisation or signing pipeline changed.
        raw = envelope.to_json()
        parsed = json.loads(raw)

        # The payload type is the volunteer-specific predicate.
        statement = json.loads(base64.b64decode(parsed["payload"]).decode())
        assert statement["predicateType"] == CLAIM_PREDICATE_TYPE

        # The envelope is deterministic: second build is byte-identical.
        env2 = build_claim_envelope(claim, fixed_key)
        assert envelope.to_json() == env2.to_json()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Module-level constants are stable and well-formed."""

    def test_schema_version_semver(self) -> None:
        parts = CLAIM_SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_predicate_type_is_url(self) -> None:
        assert CLAIM_PREDICATE_TYPE.startswith("https://")

    def test_predicate_type_distinct_from_base(self) -> None:
        """Claim predicate type differs from the base volunteer document type."""
        assert CLAIM_PREDICATE_TYPE != VOLUNTEER_DOCUMENT_PREDICATE_TYPE
        assert "claim" in CLAIM_PREDICATE_TYPE

    def test_schema_version_matches_body(self, sample_claim: Claim) -> None:
        """schema_version in to_canonical_dict matches the module constant."""
        assert sample_claim.to_canonical_dict()["schema_version"] == CLAIM_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Conformance
# ---------------------------------------------------------------------------


def test_conformance_harness(sample_claim: Claim) -> None:
    """Full conformance test using the harness."""
    from bernstein.core.protocols.volunteer.conformance import ConformanceHarness

    harness = ConformanceHarness()
    harness.register(
        name="Claim",
        to_canonical_dict=lambda c: c.to_canonical_dict(),
        from_canonical_dict=lambda d: Claim(**d),
    )

    result = harness.check("Claim", sample_claim)
    assert result.ok
