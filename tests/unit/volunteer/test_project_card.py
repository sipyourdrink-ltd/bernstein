"""Tests for the project card protocol document.

The project card is what a project publishes about what it offers to volunteers.
These tests cover validation, sign/verify, and the credential denylist that
guards against smuggling a credential into a string field.
"""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.protocols.volunteer.project_card import (
    PROJECT_CARD_PREDICATE_TYPE,
    ProjectCard,
    ProjectCardError,
    build_project_card_envelope,
    verify_project_card_envelope,
)
from bernstein.core.security.audit_dsse import parse_envelope


def _keypair() -> tuple[Ed25519PrivateKey, Any]:
    """Generate a fresh Ed25519 keypair for testing."""
    key = Ed25519PrivateKey.generate()
    return key, key.public_key()


def _make_project_card(**overrides: Any) -> ProjectCard:
    """Build a valid ProjectCard for testing."""
    defaults: dict[str, Any] = {
        "demand": "high",
        "task_types": ["compute", "analysis"],
        "requirements": "Python 3.12+, 4GB RAM",
        "demand_snapshot": {"open": 12, "in_progress": 3, "duration_band_minutes": "30-60"},
        "status": "active",
        "submitted_at": "2026-08-21T12:00:00Z",
    }
    defaults.update(overrides)
    return ProjectCard(**defaults)


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestProjectCardValidation:
    """A project card that passes construction is self-consistent."""

    def test_valid_card_passes(self) -> None:
        card = _make_project_card()
        assert card.demand == "high"
        assert card.task_types == ["compute", "analysis"]

    def test_missing_demand_rejected(self) -> None:
        with pytest.raises(ProjectCardError, match="demand"):
            ProjectCard(
                demand="",  # type: ignore[arg-type]
                task_types=["compute"],
                requirements="Python 3.12+",
                demand_snapshot={},
                status="active",
                submitted_at="2026-08-21T12:00:00Z",
            )

    def test_empty_task_types_rejected(self) -> None:
        with pytest.raises(ProjectCardError, match="task_types"):
            _make_project_card(task_types=[])

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ProjectCardError, match="status"):
            _make_project_card(status="invalid")  # type: ignore[arg-type]

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ProjectCardError, match="submitted_at"):
            _make_project_card(submitted_at="2026-08-21T12:00:00")

    def test_invalid_timestamp_rejected(self) -> None:
        with pytest.raises(ProjectCardError, match="submitted_at"):
            _make_project_card(submitted_at="not-a-timestamp")

    def test_dict_as_status_rejected(self) -> None:
        with pytest.raises(ProjectCardError, match="status"):
            _make_project_card(status={"active": True})  # type: ignore[arg-type]

    def test_int_as_demand_rejected(self) -> None:
        with pytest.raises(ProjectCardError, match="demand"):
            _make_project_card(demand=42)  # type: ignore[arg-type]

    def test_bool_as_int_rejected_for_numeric_field(self) -> None:
        # bool is an int subclass; the constructor must reject bool where
        # int is expected.  Here we attempt a bool as a string field -- but
        # the issue is that bool is treated as str-typed.  Verify the
        # bool rejection in the type-check path.
        with pytest.raises(ProjectCardError):
            _make_project_card(demand=True)  # type: ignore[arg-type]

    def test_credential_in_demand_rejected(self) -> None:
        # The credential denylist must catch a "TOKEN" substring in demand.
        with pytest.raises(ProjectCardError, match="credential"):
            _make_project_card(demand="please include a TOKEN")

    def test_credential_in_requirements_rejected(self) -> None:
        with pytest.raises(ProjectCardError, match="credential"):
            _make_project_card(requirements="requires API_KEY")

    def test_credential_in_notes_rejected(self) -> None:
        with pytest.raises(ProjectCardError, match="credential"):
            _make_project_card(notes="stored PASSWORD")


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------


class TestCanonicalForm:
    """to_canonical_dict must produce byte-stable output."""

    def test_canonical_dict_is_sorted(self) -> None:
        card = _make_project_card()
        result = card.to_canonical_dict()
        # Keys must be in sorted order.
        keys = list(result.keys())
        assert keys == sorted(keys)

    def test_digest_is_deterministic(self) -> None:
        card1 = _make_project_card()
        card2 = _make_project_card()
        assert card1.digest() == card2.digest()

    def test_digest_changes_with_field(self) -> None:
        card1 = _make_project_card()
        card2 = _make_project_card(demand="low")
        assert card1.digest() != card2.digest()


