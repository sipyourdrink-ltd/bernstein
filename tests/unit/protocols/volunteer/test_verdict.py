"""Tests for the volunteer verification verdict protocol document.

Coverage:

* VerificationVerdict construction — valid inputs, field validation.
* to_canonical_dict round-trip (dict → VerificationVerdict → dict).
* digest() returns SHA-256 of canonical_bytes.
* build_verdict_envelope → verify_verdict_envelope round-trip with correct key.
* verification fails with wrong key.
* validation rejects invalid recommendation values.
* validation rejects invalid gate_results structure.
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
from bernstein.core.protocols.volunteer.verdict import (
    VerdictError,
    VerificationVerdict,
    build_verdict_envelope,
    verify_verdict_envelope,
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
def sample_verdict() -> VerificationVerdict:
    return VerificationVerdict(
        submission_digest="a1b2c3d4e5f678901234567890abcdef1234567890abcdef1234567890abcdef12",
        gate_results=[
            {"command": "test_build", "passed": True},
            {"command": "test_lint", "passed": False},
        ],
        verifier_keyid="verifier-42",
        recommendation="accept",
        verified_at="2024-06-15T10:30:00+00:00",
        notes="Initial review passed",
    )


# ---------------------------------------------------------------------------
# Construction — valid inputs
# ---------------------------------------------------------------------------


class TestVerificationVerdictConstruction:
    """Valid inputs pass __post_init__ without error."""

    def test_minimal_valid(self) -> None:
        v = VerificationVerdict(
            submission_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234567890",
            gate_results=[{"command": "test_build", "passed": True}],
            verifier_keyid="verifier-42",
            recommendation="accept",
            verified_at="2024-06-15T10:30:00Z",
            notes="Minimal",
        )
        assert v.submission_digest == "d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234567890"
        assert v.verifier_keyid == "verifier-42"
        assert v.recommendation == "accept"
        assert v.notes == "Minimal"

    def test_non_empty_strings(self, sample_verdict: VerificationVerdict) -> None:
        """All required string fields must be non-empty."""
        v = sample_verdict
        for name in ["submission_digest", "verifier_keyid", "recommendation", "verified_at"]:
            assert v.__getattribute__(name) != "", f"{name} must be non-empty"

    def test_notes_optional(self) -> None:
        v = VerificationVerdict(
            submission_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234567890",
            gate_results=[{"command": "test_build", "passed": True}],
            verifier_keyid="verifier-42",
            recommendation="accept",
            verified_at="2024-06-15T10:30:00+00:00",
        )
        assert v.notes is None

        v2 = VerificationVerdict(
            submission_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234567890",
            gate_results=[{"command": "test_build", "passed": True}],
            verifier_keyid="verifier-42",
            recommendation="accept",
            verified_at="2024-06-15T10:30:00+00:00",
            notes="Human readable note",
        )
        assert v2.notes == "Human readable note"


class TestVerdictErrorAttributes:
    """VerdictError exposes field and reason."""

    def test_error_message(self) -> None:
        err = VerdictError("submission_digest", "must be non-empty")
        assert err.field == "submission_digest"
        assert err.reason == "must be non-empty"
        assert "submission_digest" in str(err)
        assert "must be non-empty" in str(err)


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------


class TestCanonicalForm:
    """to_canonical_dict round-trip and digest stability."""

    def test_round_trip(self, sample_verdict: VerificationVerdict) -> None:
        d = sample_verdict.to_canonical_dict()
        rebuilt = VerificationVerdict(**d)
        assert rebuilt == sample_verdict

    def test_digest_stability(self, sample_verdict: VerificationVerdict) -> None:
        d = sample_verdict.to_canonical_dict()
        expected = canonical_hash(d)
        assert sample_verdict.digest() == expected


# ---------------------------------------------------------------------------
# Envelope sign → verify round-trip
# ---------------------------------------------------------------------------


class TestEnvelopeRoundTrip:
    """build_verdict_envelope → verify_verdict_envelope."""

    def test_correct_key_passes(self, signing_key: Ed25519PrivateKey, sample_verdict: VerificationVerdict) -> None:
        envelope = build_verdict_envelope(sample_verdict, signing_key)
        result = verify_verdict_envelope(envelope, signing_key.public_key())
        assert result.ok is True
        assert result.verdict == sample_verdict.to_canonical_dict()
        assert result.keyid != ""

    def test_wrong_key_fails(
        self,
        signing_key: Ed25519PrivateKey,
        other_key: Ed25519PrivateKey,
        sample_verdict: VerificationVerdict,
    ) -> None:
        envelope = build_verdict_envelope(sample_verdict, signing_key)
        result = verify_verdict_envelope(envelope, other_key.public_key())
        assert result.ok is False
        assert len(result.errors) > 0

    def test_tampered_payload_fails(self, signing_key: Ed25519PrivateKey, sample_verdict: VerificationVerdict) -> None:
        envelope = build_verdict_envelope(sample_verdict, signing_key)
        payload_b64 = envelope.payload_b64
        # Flip first character to corrupt the payload.
        flipped = "A" if payload_b64[0] != "A" else "B"
        tampered = Envelope(
            payload_type=envelope.payload_type,
            payload_b64=flipped + payload_b64[1:],
            signatures=envelope.signatures,
        )
        result = verify_verdict_envelope(tampered, signing_key.public_key())
        assert result.ok is False

    def test_persisted_envelope_roundtrips(
        self, signing_key: Ed25519PrivateKey, sample_verdict: VerificationVerdict
    ) -> None:
        """Envelope serialised to JSON and re-parsed still verifies."""
        envelope = build_verdict_envelope(sample_verdict, signing_key)
        raw = envelope.to_json()
        reparsed = parse_envelope(json.loads(raw))
        result = verify_verdict_envelope(reparsed, signing_key.public_key())
        assert result.ok is True
        assert result.verdict == sample_verdict.to_canonical_dict()


# ---------------------------------------------------------------------------
# Validation rejects invalid recommendation values
# ---------------------------------------------------------------------------


class TestRecommendationValidation:
    """VerdictError rejects invalid recommendation enum values."""

    def test_invalid_values(self) -> None:
        invalid = ["approve", "request", "ACCEPT", "ACCEPTED", "PENDING"]
        for val in invalid:
            with pytest.raises(VerdictError) as exc_info:
                VerificationVerdict(
                    submission_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234567890",
                    gate_results=[{"command": "test_build", "passed": True}],
                    verifier_keyid="verifier-42",
                    recommendation=val,
                    verified_at="2024-06-15T10:30:00+00:00",
                    notes="Invalid",
                )
            assert "recommendation" in str(exc_info.value)
            assert "must be one of" in str(exc_info.value)

    def test_empty_recommendation_rejected(self) -> None:
        with pytest.raises(VerdictError) as exc_info:
            VerificationVerdict(
                submission_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234567890",
                gate_results=[{"command": "test_build", "passed": True}],
                verifier_keyid="verifier-42",
                recommendation="",
                verified_at="2024-06-15T10:30:00+00:00",
                notes="Empty",
            )
        assert "recommendation" in str(exc_info.value)
        assert "must be non-empty" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Validation rejects invalid gate_results structure
# ---------------------------------------------------------------------------


class TestGateResultsValidation:
    """VerdictError rejects invalid gate_results structure."""

    def test_not_list(self, sample_verdict: VerificationVerdict) -> None:
        with pytest.raises(VerdictError) as exc_info:
            sample_verdict.__class__(
                submission_digest=sample_verdict.submission_digest,
                gate_results="not a list",  # type: ignore[arg-type]
                verifier_keyid=sample_verdict.verifier_keyid,
                recommendation=sample_verdict.recommendation,
                verified_at=sample_verdict.verified_at,
                notes=sample_verdict.notes,
            )
        assert "gate_results" in str(exc_info.value)
        assert "expected list" in str(exc_info.value)

    def test_entry_not_dict(self, sample_verdict: VerificationVerdict) -> None:
        with pytest.raises(VerdictError) as exc_info:
            sample_verdict.__class__(
                submission_digest=sample_verdict.submission_digest,
                gate_results=[123],  # type: ignore[arg-type]
                verifier_keyid=sample_verdict.verifier_keyid,
                recommendation=sample_verdict.recommendation,
                verified_at=sample_verdict.verified_at,
                notes=sample_verdict.notes,
            )
        assert "gate_results[0]" in str(exc_info.value)
        assert "expected dict" in str(exc_info.value)

    def test_missing_command(self, sample_verdict: VerificationVerdict) -> None:
        with pytest.raises(VerdictError) as exc_info:
            sample_verdict.__class__(
                submission_digest=sample_verdict.submission_digest,
                gate_results=[{"passed": True}],  # type: ignore[arg-type]
                verifier_keyid=sample_verdict.verifier_keyid,
                recommendation=sample_verdict.recommendation,
                verified_at=sample_verdict.verified_at,
                notes=sample_verdict.notes,
            )
        assert "gate_results[0]" in str(exc_info.value)
        assert "missing 'command'" in str(exc_info.value)

    def test_missing_passed(self, sample_verdict: VerificationVerdict) -> None:
        with pytest.raises(VerdictError) as exc_info:
            sample_verdict.__class__(
                submission_digest=sample_verdict.submission_digest,
                gate_results=[{"command": "test_build"}],  # type: ignore[arg-type]
                verifier_keyid=sample_verdict.verifier_keyid,
                recommendation=sample_verdict.recommendation,
                verified_at=sample_verdict.verified_at,
                notes=sample_verdict.notes,
            )
        assert "gate_results[0]" in str(exc_info.value)
        assert "missing 'passed'" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Conformance harness
# ---------------------------------------------------------------------------


def assert_conformance(doc: VerificationVerdict, harness) -> VerificationVerdict:
    """Helper that asserts canonical round-trip through hub and GitHub."""
    from bernstein.core.protocols.volunteer.conformance import (
        from_github_projection,
        from_hub_projection,
        to_github_projection,
        to_hub_projection,
    )

    # Hub round-trip
    doc_dict = doc.to_canonical_dict()
    hub_bytes = to_hub_projection(doc_dict)
    hub_parsed = from_hub_projection(hub_bytes)
    assert canonical_hash(hub_parsed) == canonical_hash(doc_dict)

    # GitHub round-trip
    gh_comment = to_github_projection(doc_dict)
    gh_parsed = from_github_projection(gh_comment)
    assert canonical_hash(gh_parsed) == canonical_hash(doc_dict)

    return doc


def test_conformance_harness(sample_verdict: VerificationVerdict) -> None:
    """Full conformance test using the harness."""
    from bernstein.core.protocols.volunteer.conformance import ConformanceHarness

    harness = ConformanceHarness()
    harness.register(
        name="VerificationVerdict",
        to_canonical_dict=lambda v: v.to_canonical_dict(),
        from_canonical_dict=lambda d: VerificationVerdict(**d),
    )

    result = harness.check("VerificationVerdict", sample_verdict)
    assert result.ok
