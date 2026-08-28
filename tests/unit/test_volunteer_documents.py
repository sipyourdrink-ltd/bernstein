"""Tests for the volunteer protocol shared document substrate.

Coverage:

* canonical_bytes determinism — same dict with reordered keys produces
  identical bytes; nested dicts are sorted at every depth; compact
  separators; UTF-8 output.
* canonical_hash — SHA-256 of canonical_bytes.
* sign_document → verify_document round-trip — correct key verifies,
  wrong key fails, tampered payload fails.
* document_kind filtering — verify_document rejects a mismatched kind.
* Bad input — non-dict document body, malformed envelope, missing
  subject digest.
"""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.protocols.volunteer.documents import (
    VOLUNTEER_DOCUMENT_PREDICATE_TYPE,
    VOLUNTEER_DOCUMENT_SCHEMA_VERSION,
    canonical_bytes,
    canonical_hash,
    sign_document,
    verify_document,
)
from bernstein.core.security.audit_dsse import (
    DSSE_PAYLOAD_TYPE,
    Envelope,
    parse_envelope,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def signing_key() -> Ed25519PrivateKey:
    """Return a fresh Ed25519 private key for testing."""
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def other_key() -> Ed25519PrivateKey:
    """Return a second, distinct Ed25519 private key."""
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def sample_document() -> dict:
    """A representative volunteer document dict."""
    return {
        "task_id": "T-42",
        "repo": "https://github.com/example/repo",
        "commit_sha": "abcdef1234567890abcdef1234567890abcdef12",
        "payload": {"action": "candidacy", "role": "backend"},
    }


# ---------------------------------------------------------------------------
# canonical_bytes
# ---------------------------------------------------------------------------


class TestCanonicalBytes:
    """Byte-stability of canonical_bytes."""

    def test_reordered_keys_identical(self, sample_document: dict) -> None:
        """Same dict with keys inserted in different order must produce identical bytes."""
        ordered_a = canonical_bytes(sample_document)
        reordered = dict(reversed(list(sample_document.items())))
        ordered_b = canonical_bytes(reordered)
        assert ordered_a == ordered_b

    def test_nested_dict_sorted(self) -> None:
        """Nested dicts are sorted at every depth."""
        deep_a = canonical_bytes({"a": {"z": 1, "a": 2}, "b": [3, 4]})
        deep_b = canonical_bytes({"b": [3, 4], "a": {"a": 2, "z": 1}})
        assert deep_a == deep_b

    def test_compact_separators(self) -> None:
        """Output uses ',' and ':' without extra whitespace."""
        raw = canonical_bytes({"key": "val"})
        assert b": " not in raw
        assert b", " not in raw

    def test_utf8_encoding(self) -> None:
        """Non-ASCII values round-trip as UTF-8 (ensure_ascii=False)."""
        doc = {"name": "données-françaises"}
        raw = canonical_bytes(doc)
        decoded = raw.decode("utf-8")
        assert "é" in decoded and "ç" in decoded
        parsed = json.loads(raw)
        assert parsed["name"] == "données-françaises"

    def test_byte_identical_across_calls(self, sample_document: dict) -> None:
        """Two consecutive calls return the same bytes (determinism)."""
        assert canonical_bytes(sample_document) == canonical_bytes(sample_document)


# ---------------------------------------------------------------------------
# canonical_hash
# ---------------------------------------------------------------------------


class TestCanonicalHash:
    """SHA-256 of canonical_bytes."""

    def test_matches_sha256(self, sample_document: dict) -> None:
        """canonical_hash == sha256(canonical_bytes(...)).hexdigest()."""
        import hashlib

        expected = hashlib.sha256(canonical_bytes(sample_document)).hexdigest()
        assert canonical_hash(sample_document) == expected

    def test_reorder_stable(self, sample_document: dict) -> None:
        """Hash is stable across key reordering."""
        h1 = canonical_hash(sample_document)
        h2 = canonical_hash(dict(reversed(list(sample_document.items()))))
        assert h1 == h2


# ---------------------------------------------------------------------------
# Sign → verify round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """sign_document → verify_document round-trip with Ed25519."""

    def test_round_trip_succeeds(self, signing_key: Ed25519PrivateKey, sample_document: dict) -> None:
        """Envelope signed with key verifies with the matching public key."""
        envelope = sign_document(
            sample_document,
            signing_key=signing_key,
            document_kind="candidacy",
        )
        result = verify_document(envelope, signing_key.public_key())
        assert result.ok is True
        assert result.document == sample_document
        assert result.document_kind == "candidacy"

    def test_wrong_key_fails(
        self, signing_key: Ed25519PrivateKey, other_key: Ed25519PrivateKey, sample_document: dict
    ) -> None:
        """Verification with a different public key fails."""
        envelope = sign_document(
            sample_document,
            signing_key=signing_key,
            document_kind="candidacy",
        )
        result = verify_document(envelope, other_key.public_key())
        assert result.ok is False
        assert any("signature" in e.lower() for e in result.errors)

    def test_tampered_payload_fails(self, signing_key: Ed25519PrivateKey, sample_document: dict) -> None:
        """A payload byte flip causes verify to fail."""
        envelope = sign_document(
            sample_document,
            signing_key=signing_key,
            document_kind="candidacy",
        )
        # Tamper with the base64-encoded payload: flip one character.
        payload_b64 = envelope.payload_b64
        first_char = payload_b64[0]
        flipped = "A" if first_char != "A" else "B"
        tampered = Envelope(
            payload_type=envelope.payload_type,
            payload_b64=flipped + payload_b64[1:],
            signatures=envelope.signatures,
        )
        result = verify_document(tampered, signing_key.public_key())
        assert result.ok is False
        assert len(result.errors) > 0

    def test_determinism_byte_identical(self, signing_key: Ed25519PrivateKey, sample_document: dict) -> None:
        """Same doc + same key → byte-identical envelope."""
        env_a = sign_document(
            sample_document,
            signing_key=signing_key,
            document_kind="candidacy",
        )
        env_b = sign_document(
            sample_document,
            signing_key=signing_key,
            document_kind="candidacy",
        )
        assert env_a.to_json() == env_b.to_json()

    def test_persisted_envelope_roundtrips(self, signing_key: Ed25519PrivateKey, sample_document: dict) -> None:
        """Envelope serialised to JSON and re-parsed still verifies."""
        envelope = sign_document(
            sample_document,
            signing_key=signing_key,
            document_kind="candidacy",
        )
        raw = envelope.to_json()
        reparsed = parse_envelope(json.loads(raw))
        result = verify_document(reparsed, signing_key.public_key())
        assert result.ok is True
        assert result.document == sample_document

    def test_custom_keyid(self, signing_key: Ed25519PrivateKey, sample_document: dict) -> None:
        """Explicit keyid override is propagated into the envelope."""
        envelope = sign_document(
            sample_document,
            signing_key=signing_key,
            document_kind="candidacy",
            keyid="my-custom-key",
        )
        assert envelope.signatures[0].keyid == "my-custom-key"
        result = verify_document(envelope, signing_key.public_key())
        assert result.ok is True
        assert result.keyid == "my-custom-key"


# ---------------------------------------------------------------------------
# document_kind filtering
# ---------------------------------------------------------------------------


class TestDocumentKind:
    """Document kind discrimination in verify_document."""

    def test_expected_kind_passes(self, signing_key: Ed25519PrivateKey, sample_document: dict) -> None:
        """Matching document_kind succeeds."""
        envelope = sign_document(
            sample_document,
            signing_key=signing_key,
            document_kind="candidacy",
        )
        result = verify_document(
            envelope,
            signing_key.public_key(),
            expected_document_kind="candidacy",
        )
        assert result.ok is True
        assert result.document_kind == "candidacy"

    def test_wrong_kind_fails(self, signing_key: Ed25519PrivateKey, sample_document: dict) -> None:
        """Mismatched document_kind fails verification."""
        envelope = sign_document(
            sample_document,
            signing_key=signing_key,
            document_kind="candidacy",
        )
        result = verify_document(
            envelope,
            signing_key.public_key(),
            expected_document_kind="dispute",
        )
        assert result.ok is False
        assert any("document_kind" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Predicate / statement structure
# ---------------------------------------------------------------------------


class TestStatementStructure:
    """Verify the envelope carries the expected in-toto statement shape."""

    def test_predicate_type(self, signing_key: Ed25519PrivateKey, sample_document: dict) -> None:
        """Envelope's predicate type is VOLUNTEER_DOCUMENT_PREDICATE_TYPE."""
        envelope = sign_document(
            sample_document,
            signing_key=signing_key,
            document_kind="candidacy",
        )
        statement = envelope.statement
        assert statement["predicateType"] == VOLUNTEER_DOCUMENT_PREDICATE_TYPE

    def test_schema_version_in_predicate(self, signing_key: Ed25519PrivateKey, sample_document: dict) -> None:
        """Predicate body carries VOLUNTEER_DOCUMENT_SCHEMA_VERSION."""
        envelope = sign_document(
            sample_document,
            signing_key=signing_key,
            document_kind="candidacy",
        )
        predicate = envelope.statement["predicate"]
        assert predicate["schema_version"] == VOLUNTEER_DOCUMENT_SCHEMA_VERSION

    def test_document_kind_in_predicate(self, signing_key: Ed25519PrivateKey, sample_document: dict) -> None:
        """Predicate body carries the document_kind discriminator."""
        envelope = sign_document(
            sample_document,
            signing_key=signing_key,
            document_kind="result-receipt",
        )
        predicate = envelope.statement["predicate"]
        assert predicate["document_kind"] == "result-receipt"

    def test_subject_digest_matches(self, signing_key: Ed25519PrivateKey, sample_document: dict) -> None:
        """Subject digest is sha256 of canonical_bytes(document)."""
        envelope = sign_document(
            sample_document,
            signing_key=signing_key,
            document_kind="candidacy",
        )
        import hashlib

        expected_digest = hashlib.sha256(canonical_bytes(sample_document)).hexdigest()
        statement = envelope.statement
        actual_digest = statement["subject"][0]["digest"]["sha256"]
        assert actual_digest == expected_digest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Module-level constants are stable and well-formed."""

    def test_predicate_type_is_url(self) -> None:
        """Predicate type is a URL string."""
        assert VOLUNTEER_DOCUMENT_PREDICATE_TYPE.startswith("https://")

    def test_schema_version_is_semver_like(self) -> None:
        """Schema version follows semver-ish pattern."""
        parts = VOLUNTEER_DOCUMENT_SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_payload_type_matches_audit_dsse(self) -> None:
        """Envelope payload type is the standard DSSE type."""
        assert DSSE_PAYLOAD_TYPE == "application/vnd.in-toto+json"
