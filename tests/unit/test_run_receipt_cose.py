"""COSE_Sign1 envelope for the run receipt (issue #6207).

The audit receipt already projects a chain range into three envelopes an
off-the-shelf verifier validates (``audit_receipt.py``: ``cose``, ``intoto``,
``transparency``). The run receipt shipped only as detached-Ed25519-over-DSSE-PAE
JSON, so an evidence pipeline that speaks COSE_Sign1 - a transparency service
registering signed statements, a generic COSE verifier, an HSM that only signs
COSE - could ingest one bernstein receipt and not the other, although the same
key signs both.

What these tests pin:

  - the COSE payload is *byte-identical* to the binding bytes the JSON
    signature covers, so a third-party statement about ``run-receipt.cose`` is a
    statement about the journal head and the spine head, not about a filename;
  - a verifier with no ``bernstein`` import validates it (that is the whole
    point of emitting a standard envelope);
  - the bytes are reproducible, so the file can be a committed vector;
  - the two envelopes disagreeing is an error, not a warning - with an embedded
    payload there are two copies of the binding and they must agree;
  - no signing key means no ``.cose``, same as the JSON.

The signatures over the two envelopes are necessarily different preimages: DSSE
``pae(payload_type, binding)`` for the JSON, the RFC 9052 ``Sig_structure`` for
COSE. The shared fact is the payload, and that is what is asserted.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import cbor2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.replay.journal import EventJournal
from bernstein.core.replay.run_receipt import (
    RUN_RECEIPT_COSE_FILENAME,
    RUN_RECEIPT_PAYLOAD_TYPE,
    build_run_receipt,
    verify_run_receipt,
    write_run_receipt_if_configured,
)
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.lineage_kms import KMSAdapter

_RUN_ID = "run-receipt-cose-fixture"
_HMAC_KEY = b"x" * 32
_SIGN_SEED = b"i" * 32

#: RFC 9052 section 2: the COSE_Sign1 CBOR tag.
_COSE_SIGN1_TAG = 18
#: RFC 9052 section 3.1 header labels.
_LABEL_ALG = 1
_LABEL_CONTENT_TYPE = 3
_LABEL_KID = 4
#: RFC 9053 / IANA COSE Algorithms: EdDSA.
_ALG_EDDSA = -8


# ---------------------------------------------------------------------------
# Fixtures - mirrors tests/unit/test_run_receipt.py so the two agree on shape
# ---------------------------------------------------------------------------
def _seed_run(sdd_dir: Path, run_id: str = _RUN_ID) -> None:
    """A hermetic run: 3 journal events + 2 spine entries, no wall clock."""
    journal = EventJournal(run_id=run_id, sdd_dir=sdd_dir)
    journal.record("run_started", run_id=run_id)
    journal.record("task_claimed", task_id="T-1", role="backend")
    journal.record("run_completed", run_id=run_id, ticks=7)
    spine = LineageSpine(sdd_dir / "lineage", run_id=run_id, hmac_key=_HMAC_KEY)
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


def _kms(tmp_path: Path, seed: bytes = _SIGN_SEED) -> KMSAdapter:
    key_path = tmp_path / f"sign-{seed[:1].hex()}.pem"
    key = Ed25519PrivateKey.from_private_bytes(seed)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return FileBasedKMSAdapter(key_path, kid="test-run-receipt-key")


def _public_key(seed: bytes = _SIGN_SEED) -> Ed25519PublicKey:
    return Ed25519PrivateKey.from_private_bytes(seed).public_key()


def _decode_cose_sign1(cose_bytes: bytes) -> tuple[bytes, dict[Any, Any], bytes, bytes]:
    """Decode a COSE_Sign1 into ``(protected_bstr, unprotected, payload, sig)``."""
    tag = cbor2.loads(cose_bytes)
    assert isinstance(tag, cbor2.CBORTag), f"not a CBOR tag: {tag!r}"
    assert tag.tag == _COSE_SIGN1_TAG, f"wrong tag: {tag.tag}"
    protected_bstr, unprotected, payload, signature = tag.value
    return protected_bstr, unprotected, payload, signature


def _build(tmp_path: Path, **kwargs: Any) -> Any:
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)
    return build_run_receipt(_RUN_ID, sdd, _kms(tmp_path), **kwargs)


def _cose_path(receipt: Any) -> Path:
    assert receipt.receipt_path is not None, "receipt was not written"
    return receipt.receipt_path.parent / RUN_RECEIPT_COSE_FILENAME


# ---------------------------------------------------------------------------
# The load-bearing property: both envelopes sign the same bytes
# ---------------------------------------------------------------------------
def test_cose_payload_is_the_json_binding_bytes(tmp_path: Path) -> None:
    """The COSE payload must BE the binding the JSON signature covers.

    ``subject.digest.sha256`` in the JSON is ``sha256(binding_bytes)``
    (``run_receipt.py:697``), so digesting the COSE payload and comparing
    proves byte-identity without reaching for a private function. If these
    ever diverge, a transparency receipt over the ``.cose`` would attest
    something other than the run the JSON describes - which is the entire
    reason for emitting it.
    """
    receipt = _build(tmp_path)
    cose_path = _cose_path(receipt)

    assert cose_path.exists(), f"no COSE envelope written beside {receipt.receipt_path}"
    _protected, _unprotected, payload, _sig = _decode_cose_sign1(cose_path.read_bytes())

    doc = json.loads(receipt.receipt_bytes)
    assert hashlib.sha256(payload).hexdigest() == doc["subject"]["digest"]["sha256"], (
        "the COSE payload is not the bytes the JSON signature covers"
    )


def test_cose_protected_header_pins_alg_content_type_and_kid(tmp_path: Path) -> None:
    """A generic verifier reads the algorithm and key id out of the header."""
    receipt = _build(tmp_path)
    protected_bstr, _unprotected, _payload, _sig = _decode_cose_sign1(_cose_path(receipt).read_bytes())

    protected = cbor2.loads(protected_bstr)
    doc = json.loads(receipt.receipt_bytes)
    assert protected[_LABEL_ALG] == _ALG_EDDSA
    assert protected[_LABEL_CONTENT_TYPE] == RUN_RECEIPT_PAYLOAD_TYPE, (
        "content type must domain-separate this from the audit receipt"
    )
    assert protected[_LABEL_KID].decode("utf-8") == doc["signing"]["key_id"]


# ---------------------------------------------------------------------------
# A verifier with no bernstein code
# ---------------------------------------------------------------------------
def test_cose_receipt_verifies_with_a_generic_cose_verifier(tmp_path: Path) -> None:
    """RFC 9052 section 4.4 verification, implemented here from the spec.

    Deliberately imports nothing from ``bernstein``: if this passes, the file
    is a COSE_Sign1 and not merely our own CBOR. The ``Sig_structure`` is
    rebuilt from the decoded envelope rather than from anything the producer
    kept, so a mismatch between what was signed and what shipped fails here.
    """
    receipt = _build(tmp_path)
    protected_bstr, _unprotected, payload, signature = _decode_cose_sign1(_cose_path(receipt).read_bytes())

    sig_structure = ["Signature1", protected_bstr, b"", payload]
    _public_key().verify(signature, cbor2.dumps(sig_structure, canonical=True))


def test_a_flipped_cose_signature_byte_fails_generic_verification(tmp_path: Path) -> None:
    """The positive control above is only meaningful if this fails."""
    from cryptography.exceptions import InvalidSignature

    receipt = _build(tmp_path)
    protected_bstr, _unprotected, payload, signature = _decode_cose_sign1(_cose_path(receipt).read_bytes())
    broken = bytearray(signature)
    broken[0] ^= 0x01

    sig_structure = ["Signature1", protected_bstr, b"", payload]
    with pytest.raises(InvalidSignature):
        _public_key().verify(bytes(broken), cbor2.dumps(sig_structure, canonical=True))


# ---------------------------------------------------------------------------
# Determinism - the file can be a committed vector
# ---------------------------------------------------------------------------
def test_cose_receipt_bytes_are_identical_across_builds(tmp_path: Path) -> None:
    """Deterministic CBOR + RFC 8032 Ed25519, and no wall clock in the binding."""
    first = _build(tmp_path / "a")
    second = _build(tmp_path / "b")

    assert _cose_path(first).read_bytes() == _cose_path(second).read_bytes()


# ---------------------------------------------------------------------------
# Disagreement between the two copies of the binding is an error
# ---------------------------------------------------------------------------
def test_verify_reports_cose_json_mismatch_as_error(tmp_path: Path) -> None:
    """An embedded payload means two copies of the binding; they must agree.

    This is the cost of choosing embedded over detached, so it is policed
    rather than documented.
    """
    receipt = _build(tmp_path)
    protected_bstr, unprotected, payload, signature = _decode_cose_sign1(_cose_path(receipt).read_bytes())
    tampered_payload = payload[:-1] + bytes([payload[-1] ^ 0x01])
    tampered = cbor2.dumps(
        cbor2.CBORTag(_COSE_SIGN1_TAG, [protected_bstr, unprotected, tampered_payload, signature]),
        canonical=True,
    )

    result = verify_run_receipt(receipt.receipt_bytes, cose_bytes=tampered)

    assert result.ok is False
    assert result.status == "tampered"
    # Specifically the payload comparison, which runs before the signature
    # check. Asserting only on "cose" would also match the signature error and
    # would let the comparison be deleted with this test still green.
    assert any("payload" in e.lower() for e in result.errors), result.errors


def test_verify_accepts_a_matching_cose_envelope(tmp_path: Path) -> None:
    """The happy path, and it must report that it actually checked."""
    receipt = _build(tmp_path)

    result = verify_run_receipt(receipt.receipt_bytes, cose_bytes=_cose_path(receipt).read_bytes())

    assert result.ok is True
    assert result.cose_verified is True


def test_verify_without_cose_bytes_does_not_claim_it_checked(tmp_path: Path) -> None:
    """Absence must be distinguishable from a pass - nobody may read a receipt
    verified JSON-only as one whose COSE envelope was checked."""
    receipt = _build(tmp_path)

    result = verify_run_receipt(receipt.receipt_bytes)

    assert result.ok is True
    assert result.cose_verified is None


def test_verify_rejects_a_cose_envelope_signed_by_another_key(tmp_path: Path) -> None:
    """A COSE file whose payload matches but whose signature does not verify
    against the receipt's own embedded key is not evidence of anything."""
    receipt = _build(tmp_path)
    protected_bstr, unprotected, payload, _sig = _decode_cose_sign1(_cose_path(receipt).read_bytes())
    other = Ed25519PrivateKey.from_private_bytes(b"o" * 32)
    sig_structure = ["Signature1", protected_bstr, b"", payload]
    forged = other.sign(cbor2.dumps(sig_structure, canonical=True))
    reshaped = cbor2.dumps(
        cbor2.CBORTag(_COSE_SIGN1_TAG, [protected_bstr, unprotected, payload, forged]),
        canonical=True,
    )

    result = verify_run_receipt(receipt.receipt_bytes, cose_bytes=reshaped)

    assert result.ok is False
    assert result.status == "tampered"


