"""Committed run-receipt test vectors are exercised by CI (issue #4204).

``tests/fixtures/receipt-vectors/`` carries a signed run receipt (built
from a deterministic Ed25519 key over a hermetic 3-event / 2-spine run)
and a tampered copy of it. These tests run the exact offline verifier and
the ``bernstein verify receipt`` CLI against those committed files on
every push, so the published evidence cannot rot into a decorative file:
a receipt that stops verifying - or a tampered copy that still looks
official - fails the build. The negative is demonstrated against a real
mutation, not assumed.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.verify_cmd import verify_cmd
from bernstein.core.replay.run_receipt import verify_run_receipt

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VECTORS = _REPO_ROOT / "tests" / "fixtures" / "receipt-vectors"
_VALID = _VECTORS / "valid-run-receipt.json"
_TAMPERED = _VECTORS / "tampered-run-receipt.json"
_PUBKEY = _VECTORS / "valid-run-receipt-key.pem"

_REQUIRED_TOP_LEVEL_FIELDS = (
    "schema_version",
    "receipt_type",
    "run_id",
    "subject",
    "journal",
    "spine",
    "signing",
)


def test_valid_vector_verifies_offline() -> None:
    """The committed valid receipt must verify from its bytes alone."""
    result = verify_run_receipt(_VALID.read_bytes())
    assert result.ok is True
    assert result.status == "ok"


def test_tampered_vector_is_detected() -> None:
    """The committed tampered copy must fail closed with a tamper verdict."""
    result = verify_run_receipt(_TAMPERED.read_bytes())
    assert result.ok is False
    assert result.status == "tampered"


def test_malformed_input_is_rejected(tmp_path: Path) -> None:
    """A file that is not a receipt at all must be reported malformed."""
    malformed = tmp_path / "not-a-receipt.json"
    malformed.write_text('{"this": "is not a run receipt"}', encoding="utf-8")
    result = verify_run_receipt(malformed.read_bytes())
    assert result.status == "malformed"


def test_valid_vector_verifies_with_pinned_public_key() -> None:
    """Pinning the committed public key reaches the provenance tier."""
    result = verify_run_receipt(_VALID.read_bytes(), public_key_pem=_PUBKEY.read_bytes())
    assert result.ok is True
    assert result.status == "ok"


def test_cli_exit_codes(tmp_path: Path) -> None:
    """``bernstein verify receipt`` maps verdicts to exit codes 0/1/2."""
    malformed = tmp_path / "not-a-receipt.json"
    malformed.write_text('{"this": "is not a run receipt"}', encoding="utf-8")

    ok = CliRunner().invoke(verify_cmd, ["receipt", str(_VALID)])
    assert ok.exit_code == 0, ok.output

    tampered = CliRunner().invoke(verify_cmd, ["receipt", str(_TAMPERED)])
    assert tampered.exit_code == 2, tampered.output
    assert "TAMPER DETECTED" in tampered.output

    bad = CliRunner().invoke(verify_cmd, ["receipt", str(malformed)])
    assert bad.exit_code == 1, bad.output
    assert "MALFORMED" in bad.output


def test_valid_vector_has_all_required_top_level_fields() -> None:
    """The committed valid receipt carries every required top-level field."""
    doc = json.loads(_VALID.read_text(encoding="utf-8"))
    for field in _REQUIRED_TOP_LEVEL_FIELDS:
        assert field in doc, f"missing required top-level field: {field}"


# ---------------------------------------------------------------------------
# COSE_Sign1 projection (#6207)
#
# The committed pair below is minted from the fixed seed in `_COSE_SEED` over
# the hermetic run `_rebuild_cose_vector` builds. Both are reproducible: the
# receipt embeds a projection of each journal row that drops `ts` and
# `elapsed_s`, so nothing wall-clock reaches the signed bytes (verified across
# a real multi-second gap, not assumed). That is what lets the `.cose` be a
# committed vector rather than a snapshot.
#
# The test re-signs the same run with today's encoder and demands byte
# equality, so a change to the COSE headers, the CBOR canonical mode, or the
# binding canonicalisation fails CI instead of silently invalidating a receipt
# already handed to an auditor.
# ---------------------------------------------------------------------------
_COSE_JSON = _VECTORS / "cose-vector-run-receipt.json"
_COSE_ENVELOPE = _VECTORS / "cose-vector-run-receipt.cose"
_COSE_PUBKEY = _VECTORS / "cose-vector-run-receipt-key.pem"

_COSE_RUN_ID = "cose-vector-run"
_COSE_SEED = bytes.fromhex("6207" * 16)[:32]
_COSE_KID = "cose-vector-key"
_COSE_HMAC_KEY = b"x" * 32


def _rebuild_cose_vector(tmp_path: Path) -> object:
    """Re-mint the committed vector with today's encoder, in a temp dir."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from bernstein.core.lineage.spine import LineageSpine
    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.replay.run_receipt import build_run_receipt
    from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

    sdd = tmp_path / ".sdd"
    journal = EventJournal(run_id=_COSE_RUN_ID, sdd_dir=sdd)
    journal.record("run_started", run_id=_COSE_RUN_ID)
    journal.record("task_claimed", task_id="T-1", role="backend")
    journal.record("run_completed", run_id=_COSE_RUN_ID, ticks=7)
    spine = LineageSpine(sdd / "lineage", run_id=_COSE_RUN_ID, hmac_key=_COSE_HMAC_KEY)
    spine.record(
        artifact_path="src/app.py",
        content=b"print('hi')\n",
        actor="backend",
        step_id="T-1",
        model="m1",
        timestamp=1111,
    )
    spine.record(
        artifact_path="tests/test_app.py",
        content=b"assert True\n",
        actor="qa",
        step_id="T-2",
        model="m1",
        timestamp=2222,
    )
    key_path = tmp_path / "cose-vector.pem"
    key = Ed25519PrivateKey.from_private_bytes(_COSE_SEED)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return build_run_receipt(_COSE_RUN_ID, sdd, FileBasedKMSAdapter(key_path, kid=_COSE_KID), write=False)


