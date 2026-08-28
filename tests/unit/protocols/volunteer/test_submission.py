"""Tests for the volunteer submission protocol document.

Coverage:

* Submission construction — valid inputs, field validation.
* to_canonical_dict round-trip (dict → Submission → dict).
* digest() returns SHA-256 of canonical_bytes.
* build_submission_envelope → verify_submission_envelope round-trip.
* verification fails with wrong key.
* task_ref is preserved in canonical dict.
* digest and location fields are present.
* validation rejects missing required fields.
* conformance: round-trips through hub + GitHub projections.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from bernstein.core.protocols.volunteer.documents import (
    VOLUNTEER_DOCUMENT_PREDICATE_TYPE,
    canonical_hash,
)
from bernstein.core.protocols.volunteer.submission import (
    SUBMISSION_PREDICATE_TYPE,
    SUBMISSION_SCHEMA_VERSION,
    Submission,
    SubmissionError,
    build_submission_envelope,
    verify_submission_envelope,
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
def sample_task_ref() -> dict[str, str]:
    return {
        "repo": "https://github.com/example/repo",
        "commit_sha": "abcdef1234567890abcdef1234567890abcdef12",
        "issue_number": "42",
    }


@pytest.fixture()
def sample_submission(sample_task_ref: dict[str, str]) -> Submission:
    return Submission(
        receipt_bundle_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234ab",
        receipt_bundle_location="https://github.com/example/repo/actions/runs/12345/artifacts/67890",
        task_ref=sample_task_ref,
        submitted_at="2024-06-15T10:30:00+00:00",
    )


# ---------------------------------------------------------------------------
# Construction — valid inputs
# ---------------------------------------------------------------------------


class TestSubmissionConstruction:
    """Valid inputs pass __post_init__ without error."""

    def test_all_fields(self, sample_submission: Submission) -> None:
        s = sample_submission
        assert s.receipt_bundle_digest == "d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234ab"
        assert s.receipt_bundle_location.startswith("https://")
        assert s.task_ref["repo"] == "https://github.com/example/repo"
        assert s.task_ref["issue_number"] == "42"
        assert s.submitted_at == "2024-06-15T10:30:00+00:00"
        assert s.schema_version == SUBMISSION_SCHEMA_VERSION

    def test_minimal_valid(self, sample_task_ref: dict[str, str]) -> None:
        """Minimal submission with only required fields."""
        s = Submission(
            receipt_bundle_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234ab",
            receipt_bundle_location="https://example.com/bundle.tar.gz",
            task_ref=sample_task_ref,
        )
        assert s.receipt_bundle_digest.startswith("d4e5f6")
        assert s.submitted_at == ""

    def test_issue_number_as_int_rejected(self, sample_task_ref: dict[str, str]) -> None:
        """task_ref.issue_number as int is rejected — must be str per spec."""
        ref = dict(sample_task_ref)
        ref["issue_number"] = 42  # type: ignore[dict-item]
        with pytest.raises(SubmissionError) as exc_info:
            Submission(
                receipt_bundle_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234ab",
                receipt_bundle_location="https://example.com/bundle.tar.gz",
                task_ref=ref,
            )
        assert exc_info.value.field == "task_ref.issue_number"

    def test_bool_rejected_for_digest(self, sample_task_ref: dict[str, str]) -> None:
        with pytest.raises(SubmissionError) as exc_info:
            Submission(
                receipt_bundle_digest=False,  # type: ignore[arg-type]
                receipt_bundle_location="https://example.com/bundle.tar.gz",
                task_ref=sample_task_ref,
            )
        assert exc_info.value.field == "receipt_bundle_digest"

    def test_invalid_hex_digest_too_short(self, sample_task_ref: dict[str, str]) -> None:
        with pytest.raises(SubmissionError) as exc_info:
            Submission(
                receipt_bundle_digest="abc123",
                receipt_bundle_location="https://example.com/bundle.tar.gz",
                task_ref=sample_task_ref,
            )
        assert exc_info.value.field == "receipt_bundle_digest"


class TestSubmissionErrorAttributes:
    """SubmissionError exposes field and reason."""

    def test_error_message(self) -> None:
        err = SubmissionError("receipt_bundle_digest", "must be a 64-char hex string")
        assert err.field == "receipt_bundle_digest"
        assert err.reason == "must be a 64-char hex string"
        assert "receipt_bundle_digest" in str(err)


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------


class TestCanonicalForm:
    """to_canonical_dict round-trip and digest stability."""

    def test_round_trip(self, sample_submission: Submission) -> None:
        d = sample_submission.to_canonical_dict()
        rebuilt = Submission(**d)
        assert rebuilt == sample_submission

    def test_digest_stability(self, sample_submission: Submission) -> None:
        d = sample_submission.to_canonical_dict()
        expected = canonical_hash(d)
        assert sample_submission.digest() == expected

    def test_task_ref_is_preserved_in_canonical_dict(self, sample_submission: Submission) -> None:
        d = sample_submission.to_canonical_dict()
        assert "task_ref" in d
        assert d["task_ref"]["repo"] == sample_submission.task_ref["repo"]
        assert d["task_ref"]["commit_sha"] == sample_submission.task_ref["commit_sha"]
        assert d["task_ref"]["issue_number"] == sample_submission.task_ref["issue_number"]

    def test_digest_and_location_fields_present(self, sample_submission: Submission) -> None:
        d = sample_submission.to_canonical_dict()
        assert "receipt_bundle_digest" in d
        assert "receipt_bundle_location" in d
        assert d["receipt_bundle_digest"] == sample_submission.receipt_bundle_digest
        assert d["receipt_bundle_location"] == sample_submission.receipt_bundle_location

    def test_optional_fields_in_canonical(self, sample_submission: Submission) -> None:
        d = sample_submission.to_canonical_dict()
        assert "submitted_at" in d
        assert d["submitted_at"] == sample_submission.submitted_at

    def test_optional_fields_excluded_when_none(self, sample_task_ref: dict[str, str]) -> None:
        s = Submission(
            receipt_bundle_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234ab",
            receipt_bundle_location="https://example.com/bundle.tar.gz",
            task_ref=sample_task_ref,
        )
        d = s.to_canonical_dict()
        assert "submitted_at" not in d


# ---------------------------------------------------------------------------
# Validation rejects missing required fields
# ---------------------------------------------------------------------------


class TestValidation:
    """SubmissionError rejects invalid or missing fields."""

    def test_missing_receipt_bundle_digest(self, sample_task_ref: dict[str, str]) -> None:
        with pytest.raises(SubmissionError) as exc_info:
            Submission(
                receipt_bundle_digest="",  # type: ignore[arg-type]
                receipt_bundle_location="https://example.com/bundle.tar.gz",
                task_ref=sample_task_ref,
            )
        assert exc_info.value.field == "receipt_bundle_digest"
        assert "must be non-empty" in exc_info.value.reason

    def test_missing_receipt_bundle_location(self, sample_task_ref: dict[str, str]) -> None:
        with pytest.raises(SubmissionError) as exc_info:
            Submission(
                receipt_bundle_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234ab",
                receipt_bundle_location="",  # type: ignore[arg-type]
                task_ref=sample_task_ref,
            )
        assert exc_info.value.field == "receipt_bundle_location"

    def test_missing_task_ref_repo(self, sample_task_ref: dict[str, str]) -> None:
        ref = dict(sample_task_ref)
        ref.pop("repo")  # type: ignore[arg-type]
        with pytest.raises(SubmissionError) as exc_info:
            Submission(
                receipt_bundle_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234ab",
                receipt_bundle_location="https://example.com/bundle.tar.gz",
                task_ref=ref,  # type: ignore[arg-type]
            )
        assert exc_info.value.field == "task_ref.repo"
        assert "required field missing" in exc_info.value.reason

    def test_missing_task_ref_commit_sha(self, sample_task_ref: dict[str, str]) -> None:
        ref = dict(sample_task_ref)
        ref.pop("commit_sha")  # type: ignore[arg-type]
        with pytest.raises(SubmissionError) as exc_info:
            Submission(
                receipt_bundle_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234ab",
                receipt_bundle_location="https://example.com/bundle.tar.gz",
                task_ref=ref,  # type: ignore[arg-type]
            )
        assert exc_info.value.field == "task_ref.commit_sha"

    def test_missing_task_ref_issue_number(self, sample_task_ref: dict[str, str]) -> None:
        ref = dict(sample_task_ref)
        ref.pop("issue_number")  # type: ignore[arg-type]
        with pytest.raises(SubmissionError) as exc_info:
            Submission(
                receipt_bundle_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234ab",
                receipt_bundle_location="https://example.com/bundle.tar.gz",
                task_ref=ref,  # type: ignore[arg-type]
            )
        assert exc_info.value.field == "task_ref.issue_number"

    def test_invalid_hex_digest(self, sample_task_ref: dict[str, str]) -> None:
        with pytest.raises(SubmissionError) as exc_info:
            Submission(
                receipt_bundle_digest="not-a-valid-digest",
                receipt_bundle_location="https://example.com/bundle.tar.gz",
                task_ref=sample_task_ref,
            )
        assert exc_info.value.field == "receipt_bundle_digest"
        assert "hex string" in exc_info.value.reason

    def test_naive_datetime_rejected(self, sample_task_ref: dict[str, str]) -> None:
        naive = datetime.now().isoformat()  # no tzinfo
        with pytest.raises(SubmissionError) as exc_info:
            Submission(
                receipt_bundle_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234ab",
                receipt_bundle_location="https://example.com/bundle.tar.gz",
                task_ref=sample_task_ref,
                submitted_at=naive,
            )
        assert exc_info.value.field == "submitted_at"
        assert "timezone-aware" in exc_info.value.reason

    def test_empty_task_ref_rejected(self, sample_task_ref: dict[str, str]) -> None:
        with pytest.raises(SubmissionError) as exc_info:
            Submission(
                receipt_bundle_digest="d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef1234ab",
                receipt_bundle_location="https://example.com/bundle.tar.gz",
                task_ref={},  # type: ignore[arg-type]
            )
        assert exc_info.value.field == "task_ref.repo"


# ---------------------------------------------------------------------------
# Envelope sign → verify round-trip
# ---------------------------------------------------------------------------


class TestEnvelopeRoundTrip:
    """build_submission_envelope → verify_submission_envelope."""

    def test_submission_roundtrip(self, signing_key: Ed25519PrivateKey, sample_submission: Submission) -> None:
        """Sign + verify succeeds with correct key."""
        envelope = build_submission_envelope(sample_submission, signing_key)
        result = verify_submission_envelope(envelope, signing_key.public_key())
        assert result.ok is True
        assert result.submission == sample_submission.to_canonical_dict()
        assert result.keyid != ""

    def test_submission_verification_fails_with_wrong_key(
        self,
        signing_key: Ed25519PrivateKey,
        other_key: Ed25519PrivateKey,
        sample_submission: Submission,
    ) -> None:
        envelope = build_submission_envelope(sample_submission, signing_key)
        result = verify_submission_envelope(envelope, other_key.public_key())
        assert result.ok is False
        assert len(result.errors) > 0

    def test_tampered_payload_fails(self, signing_key: Ed25519PrivateKey, sample_submission: Submission) -> None:
        envelope = build_submission_envelope(sample_submission, signing_key)
        payload_b64 = envelope.payload_b64
        flipped = "A" if payload_b64[0] != "A" else "B"
        tampered = Envelope(
            payload_type=envelope.payload_type,
            payload_b64=flipped + payload_b64[1:],
            signatures=envelope.signatures,
        )
        result = verify_submission_envelope(tampered, signing_key.public_key())
        assert result.ok is False

    def test_persisted_envelope_roundtrips(self, signing_key: Ed25519PrivateKey, sample_submission: Submission) -> None:
        envelope = build_submission_envelope(sample_submission, signing_key)
        raw = envelope.to_json()
        reparsed = parse_envelope(json.loads(raw))
        result = verify_submission_envelope(reparsed, signing_key.public_key())
        assert result.ok is True
        assert result.submission == sample_submission.to_canonical_dict()

    def test_predicate_type_is_submission_specific(
        self, signing_key: Ed25519PrivateKey, sample_submission: Submission
    ) -> None:
        envelope = build_submission_envelope(sample_submission, signing_key)
        statement = envelope.statement
        assert statement["predicateType"] == SUBMISSION_PREDICATE_TYPE
        assert statement["predicateType"] != VOLUNTEER_DOCUMENT_PREDICATE_TYPE

    def test_document_kind_is_submission(self, signing_key: Ed25519PrivateKey, sample_submission: Submission) -> None:
        envelope = build_submission_envelope(sample_submission, signing_key)
        predicate = envelope.statement["predicate"]
        assert predicate["document_kind"] == "submission"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_schema_version_semver(self) -> None:
        parts = SUBMISSION_SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_predicate_type_is_url(self) -> None:
        assert SUBMISSION_PREDICATE_TYPE.startswith("https://")
        assert "submission" in SUBMISSION_PREDICATE_TYPE


# ---------------------------------------------------------------------------
# Conformance
# ---------------------------------------------------------------------------


def test_conformance(sample_submission: Submission) -> None:
    """The submission canonical dict round-trips through both hub and GitHub projections."""
    from bernstein.core.protocols.volunteer.conformance import (
        assert_conformance,
    )

    assert_conformance(
        sample_submission,
        name="Submission",
        to_canonical_dict=lambda s: s.to_canonical_dict(),
        from_canonical_dict=lambda d: Submission(**d),
    )