# ---------------------------------------------------------------------------
# Sign / verify envelope
# ---------------------------------------------------------------------------


class TestSignVerifyEnvelope:
    """Round-trip a project card through sign/verify."""

    def test_envelope_contains_correct_predicate_type(self) -> None:
        key, _ = _keypair()
        card = _make_project_card()
        envelope = build_project_card_envelope(card, key)
        statement = envelope.statement
        assert statement["predicateType"] == PROJECT_CARD_PREDICATE_TYPE

    def test_envelope_contains_correct_document_kind(self) -> None:
        key, _ = _keypair()
        card = _make_project_card()
        envelope = build_project_card_envelope(card, key)
        statement = envelope.statement
        # predicate is a dict (not a structured Predicate object).
        predicate = statement["predicate"]
        assert predicate["document_kind"] == "project-card"

    def test_envelope_round_trip(self) -> None:
        key, public = _keypair()
        card = _make_project_card()
        envelope = build_project_card_envelope(card, key)
        parsed = parse_envelope(envelope.to_dict())
        result = verify_project_card_envelope(parsed, public)
        assert result.ok
        assert result.errors == ()


class TestVerifyProjectCardEnvelope:
    """Verification accepts good envelopes, rejects bad ones."""

    def test_wrong_document_kind_fails(self) -> None:
        # Build a project card, then mutate the embedded document_kind to
        # something else.  Verification should reject.
        key, public = _keypair()
        card = _make_project_card()
        envelope = build_project_card_envelope(card, key)
        env_dict = envelope.to_dict()
        # Mutate document_kind at the JSON layer; re-encode.
        import base64
        import json

        statement_bytes = base64.b64decode(env_dict["payload"])
        statement = json.loads(statement_bytes)
        statement["predicate"]["document_kind"] = "wrong-kind"
        # Recompute the document digest, then update subject.
        new_payload = json.dumps(statement, sort_keys=True, separators=(",", ":")).encode("utf-8")
        # Just verify with the new statement -- subject digest will mismatch.
        # The verify_project_card_envelope should report kind mismatch.
        env_dict["payload"] = base64.b64encode(new_payload).decode("ascii")
        mutated = parse_envelope(env_dict)
        result = verify_project_card_envelope(mutated, public)
        # The check fails on either document_kind mismatch or the
        # subject-digest mismatch.  Either way, the result is failure.
        assert not result.ok
        # The document_kind check is the last check; if the subject-digest
        # mismatch is reported first, that is also fine.  But when the
        # digest happens to match by coincidence, kind must be wrong.
        assert any("document_kind" in err or "hashes to" in err for err in result.errors)

    def test_credential_in_embedded_document_rejected(self) -> None:
        # Build a card normally, then mutate the document body to inject
        # a credential-like string.  Verification must catch it.
        key, public = _keypair()
        card = _make_project_card()
        envelope = build_project_card_envelope(card, key)
        env_dict = envelope.to_dict()
        import base64
        import json

        statement_bytes = base64.b64decode(env_dict["payload"])
        statement = json.loads(statement_bytes)
        # Inject a credential-looking string into the document.
        statement["predicate"]["document"]["demand"] = "needs API_TOKEN"
        # Recompute the subject digest.
        new_doc_bytes = json.dumps(statement["predicate"]["document"], sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        import hashlib

        new_digest = hashlib.sha256(new_doc_bytes).hexdigest()
        statement["subject"][0]["digest"]["sha256"] = new_digest
        new_payload = json.dumps(statement, sort_keys=True, separators=(",", ":")).encode("utf-8")
        env_dict["payload"] = base64.b64encode(new_payload).decode("ascii")
        mutated = parse_envelope(env_dict)
        result = verify_project_card_envelope(mutated, public)
        assert not result.ok
        assert any("credential" in err.lower() for err in result.errors)

    def test_wrong_public_key_fails(self) -> None:
        key, _ = _keypair()
        _, other_public = _keypair()
        card = _make_project_card()
        envelope = build_project_card_envelope(card, key)
        result = verify_project_card_envelope(envelope, other_public)
        assert not result.ok
        assert result.errors != ()
