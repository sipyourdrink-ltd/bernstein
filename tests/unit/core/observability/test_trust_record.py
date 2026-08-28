"""Tests for :mod:`bernstein.core.observability.trust_record`.

Focused tests for the TRACE 0.2 Trust Record emitter functionality.
Tests cover journal parsing, claim construction, signing, and canonical output.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.observability.trust_record import (
    TrustRecord,
    TrustRecordEmitter,
    _sign_canonical_bytes_detached,
)


def _create_journal(tmp_path: Path, events: list[dict]) -> Path:
    """Create a journal.jsonl file with the given events."""
    journal = tmp_path / "journal.jsonl"
    lines = [json.dumps(e, sort_keys=True) for e in events]
    journal.write_text("\n".join(lines) + "\n" if lines else "")
    return journal


# ---------------------------------------------------------------------------
# TrustRecordEmitter._build_unsigned_record
# ---------------------------------------------------------------------------


class TestBuildUnsignedRecord:
    def test_empty_journal_returns_zero_counts(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        journal = _create_journal(tmp_path, [])
        record = emitter._build_unsigned_record(journal, "run-123")

        assert record.subject == "urn:bernstein:run:run-123"
        assert record.delegation is not None
        assert record.delegation.startswith("urn:bernstein:install:")
        assert record.claims == {
            "run_id": "run-123",
            "event_count": 0,
            "head_hash": "",
        }
        assert record.signature == {}

    def test_single_event_populates_head_hash_and_timestamps(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = [{"ts": 1000.0, "event_hash": "abc123", "type": "start"}]
        journal = _create_journal(tmp_path, events)
        record = emitter._build_unsigned_record(journal, "run-456")

        assert record.claims["event_count"] == 1
        assert record.claims["head_hash"] == "abc123"
        assert record.claims["first_event_ts"] == 1000.0
        assert record.claims["last_event_ts"] == 1000.0

    def test_multiple_events_records_first_and_last_timestamps(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = [
            {"ts": 1000.0, "event_hash": "first"},
            {"ts": 2000.0, "event_hash": "middle"},
            {"ts": 3000.0, "event_hash": "last"},
        ]
        journal = _create_journal(tmp_path, events)
        record = emitter._build_unsigned_record(journal, "run-789")

        assert record.claims["event_count"] == 3
        assert record.claims["head_hash"] == "last"
        assert record.claims["first_event_ts"] == 1000.0
        assert record.claims["last_event_ts"] == 3000.0

    def test_events_without_timestamps_omits_ts_fields(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = [{"event_hash": "no-ts", "type": "info"}]
        journal = _create_journal(tmp_path, events)
        record = emitter._build_unsigned_record(journal, "run-no-ts")

        assert "first_event_ts" not in record.claims
        assert "last_event_ts" not in record.claims

    def test_malformed_json_lines_skipped(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        journal = tmp_path / "journal.jsonl"
        journal.write_text('{"valid": true}\nnot json\n{"also valid": 2}\n')
        record = emitter._build_unsigned_record(journal, "run-malformed")

        assert record.claims["event_count"] == 2

    def test_missing_journal_returns_empty_record(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        missing = tmp_path / "nonexistent.jsonl"
        record = emitter._build_unsigned_record(missing, "run-miss")

        assert record.claims["event_count"] == 0
        assert record.claims["head_hash"] == ""


# ---------------------------------------------------------------------------
# TrustRecordEmitter._sign_record
# ---------------------------------------------------------------------------


class TestSignRecord:
    def test_signature_contains_eddsa_alg(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        record = TrustRecord(
            subject="urn:bernstein:run:test",
            delegation=None,
            claims={"run_id": "test"},
            signature={},
        )
        signed = emitter._sign_record(record, "test-key-id")

        assert signed.signature["alg"] == "EdDSA"
        assert signed.signature["kid"] == "test-key-id"
        assert signed.signature["sig"] != ""

    def test_signature_is_base64url(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        record = TrustRecord(
            subject="urn:bernstein:run:test",
            delegation=None,
            claims={"run_id": "test"},
            signature={},
        )
        signed = emitter._sign_record(record, "test-key-id")

        sig = signed.signature["sig"]
        # Base64url alphabet (A-Z, a-z, 0-9, -, _), possibly with padding
        import re

        assert re.match(r"^[A-Za-z0-9_-]*={0,2}$", sig)

    def test_record_payload_unchanged_after_signing(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        record = TrustRecord(
            subject="urn:bernstein:run:test",
            delegation="urn:bernstein:install:v1",
            claims={"run_id": "test", "count": 5},
            signature={},
        )
        signed = emitter._sign_record(record, "kid-1")

        assert signed.subject == record.subject
        assert signed.delegation == record.delegation
        assert signed.claims == record.claims


# ---------------------------------------------------------------------------
# _sign_canonical_bytes_detached
# ---------------------------------------------------------------------------


class TestSignCanonicalBytesDetached:
    def test_produces_jws_format(self, tmp_path: Path) -> None:
        """Output should be header..signature format (RFC 7515 compact)."""
        # Generate a key for testing
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        private_key = Ed25519PrivateKey.generate()
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        canonical_bytes = b'{"a":1,"b":2}'
        typ = "test-type"
        kid = "test-kid"

        jws = _sign_canonical_bytes_detached(canonical_bytes, private_pem, typ, kid)

        # Format: base64url(header)..base64url(signature)
        parts = jws.split(".")
        assert len(parts) == 3
        assert parts[1] == ""  # Empty body slot in detached JWS

    def test_jws_header_contains_typ_and_kid(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        private_key = Ed25519PrivateKey.generate()
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        canonical_bytes = b"test"
        typ = "application/test"
        kid = "custom-kid"

        jws = _sign_canonical_bytes_detached(canonical_bytes, private_pem, typ, kid)

        import base64

        header_b64 = jws.split(".")[0]
        # Add padding for decoding
        padded = header_b64 + "=" * (4 - len(header_b64) % 4)
        header = json.loads(base64.urlsafe_b64decode(padded))
        assert header["typ"] == typ
        assert header["kid"] == kid
        assert header["alg"] == "EdDSA"

    def test_different_payload_different_signature(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        private_key = Ed25519PrivateKey.generate()
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        sig1 = _sign_canonical_bytes_detached(b"payload1", private_pem, "t", "k")
        sig2 = _sign_canonical_bytes_detached(b"payload2", private_pem, "t", "k")

        assert sig1 != sig2

    def test_different_key_different_signature(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        private_key_1 = Ed25519PrivateKey.generate()
        private_pem_1 = private_key_1.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        private_key_2 = Ed25519PrivateKey.generate()
        private_pem_2 = private_key_2.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        payload = b"same payload"
        sig1 = _sign_canonical_bytes_detached(payload, private_pem_1, "t", "k")
        sig2 = _sign_canonical_bytes_detached(payload, private_pem_2, "t", "k")

        assert sig1 != sig2


# ---------------------------------------------------------------------------
# TrustRecordEmitter.emit_trust_record
# ---------------------------------------------------------------------------


class TestEmitTrustRecord:
    def test_output_is_canonical_json(self, tmp_path: Path) -> None:
        """Canonical JSON: sorted keys, minimal separators."""
        emitter = TrustRecordEmitter()
        events = [{"ts": 1000.0, "event_hash": "hash1", "type": "start"}]
        journal = _create_journal(tmp_path, events)

        output = emitter.emit_trust_record(journal, "run-emit")

        # Should be valid JSON
        parsed = json.loads(output)

        # Re-serialize with same options and compare
        expected = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
        assert output == expected

    def test_output_contains_all_required_fields(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = [{"ts": 1000.0, "event_hash": "h1"}]
        journal = _create_journal(tmp_path, events)

        output = emitter.emit_trust_record(journal, "run-fields")
        parsed = json.loads(output)

        assert "subject" in parsed
        assert "delegation" in parsed
        assert "claims" in parsed
        assert "signature" in parsed

    def test_subject_is_run_urn(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = []
        journal = _create_journal(tmp_path, events)

        output = emitter.emit_trust_record(journal, "my-run-id")
        parsed = json.loads(output)

        assert parsed["subject"] == "urn:bernstein:run:my-run-id"

    def test_delegation_is_install_urn(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = []
        journal = _create_journal(tmp_path, events)

        output = emitter.emit_trust_record(journal, "run-delegate")
        parsed = json.loads(output)

        delegation = parsed["delegation"]
        assert delegation is not None
        assert delegation.startswith("urn:bernstein:install:")
        # Should be 16-char token after the prefix
        token = delegation.replace("urn:bernstein:install:", "")
        assert len(token) == 16

    def test_signature_alg_is_eddsa(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = []
        journal = _create_journal(tmp_path, events)

        output = emitter.emit_trust_record(journal, "run-sig")
        parsed = json.loads(output)

        assert parsed["signature"]["alg"] == "EdDSA"

    def test_full_round_trip_produces_valid_signature(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = [{"ts": 1.0, "event_hash": "final"}]
        journal = _create_journal(tmp_path, events)

        output = emitter.emit_trust_record(journal, "run-verify")
        parsed = json.loads(output)

        # Verify signature structure
        sig = parsed["signature"]
        assert "kid" in sig
        assert "sig" in sig
        assert sig["alg"] == "EdDSA"
        assert sig["sig"] != ""


# ---------------------------------------------------------------------------
# Integration: full emit flow
# ---------------------------------------------------------------------------


class TestFullEmitFlow:
    def test_emit_trust_record_end_to_end(self, tmp_path: Path) -> None:
        """End-to-end test: journal -> trust record -> signed output."""
        emitter = TrustRecordEmitter()

        # Create a realistic journal
        events = [
            {"ts": 1690000000.0, "event_hash": "e1", "type": "run_start"},
            {"ts": 1690000001.0, "event_hash": "e2", "type": "task_spawn"},
            {"ts": 1690000002.0, "event_hash": "e3", "type": "task_complete"},
        ]
        journal = _create_journal(tmp_path, events)

        output = emitter.emit_trust_record(journal, "integration-run")

        parsed = json.loads(output)

        # Claims
        assert parsed["claims"]["run_id"] == "integration-run"
        assert parsed["claims"]["event_count"] == 3
        assert parsed["claims"]["head_hash"] == "e3"
        assert parsed["claims"]["first_event_ts"] == 1690000000.0
        assert parsed["claims"]["last_event_ts"] == 1690000002.0

        # Subject and delegation URNs
        assert parsed["subject"] == "urn:bernstein:run:integration-run"
        assert parsed["delegation"].startswith("urn:bernstein:install:")

        # Signature
        assert parsed["signature"]["alg"] == "EdDSA"
        assert parsed["signature"]["kid"].startswith("install-")
        assert len(parsed["signature"]["sig"]) > 0

    def test_empty_journal_produces_valid_record(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        journal = _create_journal(tmp_path, [])

        output = emitter.emit_trust_record(journal, "empty-run")
        parsed = json.loads(output)

        assert parsed["claims"]["event_count"] == 0
        assert parsed["claims"]["head_hash"] == ""
        assert parsed["signature"]["sig"] != ""  # Still signed


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrorCases:
    def test_journal_with_only_whitespace_lines(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        journal = tmp_path / "journal.jsonl"
        journal.write_text("   \n\t\n   \n")

        record = emitter._build_unsigned_record(journal, "run-whitespace")
        assert record.claims["event_count"] == 0

    def test_journal_with_empty_lines_only(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        journal = tmp_path / "journal.jsonl"
        journal.write_text("\n\n")

        record = emitter._build_unsigned_record(journal, "run-empty")
        assert record.claims["event_count"] == 0
