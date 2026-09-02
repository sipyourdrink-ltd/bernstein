"""Evidence-envelope v1: schema, canonical form and the committed vector (#5063).

This slice ships no producer and no verifier, so the only thing standing
between the envelope format and silent drift is the committed vector plus
these tests. Two of them carry the weight:

``test_reserializing_the_committed_vector_reproduces_its_bytes`` parses the
committed file and re-encodes it with today's canonicaliser. The file was
minted once, hashed, and published; if the canonical encoding moves -- a
different key order, a different separator, ``ensure_ascii`` flipping -- the
re-encoded bytes stop matching bytes an auditor already holds, and this test
says so before a producer is written against the moved encoding.

``test_schema_rejects_an_envelope_without_a_coverage_section`` pins the one
structural rule the format exists for: an envelope that stays silent about
what it does not cover must not validate. Coverage is required, and a
coverage section that omits its ``uncovered`` list is silence wearing a
section header, so that is refused too.

The signature check is offline and uses ``cryptography`` directly rather than
any repository verifier: there is no envelope verifier yet, and a vector
whose signature nobody ever checks is decoration.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from bernstein.core.security.evidence_envelope import (
    EVIDENCE_ENVELOPE_SCHEMA_VERSION,
    EVIDENCE_ENVELOPE_TYPE,
    canonical_binding_bytes,
    canonical_envelope_bytes,
    envelope_binding,
    envelope_digest,
    envelope_signing_input,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA = _REPO_ROOT / "schemas" / "evidence-envelope-v1.json"
_VECTORS = _REPO_ROOT / "tests" / "fixtures" / "evidence-envelope-vectors"
_VECTOR = _VECTORS / "partial-coverage-envelope.json"
_VECTOR_SHA256 = _VECTORS / "partial-coverage-envelope.sha256"
_PUBKEY = _VECTORS / "evidence-envelope-vectors-key.pem"
_BUILDER = _VECTORS / "_build_evidence_envelope_vectors.py"


def _schema() -> dict[str, Any]:
    return json.loads(_SCHEMA.read_text(encoding="utf-8"))


def _vector() -> dict[str, Any]:
    return json.loads(_VECTOR.read_text(encoding="utf-8"))


def _load_builder() -> Any:
    """Import the vector generator as a module (it must not build on import)."""
    spec = importlib.util.spec_from_file_location("_build_evidence_envelope_vectors", _BUILDER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1-3: the canonical form is what is on disk
# ---------------------------------------------------------------------------


def test_reserializing_the_committed_vector_reproduces_its_bytes() -> None:
    """The committed file *is* its own canonical encoding, byte for byte."""
    on_disk = _VECTOR.read_bytes()
    assert canonical_envelope_bytes(json.loads(on_disk)) == on_disk


def test_committed_vector_matches_its_published_digest() -> None:
    """The sidecar digest is over the committed bytes, not over a re-encoding."""
    published = _VECTOR_SHA256.read_text(encoding="utf-8").split()[0]
    assert hashlib.sha256(_VECTOR.read_bytes()).hexdigest() == published
    assert envelope_digest(_vector()) == f"sha256:{published}"


def test_canonical_bytes_ignore_input_key_order() -> None:
    """Two dicts differing only in insertion order encode identically."""
    envelope = _vector()
    shuffled = {key: envelope[key] for key in reversed(list(envelope))}
    assert canonical_envelope_bytes(shuffled) == canonical_envelope_bytes(envelope)


# ---------------------------------------------------------------------------
# 4-6: coverage is required, and an empty coverage section is not coverage
# ---------------------------------------------------------------------------


def test_committed_vector_validates_against_the_schema() -> None:
    jsonschema.validate(_vector(), _schema())


def test_schema_rejects_an_envelope_without_a_coverage_section() -> None:
    envelope = _vector()
    del envelope["coverage"]
    with pytest.raises(jsonschema.ValidationError, match="coverage"):
        jsonschema.validate(envelope, _schema())


def test_schema_rejects_a_coverage_section_that_omits_the_uncovered_list() -> None:
    """A coverage section with no ``uncovered`` member says nothing at all."""
    envelope = _vector()
    del envelope["coverage"]["uncovered"]
    with pytest.raises(jsonschema.ValidationError, match="uncovered"):
        jsonschema.validate(envelope, _schema())


def test_vector_coverage_names_every_action_it_does_not_cover() -> None:
    """The counts and the ``uncovered`` list are one statement, not two."""
    coverage = _vector()["coverage"]
    uncovered = coverage["uncovered"]
    assert coverage["actions_declared"] - coverage["actions_covered"] == len(uncovered)
    assert uncovered, "a fully-covered vector cannot demonstrate the gap-declaration rule"
    for entry in uncovered:
        assert entry["reason"].strip(), f"{entry['action']} is declared uncovered with no reason"


# ---------------------------------------------------------------------------
# 7-8: the signature covers every section and nothing else
# ---------------------------------------------------------------------------


def test_signature_section_is_outside_the_binding_it_signs() -> None:
    envelope = _vector()
    binding = envelope_binding(envelope)
    assert "signature" not in binding
    assert set(binding) == set(envelope) - {"signature"}


def test_committed_signature_verifies_over_the_canonical_binding() -> None:
    envelope = _vector()
    header_b64, _, sig_b64 = envelope["signature"]["jws"].partition("..")
    public_key = serialization.load_pem_public_key(_PUBKEY.read_bytes())
    assert isinstance(public_key, Ed25519PublicKey)
    signature = _b64url_decode(sig_b64)
    public_key.verify(signature, envelope_signing_input(header_b64=header_b64, envelope=envelope))


def test_signing_input_payload_is_the_canonical_binding_bytes() -> None:
    """The JWS payload segment is exactly the canonical binding bytes, base64url-encoded.

    ``envelope_signing_input`` and ``canonical_binding_bytes`` are two entry points
    onto the same preimage; a reader who only has the compact JWS must be able to
    recompute one from the other.
    """
    envelope = _vector()
    header_b64, _, _ = envelope["signature"]["jws"].partition("..")
    signing_input = envelope_signing_input(header_b64=header_b64, envelope=envelope)
    _, _, payload_b64 = signing_input.decode("ascii").partition(".")
    assert _b64url_decode(payload_b64) == canonical_binding_bytes(envelope)


def test_mutating_a_covered_section_breaks_the_committed_signature() -> None:
    """The negative side is demonstrated against a real mutation, not assumed."""
    envelope = _vector()
    header_b64, _, sig_b64 = envelope["signature"]["jws"].partition("..")
    tampered = copy.deepcopy(envelope)
    tampered["coverage"]["actions_covered"] += 1
    public_key = serialization.load_pem_public_key(_PUBKEY.read_bytes())
    assert isinstance(public_key, Ed25519PublicKey)
    with pytest.raises(InvalidSignature):
        public_key.verify(
            _b64url_decode(sig_b64),
            envelope_signing_input(header_b64=header_b64, envelope=tampered),
        )


# ---------------------------------------------------------------------------
# 9-10: the format identifiers and the generator
# ---------------------------------------------------------------------------


def test_vector_declares_the_pinned_schema_version_and_type() -> None:
    envelope = _vector()
    assert envelope["schema_version"] == EVIDENCE_ENVELOPE_SCHEMA_VERSION
    assert envelope["envelope_type"] == EVIDENCE_ENVELOPE_TYPE


def test_regenerating_the_vector_is_byte_identical_to_the_committed_file(tmp_path: Path) -> None:
    """Importing the generator builds nothing; calling it reproduces the bytes."""
    builder = _load_builder()
    builder.build(tmp_path)
    assert (tmp_path / _VECTOR.name).read_bytes() == _VECTOR.read_bytes()
    assert (tmp_path / _PUBKEY.name).read_bytes() == _PUBKEY.read_bytes()


def _b64url_decode(data: str) -> bytes:
    """Base64-url-decode, restoring padding (RFC 7515 2)."""
    import base64

    pad = -len(data) % 4
    return base64.urlsafe_b64decode(data + ("=" * pad))
