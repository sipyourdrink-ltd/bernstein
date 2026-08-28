#!/usr/bin/env python3
"""Re-mint the trust-record test vectors in this directory (issue #4692).

Run by hand from a source checkout, never by the test suite::

    uv run python tests/fixtures/trust-record-vectors/_build_trust_record_vectors.py

Records two real runs through the actual ``EventJournal`` write path (not a
hand-built journal), emits a single-execution Trust Record for the first and
a delegated parent+child pair (linked by ``parent_record_hash``) for the
second, and writes all three alongside the deterministic Ed25519 key that
signed them.

Why the vectors are committed rather than generated at test time
------------------------------------------------------------------
Mirrors ``tests/fixtures/receipt-vectors/_build_audit_receipt_vectors.py``: a
generator that drifts along with the encoder cannot detect the drift.
``tests/unit/test_trust_record_format_vectors.py`` re-verifies the
*committed* signatures and field surface with today's encoder; regenerating
inside that test would move both sides of the comparison at once and prove
nothing.

Running this script is therefore a deliberate re-mint, not a reproduction.
Re-mint only when the Trust Record format itself changed, and review the
result as new evidence -- the diff cannot tell you which part of it was the
encoder.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.observability.trust_record import TrustRecordEmitter
from bernstein.core.replay.journal import EventJournal

# Deterministic constants -- never reused outside this fixture generator.
_SIGN_SEED = b"k" * 32
_INSTALL_REV = "fixturefixture01"

OUT_DIR = Path(__file__).resolve().parent


def main() -> None:
    key = Ed25519PrivateKey.from_private_bytes(_SIGN_SEED)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    emitter = TrustRecordEmitter(
        install_rev_getter=lambda: _INSTALL_REV,
        get_private_key_pem=lambda: private_pem,
    )

    with tempfile.TemporaryDirectory() as tmp:
        sdd_dir = Path(tmp) / ".sdd"

        # 1. Single-execution run: a real 3-event journal through the
        # production EventJournal write path.
        solo_journal = EventJournal("trust-record-vector-solo", sdd_dir)
        solo_journal.record("run_started", role="backend")
        solo_journal.record("task_claimed", task_id="T-1")
        solo_journal.record("run_completed", status="ok")

        solo_output = emitter.emit_trust_record(solo_journal.path, "trust-record-vector-solo")
        solo_path = OUT_DIR / "single-execution-trust-record.json"
        solo_path.write_text(solo_output + "\n", encoding="utf-8")
        print(f"Wrote single-execution vector: {solo_path}  ({len(solo_output)} bytes)")

        # 2. Delegated pair: a parent run that spawns a child, one Trust
        # Record per execution hop, linked by parent_record_hash.
        parent_journal = EventJournal("trust-record-vector-parent", sdd_dir)
        parent_journal.record("run_started", role="orchestrator")
        parent_journal.record("agent_spawned", agent="child-1")

        parent_output = emitter.emit_trust_record(parent_journal.path, "trust-record-vector-parent")
        parent_path = OUT_DIR / "delegated-parent-trust-record.json"
        parent_path.write_text(parent_output + "\n", encoding="utf-8")
        print(f"Wrote delegated-parent vector: {parent_path}  ({len(parent_output)} bytes)")

        child_journal = EventJournal("trust-record-vector-child", sdd_dir)
        child_journal.record("run_started", role="backend")
        child_journal.record("run_completed", status="ok")

        child_output = emitter.emit_trust_record(
            child_journal.path,
            "trust-record-vector-child",
            parent_record=parent_output,
        )
        child_path = OUT_DIR / "delegated-child-trust-record.json"
        child_path.write_text(child_output + "\n", encoding="utf-8")
        print(f"Wrote delegated-child vector: {child_path}  ({len(child_output)} bytes)")

    # 3. Public key PEM -- pinned alongside as a second, independent check
    # that it agrees with the key recoverable from the self-certifying
    # did:key subject. Not required for verification itself.
    pubkey_path = OUT_DIR / "trust-record-vectors-key.pem"
    pubkey_path.write_bytes(public_pem)
    print(f"Wrote public key: {pubkey_path}")


if __name__ == "__main__":
    main()
