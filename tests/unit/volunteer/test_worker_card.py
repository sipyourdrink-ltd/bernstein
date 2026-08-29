"""Tests for the worker card protocol document.

The worker card is what a donor publishes about what capabilities they offer.
These tests cover validation, sign/verify, and the credential denylist that
guards against smuggling a credential into a string field.
"""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.protocols.volunteer.worker_card import (
    WORKER_CARD_PREDICATE_TYPE,
    WorkerCard,
    WorkerCardError,
    build_worker_card_envelope,
    verify_worker_card_envelope,
)
from bernstein.core.security.audit_dsse import parse_envelope


def _keypair() -> tuple[Ed25519PrivateKey, Any]:
    """Generate a fresh Ed25519 keypair for testing."""
    key = Ed25519PrivateKey.generate()
    return key, key.public_key()


def _make_worker_card(**overrides: Any) -> WorkerCard:
    """Build a valid WorkerCard for testing."""
    defaults: dict[str, Any] = {
        "task_types": ["compute", "analysis"],
        "capabilities": "high-performance compute",
        "cpu_ceiling": "large",
        "ram_ceiling": "large",
        "gpu_ceiling": "micro",
        "sandbox_tier": "container",
        "availability_window": "weekdays 9am-5pm",
        "budget_posture": "generous",
        "submitted_at": "2026-08-21T12:00:00Z",
    }
    defaults.update(overrides)
    return WorkerCard(**defaults)


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestWorkerCardValidation:
    """A worker card that passes construction is self-consistent."""

    def test_valid_card_passes(self) -> None:
        card = _make_worker_card()
        assert card.task_types == ["compute", "analysis"]
        assert card.cpu_ceiling == "large"

    def test_empty_task_types_rejected(self) -> None:
        with pytest.raises(WorkerCardError, match="task_types"):
            _make_worker_card(task_types=[])

    def test_unknown_task_type_rejected(self) -> None:
        with pytest.raises(WorkerCardError):
            _make_worker_card(task_types=["invalid_type"])

    def test_invalid_cpu_ceiling_rejected(self) -> None:
        with pytest.raises(WorkerCardError, match="cpu_ceiling"):
            _make_worker_card(cpu_ceiling="invalid")

    def test_invalid_ram_ceiling_rejected(self) -> None:
        with pytest.raises(WorkerCardError, match="ram_ceiling"):
            _make_worker_card(ram_ceiling="invalid")

    def test_invalid_gpu_ceiling_rejected(self) -> None:
        with pytest.raises(WorkerCardError, match="gpu_ceiling"):
            _make_worker_card(gpu_ceiling="invalid")

    def test_invalid_sandbox_tier_rejected(self) -> None:
        with pytest.raises(WorkerCardError, match="sandbox_tier"):
            _make_worker_card(sandbox_tier="invalid")

    def test_empty_availability_window_rejected(self) -> None:
        with pytest.raises(WorkerCardError, match="availability_window"):
            _make_worker_card(availability_window="")

    def test_invalid_budget_posture_rejected(self) -> None:
        with pytest.raises(WorkerCardError, match="budget_posture"):
            _make_worker_card(budget_posture="invalid")

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(WorkerCardError, match="submitted_at"):
            _make_worker_card(submitted_at="2026-08-21T12:00:00")

    def test_credential_in_cpu_ceiling_rejected(self) -> None:
        # The credential denylist must catch a "KEY" substring in cpu_ceiling.
        with pytest.raises(WorkerCardError, match="credential"):
            _make_worker_card(cpu_ceiling="MY_SECRET_KEY")

    def test_credential_in_ram_ceiling_rejected(self) -> None:
        with pytest.raises(WorkerCardError, match="credential"):
            _make_worker_card(ram_ceiling="some_TOKEN")

    def test_credential_in_gpu_ceiling_rejected(self) -> None:
        with pytest.raises(WorkerCardError, match="credential"):
            _make_worker_card(gpu_ceiling="ghp_SECRET")

    def test_credential_in_sandbox_tier_rejected(self) -> None:
        with pytest.raises(WorkerCardError, match="credential"):
            _make_worker_card(sandbox_tier="custom_KEY")

    def test_credential_in_availability_rejected(self) -> None:
        with pytest.raises(WorkerCardError, match="credential"):
            _make_worker_card(availability_window="sk-abc123-token")

    def test_credential_in_budget_posture_rejected(self) -> None:
        with pytest.raises(WorkerCardError, match="credential"):
            _make_worker_card(budget_posture="with_PASSWORD")

    def test_dict_as_task_type_rejected(self) -> None:
        with pytest.raises(WorkerCardError, match="task_types"):
            _make_worker_card(task_types=[{"type": "compute"}])  # type: ignore[arg-type]

    def test_list_as_cpu_ceiling_rejected(self) -> None:
        with pytest.raises(WorkerCardError, match="cpu_ceiling"):
            _make_worker_card(cpu_ceiling=["large"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------


class TestCanonicalForm:
    """to_canonical_dict must produce byte-stable output."""

    def test_canonical_dict_is_sorted(self) -> None:
        card = _make_worker_card()
        result = card.to_canonical_dict()
        # Keys must be in sorted order.
        keys = list(result.keys())
        assert keys == sorted(keys)

    def test_digest_is_deterministic(self) -> None:
        card1 = _make_worker_card()
        card2 = _make_worker_card()
        assert card1.digest() == card2.digest()

    def test_digest_changes_with_field(self) -> None:
        card1 = _make_worker_card()
        card2 = _make_worker_card(cpu_ceiling="small")
        assert card1.digest() != card2.digest()


# ---------------------------------------------------------------------------
# Sign / verify envelope
# ---------------------------------------------------------------------------


class TestSignVerifyEnvelope:
    """Round-trip a worker card through sign/verify."""

    def test_envelope_contains_correct_predicate_type(self) -> None:
        key, _ = _keypair()
        card = _make_worker_card()
        envelope = build_worker_card_envelope(card, key)
        statement = envelope.statement
        assert statement["predicateType"] == WORKER_CARD_PREDICATE_TYPE

    def test_envelope_contains_correct_document_kind(self) -> None:
        key, _ = _keypair()
        card = _make_worker_card()
        envelope = build_worker_card_envelope(card, key)
        statement = envelope.statement
        # predicate is a dict (not a structured Predicate object).
        predicate = statement["predicate"]
        assert predicate["document_kind"] == "worker-card"

    def test_envelope_round_trip(self) -> None:
        key, public = _keypair()
        card = _make_worker_card()
        envelope = build_worker_card_envelope(card, key)
        parsed = parse_envelope(envelope.to_dict())
        result = verify_worker_card_envelope(parsed, public)
        assert result.ok
        assert result.errors == ()


class TestVerifyWorkerCardEnvelope:
    """Verification accepts good envelopes, rejects bad ones."""

    def test_wrong_document_kind_fails(self) -> None:
        # Build a worker card, then mutate the embedded document_kind to
        # something else.  Verification should reject.
        key, public = _keypair()
        card = _make_worker_card()
        envelope = build_worker_card_envelope(card, key)
        env_dict = envelope.to_dict()
        import base64
        import json

        statement_bytes = base64.b64decode(env_dict["payload"])
        statement = json.loads(statement_bytes)
        # Mutate document_kind at the JSON layer; re-encode.
        statement["predicate"]["document_kind"] = "wrong-kind"
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
        result = verify_worker_card_envelope(mutated, public)
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
        card = _make_worker_card()
        envelope = build_worker_card_envelope(card, key)
        env_dict = envelope.to_dict()
        import base64
        import json

        statement_bytes = base64.b64decode(env_dict["payload"])
        statement = json.loads(statement_bytes)
        # Inject a credential-looking string into the document.
        statement["predicate"]["document"]["capabilities"] = "uses API_KEY"
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
        result = verify_worker_card_envelope(mutated, public)
        assert not result.ok
        assert any("credential" in err.lower() for err in result.errors)

    def test_wrong_public_key_fails(self) -> None:
        key, _ = _keypair()
        _, other_public = _keypair()
        card = _make_worker_card()
        envelope = build_worker_card_envelope(card, key)
        result = verify_worker_card_envelope(envelope, other_public)
        assert not result.ok
        assert result.errors != ()
