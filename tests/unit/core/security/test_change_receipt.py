"""Tests for change receipt data model and verification.

Coverage:

* ChangeAttempt serialization and required fields.
* ChangeReceipt serialization, canonical bytes, and digest.
* the shared verifier over a signed change receipt: valid passes, missing
  fields fail, wrong types fail.
* Digest consistency: re-serializing produces matching hash.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bernstein.core.receipts.protocol import (
    ReceiptVerification,
    registered_kinds,
    sign_receipt,
    verify_receipt,
)
from bernstein.core.security.change_receipt import (
    CHANGE_RECEIPT_SCHEMA_VERSION,
    RECEIPT_KIND,
    ChangeAttempt,
    ChangeReceipt,
    canonical_bytes,
    change_receipt_payload_errors,
)
from bernstein.core.skills.catalog.signature import generate_signer_keypair


def _verify(payload: Any) -> ReceiptVerification:
    """Sign a change receipt payload and verify it through the one verifier."""
    private_pem, public_pem = generate_signer_keypair()
    envelope = sign_receipt(RECEIPT_KIND, payload, private_key_pem=private_pem, public_key_pem=public_pem)
    return verify_receipt(envelope)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_change() -> ChangeAttempt:
    """Return a representative change attempt."""
    return ChangeAttempt(
        change_id="change-001",
        change_type="create",
        target="iam.User:alice",
        attempted_at="2025-01-15T10:30:00Z",
        outcome="success",
        error_message="",
    )


@pytest.fixture()
def sample_receipt(sample_change: ChangeAttempt) -> ChangeReceipt:
    """Return a complete, valid change receipt."""
    return ChangeReceipt(
        plan_id="plan-abc123",
        plan_digest="a" * 64,
        playbook_digest="b" * 64,
        environment_digest="c" * 64,
        approver_identity="alice@example.com",
        changes=(sample_change,),
        final_status="complete",
        timestamp="2025-01-15T10:31:00Z",
    )


# ---------------------------------------------------------------------------
# ChangeAttempt
# ---------------------------------------------------------------------------


class TestChangeAttempt:
    """ChangeAttempt dataclass behavior."""

    def test_to_dict_round_trip(self, sample_change: ChangeAttempt) -> None:
        """to_dict produces JSON-compatible dict."""
        d = sample_change.to_dict()
        assert d["change_id"] == "change-001"
        assert d["change_type"] == "create"
        assert d["target"] == "iam.User:alice"
        assert d["outcome"] == "success"
        assert d["error_message"] == ""
        # JSON-serializable
        json.dumps(d)

    def test_failure_outcome(self) -> None:
        """ChangeAttempt with failure outcome."""
        change = ChangeAttempt(
            change_id="change-002",
            change_type="update",
            target="kv.Secret:db-pass",
            attempted_at="2025-01-15T10:35:00Z",
            outcome="failure",
            error_message="permission denied",
        )
        d = change.to_dict()
        assert d["outcome"] == "failure"
        assert d["error_message"] == "permission denied"

    def test_skipped_outcome(self) -> None:
        """ChangeAttempt with skipped outcome."""
        change = ChangeAttempt(
            change_id="change-003",
            change_type="delete",
            target="iam.User:bob",
            attempted_at="2025-01-15T10:40:00Z",
            outcome="skipped",
            error_message="",
        )
        d = change.to_dict()
        assert d["outcome"] == "skipped"


# ---------------------------------------------------------------------------
# ChangeReceipt
# ---------------------------------------------------------------------------


class TestChangeReceipt:
    """ChangeReceipt dataclass behavior."""

    def test_to_dict_includes_schema_version(self, sample_receipt: ChangeReceipt) -> None:
        """to_dict includes schema_version."""
        d = sample_receipt.to_dict()
        assert d["schema_version"] == CHANGE_RECEIPT_SCHEMA_VERSION

    def test_to_dict_includes_all_fields(self, sample_receipt: ChangeReceipt) -> None:
        """to_dict includes all required fields."""
        d = sample_receipt.to_dict()
        assert d["plan_id"] == "plan-abc123"
        assert d["plan_digest"] == "a" * 64
        assert d["playbook_digest"] == "b" * 64
        assert d["environment_digest"] == "c" * 64
        assert d["approver_identity"] == "alice@example.com"
        assert len(d["changes"]) == 1
        assert d["final_status"] == "complete"
        assert d["timestamp"] == "2025-01-15T10:31:00Z"

    def test_canonical_bytes_deterministic(self, sample_receipt: ChangeReceipt) -> None:
        """Same receipt produces identical canonical bytes."""
        a = sample_receipt.canonical_bytes()
        b = sample_receipt.canonical_bytes()
        assert a == b

    def test_digest_is_sha256_hex(self, sample_receipt: ChangeReceipt) -> None:
        """digest is 64-char hex string (sha256)."""
        d = sample_receipt.digest
        assert len(d) == 64
        assert all(c in "0123456789abcdef" for c in d)

    def test_digest_matches_canonical_bytes(self, sample_receipt: ChangeReceipt) -> None:
        """digest is sha256 of canonical_bytes."""
        import hashlib

        expected = hashlib.sha256(sample_receipt.canonical_bytes()).hexdigest()
        assert sample_receipt.digest == expected

    def test_multiple_changes(self, sample_change: ChangeAttempt) -> None:
        """Receipt with multiple changes."""
        change2 = ChangeAttempt(
            change_id="change-002",
            change_type="update",
            target="kv.Secret:api-key",
            attempted_at="2025-01-15T11:00:00Z",
            outcome="success",
            error_message="",
        )
        receipt = ChangeReceipt(
            plan_id="plan-multi",
            plan_digest="d" * 64,
            playbook_digest="e" * 64,
            environment_digest="f" * 64,
            approver_identity="bob@example.com",
            changes=(sample_change, change2),
            final_status="complete",
            timestamp="2025-01-15T11:01:00Z",
        )
        d = receipt.to_dict()
        assert len(d["changes"]) == 2

    def test_partial_status(self, sample_change: ChangeAttempt) -> None:
        """Receipt with partial status."""
        failed_change = ChangeAttempt(
            change_id="change-002",
            change_type="update",
            target="iam.User:charlie",
            attempted_at="2025-01-15T12:00:00Z",
            outcome="failure",
            error_message="conflict",
        )
        receipt = ChangeReceipt(
            plan_id="plan-partial",
            plan_digest="g" * 64,
            playbook_digest="h" * 64,
            environment_digest="i" * 64,
            approver_identity="carol@example.com",
            changes=(sample_change, failed_change),
            final_status="partial",
            timestamp="2025-01-15T12:01:00Z",
        )
        d = receipt.to_dict()
        assert d["final_status"] == "partial"


# ---------------------------------------------------------------------------
# The registered payload check, called directly
# ---------------------------------------------------------------------------


class TestPayloadCheck:
    """The ``security.change`` payload check on its own, off the envelope path.

    The shared verifier reaches this function through the kind registry, so a
    direct call is the only place its contract is pinned: which fields it
    rejects, and that it names them.
    """

    def test_importing_the_module_registers_its_kind(self) -> None:
        """The check is wired: importing the module puts its kind in the registry."""
        assert RECEIPT_KIND in registered_kinds()

    def test_well_formed_payload_has_no_errors(self, sample_receipt: ChangeReceipt) -> None:
        """A valid payload produces no semantic errors."""
        assert change_receipt_payload_errors(sample_receipt.to_dict()) == ()

    def test_invalid_final_status_is_named(self, sample_receipt: ChangeReceipt) -> None:
        """A rejected field is named, so a caller can report which one failed."""
        payload = sample_receipt.to_dict()
        payload["final_status"] = "not-a-status"
        errors = change_receipt_payload_errors(payload)
        assert any(e.startswith("final_status:") for e in errors), errors

    def test_missing_changes_is_reported(self, sample_receipt: ChangeReceipt) -> None:
        """A receipt that records no change list is not well-formed."""
        payload = sample_receipt.to_dict()
        del payload["changes"]
        errors = change_receipt_payload_errors(payload)
        assert any(e.startswith("changes:") for e in errors), errors


# ---------------------------------------------------------------------------
# Verification through the shared protocol
# ---------------------------------------------------------------------------


class TestVerifyReceipt:
    """The shared verifier's behaviour on the ``security.change`` kind."""

    def test_valid_receipt_passes(self, sample_receipt: ChangeReceipt) -> None:
        """Valid receipt verifies without errors."""
        result = _verify(sample_receipt.to_dict())
        assert result.ok
        assert result.kind == RECEIPT_KIND
        assert result.payload_digest == sample_receipt.digest
        assert not result.errors

    def test_missing_plan_id_fails(self, sample_receipt: ChangeReceipt) -> None:
        """Missing plan_id fails verification."""
        d = sample_receipt.to_dict()
        del d["plan_id"]
        result = _verify(d)
        assert not result.ok
        assert any(e.startswith("plan_id:") for e in result.errors)

    def test_wrong_schema_version_fails(self, sample_receipt: ChangeReceipt) -> None:
        """Wrong schema_version fails verification."""
        d = sample_receipt.to_dict()
        d["schema_version"] = "0.0.0"
        result = _verify(d)
        assert not result.ok
        assert any(e.startswith("schema_version:") for e in result.errors)

    def test_wrong_type_plan_id_fails(self, sample_receipt: ChangeReceipt) -> None:
        """Non-string plan_id fails verification."""
        d = sample_receipt.to_dict()
        d["plan_id"] = 123
        result = _verify(d)
        assert not result.ok
        assert any(e.startswith("plan_id:") for e in result.errors)

    def test_invalid_final_status_fails(self, sample_receipt: ChangeReceipt) -> None:
        """Invalid final_status value fails verification."""
        d = sample_receipt.to_dict()
        d["final_status"] = "unknown"
        result = _verify(d)
        assert not result.ok
        assert any(e.startswith("final_status:") for e in result.errors)

    def test_changes_not_list_fails(self, sample_receipt: ChangeReceipt) -> None:
        """Non-list changes fails verification."""
        d = sample_receipt.to_dict()
        d["changes"] = "not-a-list"
        result = _verify(d)
        assert not result.ok
        assert any(e.startswith("changes:") for e in result.errors)

    def test_change_missing_change_id_fails(self, sample_receipt: ChangeReceipt) -> None:
        """Change with a wrongly-typed change_id fails verification."""
        d = sample_receipt.to_dict()
        d["changes"][0]["change_id"] = 123  # wrong type
        result = _verify(d)
        assert not result.ok
        assert any("change_id" in e for e in result.errors)

    def test_change_invalid_outcome_fails(self, sample_receipt: ChangeReceipt) -> None:
        """Change with invalid outcome fails verification."""
        d = sample_receipt.to_dict()
        d["changes"][0]["outcome"] = "unknown"
        result = _verify(d)
        assert not result.ok
        assert any("outcome" in e for e in result.errors)

    def test_non_dict_root_fails(self) -> None:
        """A receipt that is not an object fails verification rather than raising."""
        result = verify_receipt(["not", "a", "dict"])  # type: ignore[arg-type]
        assert not result.ok
        assert any("must be an object" in e for e in result.errors)

    def test_digest_matches_recomputed(self, sample_receipt: ChangeReceipt) -> None:
        """The verifier reports the digest the receipt computes for itself."""
        result = _verify(sample_receipt.to_dict())
        assert result.ok
        assert result.payload_digest == sample_receipt.digest


# ---------------------------------------------------------------------------
# canonical_bytes
# ---------------------------------------------------------------------------


class TestCanonicalBytes:
    """Byte-stability of canonical_bytes."""

    def test_reordered_keys_identical(self, sample_receipt: ChangeReceipt) -> None:
        """Same dict with keys in different order produces identical bytes."""
        d = sample_receipt.to_dict()
        a = canonical_bytes(d)
        # Reorder top-level keys
        reordered = dict(reversed(list(d.items())))
        b = canonical_bytes(reordered)
        assert a == b

    def test_compact_separators(self) -> None:
        """Output uses ',' and ':' without extra whitespace."""
        raw = canonical_bytes({"key": "val"})
        assert b": " not in raw
        assert b", " not in raw

    def test_utf8_encoding(self) -> None:
        """Non-ASCII values round-trip as UTF-8."""
        doc = {"approver": "françois@example.com"}
        raw = canonical_bytes(doc)
        decoded = raw.decode("utf-8")
        assert "ç" in decoded
        parsed = json.loads(raw)
        assert parsed["approver"] == "françois@example.com"
