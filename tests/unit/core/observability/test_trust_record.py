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
from bernstein.core.replay.journal import (
    _GENESIS_HASH,
    _payload_hash,
    compute_event_hash,
)


def _create_journal(tmp_path: Path, events: list[dict]) -> Path:
    """Create a journal.jsonl file with the given events, chained properly.

    Each entry in *events* is a decision payload (``{"type": ..., ...}``).
    The helper builds the Merkle chain fields (``prev_hash``,
    ``payload_hash``, ``event_hash``, ``index``) from the payload so that
    :func:`verify_events` accepts the file. A bare ``event_hash`` on the
    payload is dropped: the chain fields own the head hash.
    """
    journal = tmp_path / "journal.jsonl"
    lines: list[str] = []
    prev_hash = _GENESIS_HASH
    for index, payload in enumerate(events):
        event_type = str(payload.get("type", "event"))
        chain_payload = {k: v for k, v in payload.items() if k != "event_hash"}
        p_hash = _payload_hash(event_type, chain_payload)
        e_hash = compute_event_hash(
            prev_hash=prev_hash,
            event_type=event_type,
            payload_hash=p_hash,
            index=index,
        )
        entry = {
            "index": index,
            "event": event_type,
            "prev_hash": prev_hash,
            "payload_hash": p_hash,
            "event_hash": e_hash,
        }
        entry.update(chain_payload)
        lines.append(json.dumps(entry, sort_keys=True))
        prev_hash = e_hash
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
        # The journal format must match the internal chain: each row needs type
        # and any optional payload (but NOT event_hash, which is computed).
        events = [{"type": "start", "ts": 1000.0}]
        journal = _create_journal(tmp_path, events)
        record = emitter._build_unsigned_record(journal, "run-456")

        assert record.claims["event_count"] == 1
        # head_hash is computed from the chain; we trust it's present and non-empty.
        assert record.claims["head_hash"] != ""
        assert record.claims["first_event_ts"] == 1000.0
        assert record.claims["last_event_ts"] == 1000.0

    def test_multiple_events_records_first_and_last_timestamps(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = [
            {"type": "first", "ts": 1000.0},
            {"type": "middle", "ts": 2000.0},
            {"type": "last", "ts": 3000.0},
        ]
        journal = _create_journal(tmp_path, events)
        record = emitter._build_unsigned_record(journal, "run-789")

        assert record.claims["event_count"] == 3
        assert record.claims["head_hash"] != ""
        assert record.claims["first_event_ts"] == 1000.0
        assert record.claims["last_event_ts"] == 3000.0

    def test_events_without_timestamps_omits_ts_fields(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = [{"type": "info"}]
        journal = _create_journal(tmp_path, events)
        record = emitter._build_unsigned_record(journal, "run-no-ts")

        assert "first_event_ts" not in record.claims
        assert "last_event_ts" not in record.claims

    def test_malformed_json_lines_skipped(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        # Build two valid chained events, then interleave a malformed line.
        events = [{"type": "valid"}, {"type": "also_valid"}]
        journal = _create_journal(tmp_path, events)
        # Insert a malformed line between events 0 and 1 (appended, not in
        # the chain): the tolerant reader must skip it and keep the chain
        # intact for the two real events.
        raw_lines = journal.read_text(encoding="utf-8").strip().splitlines()
        raw_lines.insert(1, "not json")
        journal.write_text("\n".join(raw_lines) + "\n")
        record = emitter._build_unsigned_record(journal, "run-malformed")

        assert record.claims["event_count"] == 2

    def test_missing_journal_returns_empty_record(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        missing = tmp_path / "nonexistent.jsonl"
        record = emitter._build_unsigned_record(missing, "run-miss")

        assert record.claims["event_count"] == 0
        assert record.claims["head_hash"] == ""

    def test_a_journal_with_a_broken_chain_is_refused(self, tmp_path: Path) -> None:
        """A tampered journal (mutated prev_hash) must not produce a record.

        The error must name the divergent step index (R12), not merely
        report a bare true/false.
        """
        import pytest

        emitter = TrustRecordEmitter()
        # Build a valid two-event journal, then corrupt the second event's
        # prev_hash so the chain breaks at step 1.
        events = [{"type": "event_1"}, {"type": "event_2"}]
        journal = _create_journal(tmp_path, events)
        raw = json.loads(journal.read_text(encoding="utf-8").splitlines()[1])
        raw["prev_hash"] = "deadbeef" * 8
        lines = journal.read_text(encoding="utf-8").strip().splitlines()
        lines[1] = json.dumps(raw, sort_keys=True)
        journal.write_text("\n".join(lines) + "\n")

        with pytest.raises(ValueError, match="journal chain broken"):
            emitter._build_unsigned_record(journal, "run-broken")


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
            {"ts": 1690000000.0, "type": "run_start"},
            {"ts": 1690000001.0, "type": "task_spawn"},
            {"ts": 1690000002.0, "type": "task_complete"},
        ]
        journal = _create_journal(tmp_path, events)

        output = emitter.emit_trust_record(journal, "integration-run")

        parsed = json.loads(output)

        # Claims
        assert parsed["claims"]["run_id"] == "integration-run"
        assert parsed["claims"]["event_count"] == 3
        # head_hash is computed from the chain; the important property is
        # that it is non-empty and reproducible from the journal.
        assert parsed["claims"]["head_hash"] != ""
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


# ---------------------------------------------------------------------------
# Determinism: same journal, byte-identical unsigned payload
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_the_same_journal_yields_a_byte_identical_unsigned_payload(self, tmp_path: Path) -> None:
        """Two emitter calls on the same journal must produce identical bytes.

        Determinism is structurally guaranteed (JSON sorted keys, compact
        separators, ``json.loads`` round-trip preserves float identity), but
        this test pins the invariant with an inter-process comparison so a
        future refactor cannot silently break it.
        """
        import subprocess
        import sys

        events = [
            {"ts": 1690000000.0, "type": "run_start"},
            {"ts": 1690000001.0, "type": "task_spawn"},
            {"ts": 1690000002.0, "type": "task_complete"},
        ]
        journal = _create_journal(tmp_path, events)
        run_id = "determinism-run"

        # Spawn two independent subprocesses that each call
        # _build_unsigned_record and serialize with the same canonical
        # options; byte-identical output across processes is the invariant.
        snippet = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from bernstein.core.observability.trust_record import TrustRecordEmitter\n"
            "journal = Path(sys.argv[1])\n"
            "run_id = sys.argv[2]\n"
            "record = TrustRecordEmitter()._build_unsigned_record(journal, run_id)\n"
            "body = {\n"
            "    'subject': record.subject,\n"
            "    'delegation': record.delegation,\n"
            "    'claims': record.claims,\n"
            "    'signature': record.signature,\n"
            "}\n"
            "print(json.dumps(body, sort_keys=True, separators=(',', ':')))\n"
        )
        # Compare the canonical bytes from two independent subprocesses
        first = subprocess.run(
            [sys.executable, "-c", snippet, str(journal), run_id],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.rstrip("\n")
        second = subprocess.run(
            [sys.executable, "-c", snippet, str(journal), run_id],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.rstrip("\n")
        # Compare the canonical bytes for determinism
        assert first == second

        # And a second call on the same process must match the subprocess
        # output exactly.
        in_process = TrustRecordEmitter()._build_unsigned_record(journal, run_id)
        in_process_body = {
            "subject": in_process.subject,
            "delegation": in_process.delegation,
            "claims": in_process.claims,
            "signature": in_process.signature,
        }
        in_process_bytes = json.dumps(in_process_body, sort_keys=True, separators=(",", ":"))
        assert in_process_bytes == first


# ---------------------------------------------------------------------------
# Core install unchanged without the [trace] extra
# ---------------------------------------------------------------------------


class TestCoreInstallWithoutTraceExtra:
    def test_importing_bernstein_does_not_import_agentrust_trace(self) -> None:
        """Importing bernstein must not pull in agentrust_trace.

        The trace extra is optional; a future refactor that accidentally
        adds a top-level import would silently reintroduce the transitive
        dependency, so this test pins the guard with a subprocess.
        """
        import subprocess
        import sys

        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, bernstein; print([m for m in sys.modules if 'agentrust' in m])",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        # The list must be empty: no agentrust module may be loaded.
        assert proc.stdout.rstrip("\n") == "[]"
