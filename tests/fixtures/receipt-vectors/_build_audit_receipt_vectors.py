#!/usr/bin/env python3
"""Re-mint the audit-receipt test vectors in this directory (issue #4204).

Run by hand from a source checkout, never by the test suite::

    uv run python tests/fixtures/receipt-vectors/_build_audit_receipt_vectors.py

It seeds a throwaway HMAC audit chain with three events, projects the range
into a receipt signed by the deterministic Ed25519 key pinned below, and
writes the signed receipt, its public key, and a copy with one embedded event
mutated.

Why the vectors are committed rather than generated at test time
----------------------------------------------------------------
A generator that drifts along with the encoder cannot detect the drift.
``tests/unit/test_audit_receipt_format_vectors.py`` re-signs the *committed*
event range with today's encoder and requires byte-equality with the committed
receipt, so a change to the canonical JSON, the event JSONL, the COSE headers,
the DSSE pre-authentication encoding or the Merkle hashing fails CI instead of
silently invalidating a receipt already handed to an auditor. Minting the
vectors inside that test would move both sides of the comparison at once and
prove nothing.

Running this script is therefore a deliberate re-mint, not a reproduction.
Audit events are stamped with the wall clock, so a fresh run yields new
timestamps, hence new HMACs, a new chain head and new signatures: every output
file changes, whether or not the format did. Re-mint only when the receipt
format itself changed, and review the result as new evidence -- the diff
cannot tell you which part of it was the encoder.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.security.audit import AuditLog
from bernstein.core.security.audit_receipt import build_receipt
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

# Deterministic constants matching tests/unit/test_audit_receipt.py
_HMAC_KEY = b"x" * 32
_SIGN_SEED = b"i" * 32
_SINCE = "2020-01-01T00:00:00.000000Z"
_UNTIL = "2100-01-01T00:00:00.000000Z"

OUT_DIR = Path(__file__).resolve().parent


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # 1. Seed the audit chain with 3 HMAC-chained events.
        audit_dir = tmp_path / ".sdd" / "audit"
        audit_dir.mkdir(parents=True)
        log = AuditLog(audit_dir, key=_HMAC_KEY)
        log.log("task.created", "alice", "task", "T-1", {"role": "backend"})
        log.log("agent.spawned", "orchestrator", "agent", "A-1", {"task": "T-1"})
        log.log("task.completed", "alice", "task", "T-1", {"status": "ok"})

        # 2. Build the deterministic Ed25519 signing key.
        key = Ed25519PrivateKey.from_private_bytes(_SIGN_SEED)
        key_path = tmp_path / "sign.pem"
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        kms = FileBasedKMSAdapter(key_path, kid="test-audit-receipt-key")

        # 3. Build the valid receipt (write=False, we write manually).
        receipt = build_receipt(
            audit_dir,
            since=_SINCE,
            until=_UNTIL,
            key=_HMAC_KEY,
            kms_adapter=kms,
            write=False,
        )

        # Write the valid receipt.
        valid_path = OUT_DIR / "valid-receipt.json"
        valid_path.write_bytes(receipt.receipt_bytes)
        print(f"Wrote valid receipt: {valid_path}  ({len(receipt.receipt_bytes)} bytes)")

        # 4. Extract the public key PEM.
        pub_bytes = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        pubkey_path = OUT_DIR / "valid-receipt-key.pem"
        pubkey_path.write_bytes(pub_bytes)
        print(f"Wrote public key:   {pubkey_path}")

        # 5. Build the tampered copy — mutate actor in the first embedded event.
        doc = json.loads(receipt.receipt_bytes)
        first_event = doc["events"][0]
        original_actor = first_event["actor"]
        first_event["actor"] = "TAMPERED_ACTOR"
        print(f"Tampering events[0].actor: {original_actor!r} -> {first_event['actor']!r}")
        tampered_path = OUT_DIR / "tampered-receipt.json"
        # Use the same canonical JSON serialization as the valid receipt.
        tampered_bytes = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
        tampered_path.write_bytes(tampered_bytes)
        print(f"Wrote tampered receipt: {tampered_path}  ({len(tampered_bytes)} bytes)")


if __name__ == "__main__":
    main()