def test_committed_cose_vector_is_reproduced_byte_for_byte(tmp_path: Path) -> None:
    """Today's encoder must re-mint the committed envelope exactly.

    This is the drift detector. A generator run at test time would move both
    sides of the comparison at once and prove nothing.
    """
    rebuilt = _rebuild_cose_vector(tmp_path)

    assert rebuilt.cose_bytes == _COSE_ENVELOPE.read_bytes(), (
        "the COSE encoder no longer reproduces the committed vector"
    )
    assert rebuilt.receipt_bytes == _COSE_JSON.read_bytes(), (
        "the JSON encoder no longer reproduces the committed vector"
    )


def test_committed_cose_vector_verifies_against_its_json() -> None:
    """The committed pair must agree, and verify must say that it checked."""
    result = verify_run_receipt(_COSE_JSON.read_bytes(), cose_bytes=_COSE_ENVELOPE.read_bytes())

    assert result.ok is True
    assert result.status == "ok"
    assert result.cose_verified is True


def test_committed_cose_vector_verifies_with_the_pinned_public_key() -> None:
    """Pinning the key reaches the provenance tier with the envelope checked."""
    result = verify_run_receipt(
        _COSE_JSON.read_bytes(),
        public_key_pem=_COSE_PUBKEY.read_bytes(),
        cose_bytes=_COSE_ENVELOPE.read_bytes(),
    )

    assert result.ok is True
    assert result.cose_verified is True


def test_committed_cose_vector_is_a_standard_cose_sign1() -> None:
    """Decodable and verifiable by RFC 9052 alone - no bernstein in the loop.

    The committed bytes are what a third party would be handed, so the
    standard-conformance claim is made against the file, not against a
    freshly built object.
    """
    import cbor2
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    tagged = cbor2.loads(_COSE_ENVELOPE.read_bytes())
    assert isinstance(tagged, cbor2.CBORTag)
    assert tagged.tag == 18, "RFC 9052 section 2: COSE_Sign1 is tag 18"
    protected_bstr, _unprotected, payload, signature = tagged.value

    public_key = load_pem_public_key(_COSE_PUBKEY.read_bytes())
    sig_structure = ["Signature1", protected_bstr, b"", payload]
    public_key.verify(signature, cbor2.dumps(sig_structure, canonical=True))

    # And the payload is the binding the JSON's own subject digest names.
    import hashlib

    doc = json.loads(_COSE_JSON.read_text(encoding="utf-8"))
    assert hashlib.sha256(payload).hexdigest() == doc["subject"]["digest"]["sha256"]
