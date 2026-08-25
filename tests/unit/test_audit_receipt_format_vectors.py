"""Committed audit-receipt test vectors are exercised by CI (issue #4204).

``tests/fixtures/receipt-vectors/`` carries a signed audit receipt (three
HMAC-chained events projected into COSE, DSSE/in-toto and transparency
blocks), a tampered copy of it, and the Ed25519 public key it was signed
under. These tests point the production verifiers at those committed files
on every push, so the published evidence cannot rot into a decorative file.

The vectors exist to catch encoding drift, and two tests here are what make
that possible. ``test_current_encoder_reproduces_the_committed_receipt_bytes``
re-signs the frozen event range with today's encoder and demands the result
match bytes that were signed and published long before: change the canonical
JSON, the event JSONL, the COSE headers, the DSSE pre-authentication encoding
or the Merkle hashing, and the re-signed receipt diverges from a signature an
auditor already holds. The subject-binding test narrows that to the chain head
alone, so a failure says which layer moved. The negative side is demonstrated
against a real mutation, not assumed: one embedded event was altered, and
every signed format must fail on it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.security.audit_receipt import (
    ALL_FORMATS,
    materialize_receipt,
    receipt_events_head,
)
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VECTORS = _REPO_ROOT / "tests" / "fixtures" / "receipt-vectors"
_VALID = _VECTORS / "valid-receipt.json"
_TAMPERED = _VECTORS / "tampered-receipt.json"
_PUBKEY = _VECTORS / "valid-receipt-key.pem"
_SCHEMA = _REPO_ROOT / "schemas" / "audit-receipt-v1.json"

#: The standalone verifier an external auditor runs. ``bernstein audit
#: receipt verify`` shells to this same script and propagates its exit code,
#: so verifying against it is verifying the operator-facing surface.
_VERIFIER_SCRIPT = _REPO_ROOT / "tools" / "verify_audit_receipt.py"

#: Deterministic signing seed the vectors were minted under. Pinned by
#: ``tests/fixtures/receipt-vectors/_build_audit_receipt_vectors.py``; a
#: test-only key, published alongside the vectors it signs.
_SIGN_SEED = b"i" * 32


def _load_verifier() -> Any:
    """Load the standalone verifier as a module (it imports no bernstein)."""
    spec = importlib.util.spec_from_file_location("verify_audit_receipt", _VERIFIER_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so its dataclasses can resolve the module namespace.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _verify(receipt: Path, *, pinned_pem: bytes | None = None) -> Any:
    """Run every production check against ``receipt`` and return the verdict."""
    verifier = _load_verifier()
    return verifier.run_verify(
        receipt_path=receipt,
        which="all",
        pinned_jwk=None,
        pinned_pem=pinned_pem,
        verbose=False,
        stream=io.StringIO(),
    )


def _check(result: Any, name: str) -> Any:
    """Return the named check, failing loudly when the verifier skipped it."""
    for check in result.checks:
        if check.name == name:
            return check
    pytest.fail(f"verifier never ran the {name!r} check; ran {[c.name for c in result.checks]}")


def test_committed_audit_receipt_verifies_against_its_published_key() -> None:
    """The committed receipt verifies at the provenance tier, not just TOFU."""
    result = _verify(_VALID, pinned_pem=_PUBKEY.read_bytes())
    assert result.ok is True, [(c.name, c.detail) for c in result.checks if not c.ok]
    # "pinned-pem" is the verifier's word for: the embedded key matched the
    # out-of-band key, so this pass proves who signed, not merely self-consistency.
    assert _check(result, "public_key").detail == "pinned-pem"
    for fmt in ALL_FORMATS:
        assert _check(result, fmt).ok is True


def test_committed_audit_receipt_reaches_only_the_integrity_tier_without_a_pin() -> None:
    """Unpinned verification is trust-on-first-use and says so."""
    result = _verify(_VALID)
    assert result.ok is True
    assert _check(result, "public_key").detail == "trust-on-first-use"


def test_tampered_audit_receipt_fails_subject_binding_with_a_divergent_head() -> None:
    """The tampered copy fails on the binding check, naming the head mismatch."""
    result = _verify(_TAMPERED)
    assert result.ok is False

    binding = _check(result, "subject_binding")
    assert binding.ok is False
    assert "chain tampered" in binding.detail

    # The failure is a recomputed-head divergence, not a stray signature error:
    # the mutated event range no longer hashes to the digest that was signed.
    signed_subject = json.loads(_VALID.read_text(encoding="utf-8"))["subject"]["digest"]["sha256"]
    tampered_doc = json.loads(_TAMPERED.read_text(encoding="utf-8"))
    assert receipt_events_head(tampered_doc["events"]) != signed_subject
    assert tampered_doc["subject"]["digest"]["sha256"] == signed_subject


def test_tampering_one_embedded_event_collapses_every_signed_format() -> None:
    """Mutating one event must break all three formats, not just one."""
    result = _verify(_TAMPERED)
    failed = {check.name for check in result.checks if not check.ok}
    assert set(ALL_FORMATS) <= failed, f"formats that still verified: {set(ALL_FORMATS) - failed}"


def test_tampered_vector_differs_from_the_valid_one_in_exactly_one_field() -> None:
    """The negative vector is a single-field mutation, not a different file.

    This is what makes the collapse above attributable: three formats stop
    verifying because one actor string moved, not because the file was
    rebuilt into something else.
    """
    valid = json.loads(_VALID.read_text(encoding="utf-8"))
    tampered = json.loads(_TAMPERED.read_text(encoding="utf-8"))

    assert tampered["events"][0].pop("actor") != valid["events"][0].pop("actor")
    assert tampered == valid, "the tampered vector diverges beyond events[0].actor"


def test_committed_audit_receipt_subject_binds_the_head_under_the_current_encoder() -> None:
    """Today's event canonicalization must still hash to the signed subject.

    The subject digest was fixed when the vector was signed. Recomputing it
    with the current ``_events_jsonl_bytes`` encoding is the compatibility
    check: any change to that encoding moves the head away from a digest that
    is already signed and published, and fails here.
    """
    doc = json.loads(_VALID.read_text(encoding="utf-8"))
    recomputed = receipt_events_head(doc["events"])
    assert recomputed == doc["subject"]["digest"]["sha256"]
    assert recomputed == doc["range"]["head_sha256"]


def test_committed_vectors_survive_checkout_without_eol_translation() -> None:
    """A signature is over bytes, so a CRLF checkout would invalidate them."""
    for path in (_VALID, _TAMPERED, _PUBKEY):
        assert b"\r\n" not in path.read_bytes(), (
            f"{path.name} was checked out with CRLF endings; .gitattributes must keep "
            "tests/fixtures/receipt-vectors/ out of end-of-line translation"
        )


def test_current_encoder_reproduces_the_committed_receipt_bytes(tmp_path: Path) -> None:
    """Re-signing the frozen range today must reproduce the committed bytes.

    The events, the window, the chain head and the signing key are all read
    back from the committed vector, so the only variable is the encoder. An
    encoding change anywhere in the receipt serialization, the COSE headers,
    the DSSE pre-authentication encoding or the Merkle hashing breaks
    byte-equality with a receipt that was already signed and handed out.
    """
    committed_bytes = _VALID.read_bytes()
    doc = json.loads(committed_bytes)

    key = Ed25519PrivateKey.from_private_bytes(_SIGN_SEED)
    key_path = tmp_path / "sign.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    # Fail on a re-mint under a different key before comparing opaque bytes.
    published_pub = serialization.load_pem_public_key(_PUBKEY.read_bytes())
    assert key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    ) == published_pub.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    ), "the committed vectors were minted under a different key than _SIGN_SEED"

    resigned = materialize_receipt(
        tmp_path,
        since=doc["range"]["since"],
        until=doc["range"]["until"],
        rebuilt=doc["events"],
        head_hmac=doc["range"]["head_hmac"],
        head_sha256=receipt_events_head(doc["events"]),
        kms_adapter=FileBasedKMSAdapter(key_path, kid=doc["signing"]["key_id"]),
        requested=ALL_FORMATS,
        subject_name=doc["subject"]["name"],
        online_rekor=False,
        output_dir=None,
        write=False,
    )
    assert resigned.receipt_bytes == committed_bytes


def test_verifier_exit_codes_match_the_documented_contract(tmp_path: Path) -> None:
    """``0`` verified, ``1`` a check failed, ``2`` unreadable input."""
    verifier = _load_verifier()
    missing = tmp_path / "absent.json"

    assert verifier.main(["--receipt", str(_VALID), "--public-key", str(_PUBKEY)]) == 0
    assert verifier.main(["--receipt", str(_TAMPERED)]) == 1
    assert verifier.main(["--receipt", str(missing)]) == 2


def test_committed_audit_receipt_conforms_to_the_published_schema() -> None:
    """The vector still validates against ``schemas/audit-receipt-v1.json``."""
    import jsonschema

    jsonschema.validate(
        json.loads(_VALID.read_text(encoding="utf-8")),
        json.loads(_SCHEMA.read_text(encoding="utf-8")),
    )