def test_verify_reports_malformed_cose_without_crashing(tmp_path: Path) -> None:
    """Arbitrary bytes in the .cose slot are a malformed input, not a traceback."""
    receipt = _build(tmp_path)

    result = verify_run_receipt(receipt.receipt_bytes, cose_bytes=b"\xff not cbor")

    assert result.ok is False
    assert result.errors


# ---------------------------------------------------------------------------
# No key, no file
# ---------------------------------------------------------------------------
def test_no_cose_file_without_a_signing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``write_run_receipt_if_configured`` is a no-op without a key, and that
    must extend to the COSE envelope - an unsigned ``.cose`` would be a file
    that looks like evidence and is not."""
    monkeypatch.delenv("BERNSTEIN_RUN_RECEIPT_SIGNING_KEY_PATH", raising=False)
    monkeypatch.delenv("BERNSTEIN_RUN_RECEIPT_SIGNING_ENV_VAR", raising=False)
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)

    assert write_run_receipt_if_configured(_RUN_ID, sdd) is None
    assert list(sdd.rglob(RUN_RECEIPT_COSE_FILENAME)) == []


def test_write_disabled_emits_neither_file(tmp_path: Path) -> None:
    """``write=False`` must not leave a stray COSE envelope behind."""
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)

    receipt = build_run_receipt(_RUN_ID, sdd, _kms(tmp_path), write=False)

    assert receipt.receipt_path is None
    assert list(sdd.rglob(RUN_RECEIPT_COSE_FILENAME)) == []


def test_verify_rejects_a_validly_signed_cose_over_the_wrong_binding(tmp_path: Path) -> None:
    """The payload comparison has to be load-bearing on its own.

    A tampered payload carrying a *stale* signature is also caught by the
    signature check, so that case cannot prove the comparison runs at all.
    Here the envelope is correctly signed over a different binding - what a
    producer bug would emit, or anyone holding the signing key - so nothing
    but comparing the payload against the recomputed binding catches it.
    """
    receipt = _build(tmp_path)
    protected_bstr, unprotected, payload, _sig = _decode_cose_sign1(_cose_path(receipt).read_bytes())
    wrong = payload.replace(b'"journal_event_count":3', b'"journal_event_count":4')
    assert wrong != payload, "the substitution must actually alter the binding"

    key = Ed25519PrivateKey.from_private_bytes(_SIGN_SEED)
    signature = key.sign(cbor2.dumps(["Signature1", protected_bstr, b"", wrong], canonical=True))
    forged = cbor2.dumps(
        cbor2.CBORTag(_COSE_SIGN1_TAG, [protected_bstr, unprotected, wrong, signature]),
        canonical=True,
    )

    result = verify_run_receipt(receipt.receipt_bytes, cose_bytes=forged)

    assert result.ok is False
    assert result.status == "tampered"
    assert any("payload" in e.lower() for e in result.errors), result.errors


def test_verify_rejects_a_cose_envelope_typed_as_another_receipt_family(
    tmp_path: Path,
) -> None:
    """The content type is what domain-separates the envelopes the same key
    signs, so an envelope typed as an audit receipt is not a run receipt even
    when its payload and signature are both correct.

    Built the hard way - a fresh protected header and a genuine signature over
    it - because a header edit alone would break the signature and be caught
    for the wrong reason.
    """
    receipt = _build(tmp_path)
    _protected, unprotected, payload, _sig = _decode_cose_sign1(_cose_path(receipt).read_bytes())
    wrong_header = cbor2.dumps(
        {
            _LABEL_ALG: _ALG_EDDSA,
            _LABEL_CONTENT_TYPE: "application/vnd.bernstein.audit-receipt+json",
            _LABEL_KID: b"test-run-receipt-key",
        },
        canonical=True,
    )
    key = Ed25519PrivateKey.from_private_bytes(_SIGN_SEED)
    signature = key.sign(cbor2.dumps(["Signature1", wrong_header, b"", payload], canonical=True))
    mistyped = cbor2.dumps(
        cbor2.CBORTag(_COSE_SIGN1_TAG, [wrong_header, unprotected, payload, signature]),
        canonical=True,
    )

    result = verify_run_receipt(receipt.receipt_bytes, cose_bytes=mistyped)

    assert result.ok is False
    assert result.status == "tampered"
    assert any("content type" in e.lower() for e in result.errors), result.errors
