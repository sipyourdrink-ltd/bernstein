"""Unit tests for ``core.replay.diagnosis_receipt`` (#2928).

Covers the acceptance criteria of the signed diagnosis receipt: empirical
byte-identity across independent invocations, offline verification via
Ed25519 + operator HMAC + full re-derivation, and tamper collapse for both
the receipt and the underlying journal.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.persistence.lineage_signer import (
    Ed25519FileKeySigner,
    Ed25519PublicKeyVerifier,
)
from bernstein.core.replay.diagnose import SignalPredicate, diagnose_run
from bernstein.core.replay.diagnose_signals import replay_signal
from bernstein.core.replay.diagnosis_receipt import (
    DiagnosisReceiptError,
    build_diagnosis_receipt,
    verify_diagnosis_receipt,
)
from bernstein.core.replay.diff import REASON_CODE_FIRST_FAILING_TOOL_RESULT
from bernstein.core.replay.journal import EventJournal

if TYPE_CHECKING:
    from pathlib import Path

AUDIT_KEY = b"unit-test-audit-hmac-key-32bytes"
BAD_HASH = hashlib.sha256(b"the-offending-content").hexdigest()


def _write_keypair(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """Write a fresh Ed25519 keypair; returns (private_pem, public_pem)."""
    private = Ed25519PrivateKey.generate()
    priv_path = tmp_path / f"{name}-priv.pem"
    pub_path = tmp_path / f"{name}-pub.pem"
    priv_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    pub_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return priv_path, pub_path


def _predicate() -> SignalPredicate:
    return SignalPredicate(
        predicate_id="incident/v1",
        params={"kind": "incident", "case_id": "inc-x", "needles": [BAD_HASH]},
        default_reason_code=REASON_CODE_FIRST_FAILING_TOOL_RESULT,
        needles=(BAD_HASH,),
    )


def _seed(tmp_path: Path, run_id: str = "run-1", *, bad_step: int = 2) -> Path:
    sdd = tmp_path / ".sdd"
    journal = EventJournal(run_id, sdd)
    for i in range(5):
        if i == bad_step:
            journal.record("tool_result", step=i, content_hash=f"sha256:{BAD_HASH}")
        else:
            journal.record("tick", step=i)
    return journal.path


def _tamper_payload(journal_path: Path, index: int) -> None:
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[index])
    row["step"] = "tampered"
    lines[index] = json.dumps(row, default=str)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build(tmp_path: Path, journal_path: Path, signer_path: Path, *, write: bool = True):
    result = diagnose_run(journal_path, _predicate(), run_id="run-1")
    return build_diagnosis_receipt(
        result,
        chain_head_hmac="head-hmac-anchor",
        signer=Ed25519FileKeySigner.from_path(signer_path),
        audit_key=AUDIT_KEY,
        output_dir=tmp_path / "evidence" if write else None,
        write=write,
    )


def test_diagnosis_is_byte_identical_across_invocations(tmp_path: Path) -> None:
    """Two independent invocations over the same journal produce identical
    receipt bytes, culprit fields, predicate hash, and receipt sha256."""
    journal_path = _seed(tmp_path)
    priv, _pub = _write_keypair(tmp_path, "signer")

    first = _build(tmp_path, journal_path, priv, write=False)
    second = _build(tmp_path, journal_path, priv, write=False)

    assert first.receipt_bytes == second.receipt_bytes
    assert first.sha256 == second.sha256
    assert first.receipt_hash == second.receipt_hash
    assert first.receipt["culprit_index"] == second.receipt["culprit_index"] == 2
    assert first.receipt["culprit_step_hash"] == second.receipt["culprit_step_hash"]
    assert first.receipt["predicate_hash"] == second.receipt["predicate_hash"]


def test_diagnosis_receipt_survives_offline_verification(tmp_path: Path) -> None:
    """Build -> write -> verify with pinned public key + audit key: ok."""
    journal_path = _seed(tmp_path)
    priv, pub = _write_keypair(tmp_path, "signer")

    receipt = _build(tmp_path, journal_path, priv)
    assert receipt.receipt_path is not None
    assert receipt.receipt_path.is_file()

    outcome = verify_diagnosis_receipt(
        receipt.receipt_path,
        journal_path=journal_path,
        verifier=Ed25519PublicKeyVerifier.from_path(pub),
        audit_key=AUDIT_KEY,
    )
    assert outcome.ok, outcome.reason
    assert outcome.hmac_checked is True


def test_verification_without_audit_key_reports_hmac_unchecked(tmp_path: Path) -> None:
    journal_path = _seed(tmp_path)
    priv, pub = _write_keypair(tmp_path, "signer")
    receipt = _build(tmp_path, journal_path, priv)
    assert receipt.receipt_path is not None

    outcome = verify_diagnosis_receipt(
        receipt.receipt_path,
        journal_path=journal_path,
        verifier=Ed25519PublicKeyVerifier.from_path(pub),
        audit_key=None,
    )
    assert outcome.ok
    assert outcome.hmac_checked is False


def test_receipt_signature_round_trips_and_wrong_key_fails(tmp_path: Path) -> None:
    """The embedded key verifies; a pinned wrong key is rejected."""
    journal_path = _seed(tmp_path)
    priv, _pub = _write_keypair(tmp_path, "signer")
    _wrong_priv, wrong_pub = _write_keypair(tmp_path, "wrong")
    receipt = _build(tmp_path, journal_path, priv)
    assert receipt.receipt_path is not None

    embedded = verify_diagnosis_receipt(receipt.receipt_path, journal_path=journal_path, audit_key=AUDIT_KEY)
    assert embedded.ok, embedded.reason

    wrong = verify_diagnosis_receipt(
        receipt.receipt_path,
        journal_path=journal_path,
        verifier=Ed25519PublicKeyVerifier.from_path(wrong_pub),
        audit_key=AUDIT_KEY,
    )
    assert not wrong.ok
    assert "signature" in wrong.reason


def test_tampered_receipt_rejected(tmp_path: Path) -> None:
    """Any mutated body field breaks the content address."""
    journal_path = _seed(tmp_path)
    priv, _pub = _write_keypair(tmp_path, "signer")
    receipt = _build(tmp_path, journal_path, priv)
    assert receipt.receipt_path is not None

    raw = json.loads(receipt.receipt_path.read_text(encoding="utf-8"))
    raw["culprit_index"] = 0  # point the finger elsewhere
    receipt.receipt_path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")

    outcome = verify_diagnosis_receipt(receipt.receipt_path, journal_path=journal_path, audit_key=AUDIT_KEY)
    assert not outcome.ok
    assert "does not recompute" in outcome.reason


def test_tampered_journal_at_culprit_fails_verification(tmp_path: Path) -> None:
    """A journal mutated at the culprit step no longer verifies the receipt."""
    journal_path = _seed(tmp_path, bad_step=2)
    priv, _pub = _write_keypair(tmp_path, "signer")
    receipt = _build(tmp_path, journal_path, priv)
    assert receipt.receipt_path is not None

    _tamper_payload(journal_path, 2)

    outcome = verify_diagnosis_receipt(receipt.receipt_path, journal_path=journal_path, audit_key=AUDIT_KEY)
    assert not outcome.ok


def test_tampered_journal_after_culprit_fails_verification(tmp_path: Path) -> None:
    """A journal mutated after the culprit step also fails verification."""
    journal_path = _seed(tmp_path, bad_step=2)
    priv, _pub = _write_keypair(tmp_path, "signer")
    receipt = _build(tmp_path, journal_path, priv)
    assert receipt.receipt_path is not None

    _tamper_payload(journal_path, 4)

    outcome = verify_diagnosis_receipt(receipt.receipt_path, journal_path=journal_path, audit_key=AUDIT_KEY)
    assert not outcome.ok


def test_garbage_line_injected_into_journal_rejected_on_verify(tmp_path: Path) -> None:
    """A receipt stops verifying once any unparsable line enters the journal:
    the exact-bytes pin catches it first, and the strict diagnostic loader
    refuses the filtered sequence regardless (bot-ack: 3705961185)."""
    journal_path = _seed(tmp_path)
    priv, _pub = _write_keypair(tmp_path, "signer")
    receipt = _build(tmp_path, journal_path, priv)
    assert receipt.receipt_path is not None

    with journal_path.open("a", encoding="utf-8") as f:
        f.write("{torn trailing write\n")

    outcome = verify_diagnosis_receipt(receipt.receipt_path, journal_path=journal_path, audit_key=AUDIT_KEY)
    assert not outcome.ok

    from bernstein.core.replay.diagnose import DiagnoseError

    with pytest.raises(DiagnoseError, match="unparsable line"):
        diagnose_run(journal_path, _predicate(), run_id="run-1")


def test_absent_journal_fails_verification(tmp_path: Path) -> None:
    journal_path = _seed(tmp_path)
    priv, _pub = _write_keypair(tmp_path, "signer")
    receipt = _build(tmp_path, journal_path, priv)
    assert receipt.receipt_path is not None

    journal_path.unlink()

    outcome = verify_diagnosis_receipt(receipt.receipt_path, journal_path=journal_path, audit_key=AUDIT_KEY)
    assert not outcome.ok
    assert "absent" in outcome.reason


def test_clean_diagnosis_refuses_a_receipt(tmp_path: Path) -> None:
    """A clean (no-culprit) diagnosis emits no receipt, by contract."""
    sdd = tmp_path / ".sdd"
    journal = EventJournal("run-clean", sdd)
    journal.record("tick", step=0)
    priv, _pub = _write_keypair(tmp_path, "signer")

    result = diagnose_run(journal.path, replay_signal(), run_id="run-clean")
    assert result.located is False

    with pytest.raises(DiagnosisReceiptError, match="no culprit"):
        build_diagnosis_receipt(
            result,
            chain_head_hmac="head",
            signer=Ed25519FileKeySigner.from_path(priv),
            audit_key=AUDIT_KEY,
            output_dir=tmp_path / "evidence",
        )
