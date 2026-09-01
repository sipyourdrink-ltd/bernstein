"""CI conformance lane: trace-tests verify against fixture run.

Loads a fixture run (tests/fixtures/runs/trust-record-vector-solo.jsonl)
and verifies the emitted Trust Record against the TRACE 0.2 software-only profile.
Must import the TrustRecordEmitter from slice 1, use the same signed output
verification logic, and exit with 0 on success.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from bernstein.core.observability.trust_record import TrustRecordEmitter, verify_trust_record
from bernstein.core.replay.journal import EventJournal, JournalVerifyResult, verify_events
from bernstein.core.security.agent_card_signer import canonicalize_jcs

# The fixture is named for what it holds, not for when it was recorded: a
# wall-clock name carries a timezone offset whose colons are illegal in a
# Windows filename, and this repo runs Windows CI shards.
_FIXTURE_RUN_PATH = Path(__file__).parents[2] / "tests" / "fixtures" / "runs" / "trust-record-vector-solo.jsonl"
_SOLO_PUBKEY_PATH = (
    Path(__file__).parents[2] / "tests" / "fixtures" / "trust-record-vectors" / "trust-record-vectors-key.pem"
)

# Deterministic constants -- same as the fixture key
_FIXTURE_BUILD_DIGEST = "sha256:0f773047eab6842fc8f06605a90dec9916ac85684e0975ee6c8d11354f58dd4d"
_FIXTURE_INSTALL_REV = "fixturefixture01"
_FIXTURE_SIGN_SEED = b"k" * 32


def _fixture_private_key_pem() -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(_FIXTURE_SIGN_SEED)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _canonical_body_bytes(doc: dict) -> bytes:
    """Rebuild the exact bytes the emitter signed from a parsed record."""
    _BASE_SIGNED_FIELDS = (
        "eat_profile",
        "iat",
        "subject",
        "model",
        "runtime",
        "policy",
        "data_class",
        "tool_transcript",
        "build_provenance",
        "appraisal",
        "cnf",
    )
    body = {field: doc[field] for field in _BASE_SIGNED_FIELDS}
    if "delegation" in doc:
        body["delegation"] = doc["delegation"]
    if "references" in doc:
        body["references"] = doc["references"]
    return canonicalize_jcs(body)


def _verify_offline(doc: dict, public_key_pem: bytes) -> bool:
    """Re-verify a parsed record's bare Ed25519 signature string, offline."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    public_key = load_pem_public_key(public_key_pem)
    sig_b64 = doc["signature"]
    padded = sig_b64 + "=" * (-len(sig_b64) % 4)
    raw_sig = base64.urlsafe_b64decode(padded)
    try:
        public_key.verify(raw_sig, _canonical_body_bytes(doc))
    except InvalidSignature:
        return False
    return True


def _public_key_pem_from_cnf_jwk(doc: dict) -> bytes:
    """Recover the SPKI PEM public key from a record's cnf.jwk."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    jwk = doc["cnf"]["jwk"]
    assert jwk["kty"] == "OKP"
    assert jwk["crv"] == "Ed25519"
    x_b64 = jwk["x"]
    padded = x_b64 + "=" * (4 - len(x_b64) % 4)
    raw_public_key = base64.urlsafe_b64decode(padded)
    return Ed25519PublicKey.from_public_bytes(raw_public_key).public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def test_fixture_run_emits_valid_trust_record(tmp_path: Path) -> None:
    """Load fixture run journal and verify emitted Trust Record matches expected output."""
    # Load the fixture run journal
    journal_lines = _FIXTURE_RUN_PATH.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in journal_lines if line.strip()]

    # Verify the journal's hash chain before trusting anything it recorded
    verdict: JournalVerifyResult = verify_events(events)
    assert verdict.chain_consistent, f"journal chain broken: {verdict.errors[0] if verdict.errors else 'unknown error'}"

    # Reconstruct the journal from the fixture events so the EventJournal can
    # write the chain correctly
    sdd_dir = tmp_path / ".sdd"
    sdd_dir.mkdir()
    journal = EventJournal("trust-record-vector-solo", sdd_dir)
    for event in events:
        # Re-record each event (preserving the ts field)
        event_type = event.get("event")
        event_payload = {k: v for k, v in event.items() if k != "event"}
        # Manually set the timestamp and chain hash to match the original
        # The EventJournal.record() method handles chain hashing
        # We need to use the same _record method that builds the chain
        journal.record(event_type, **event_payload)

    # Build Trust Record from the reconstructed journal using TrustRecordEmitter
    emitter = TrustRecordEmitter(
        install_rev_getter=lambda: _FIXTURE_INSTALL_REV,
        get_private_key_pem=lambda: _fixture_private_key_pem(),
        get_installed_digest=lambda: _FIXTURE_BUILD_DIGEST,
    )

    trust_record_json = emitter.emit_trust_record(
        journal_path=journal.path,
        run_id="trust-record-vector-run",
        exec_id="trust-record-vector-solo",
    )

    # Parse the emitted Trust Record
    trust_record = json.loads(trust_record_json)

    # No golden copy of the record on disk: the signature assertion at the end
    # of this test already pins every signed field byte-for-byte through the
    # JCS canonicalisation, so a second copy could only ever drift from it.

    # Verify signature using the cnf.jwk (same approach as test_trust_record_format_vectors.py)
    public_key_pem = _public_key_pem_from_cnf_jwk(trust_record)
    assert _verify_offline(trust_record, public_key_pem) is True

    # Also verify using the repo's own verify_trust_record function
    assert verify_trust_record(trust_record, _SOLO_PUBKEY_PATH.read_bytes()) is True

    # Verify TRACE 0.2 software-only profile constraints
    assert trust_record["eat_profile"] == "tag:agentrust-io.com,2026:trace-v0.2"
    assert trust_record["runtime"]["platform"] == "software-only"
    assert (
        trust_record["runtime"]["measurement"]
        == "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    )
    assert trust_record["policy"]["enforcement_mode"] == "enforce"
    assert trust_record["appraisal"]["status"] == "none"
    assert trust_record["appraisal"]["verifier"] == "https://bernstein.run/trace/verifier"
    assert trust_record["build_provenance"]["slsa_level"] == 0
    assert (
        trust_record["build_provenance"]["provenance_uri"] == "https://github.com/sipyourdrink-ltd/bernstein/releases"
    )
    assert trust_record["cnf"]["jwk"]["kty"] == "OKP"
    assert trust_record["cnf"]["jwk"]["crv"] == "Ed25519"
    assert trust_record["subject"] == "spiffe://bernstein.run/run/trust-record-vector-run/exec/trust-record-vector-solo"
    assert trust_record["model"]["provider"] == "anthropic"
    assert trust_record["model"]["model_id"] == "claude-sonnet-5"
    assert trust_record["data_class"] == "internal"
    assert trust_record["tool_transcript"]["call_count"] == 1
    assert (
        trust_record["tool_transcript"]["hash"]
        == "sha256:17e421f33794d338b89ba18d7e6acbb6cdbd893a02e49673a7baab0ccfd63cb0"
    )
    assert len(trust_record["references"]) == 1
    assert trust_record["references"][0]["rel"] == "produced-artifact"
    assert trust_record["references"][0]["id"] == "solo-report"
    assert trust_record["references"][0]["resolver"] == "urn:bernstein:artifacts"
    assert (
        trust_record["references"][0]["digest"]
        == "sha256:2d5b21544c3ea8dd555673ba7ee243ae5fc75c8676002a3fa99f290aef1bd7ae"
    )
    # Pinning the signature keeps emission byte-reproducible: the same journal
    # must always mint the same record, which verify_trust_record above cannot
    # show. It is pinned by digest rather than by the literal because a base64
    # signature run reads as misspelled prose to the spellchecker (issue #4692),
    # and excluding a human-authored test module from that scan is the thing
    # tests/unit/test_trust_record_vectors_spellcheck_scope.py exists to prevent.
    assert hashlib.sha256(trust_record["signature"].encode()).hexdigest() == (
        "45c73c60910ed582d48984b82e242c231e76ddc5c19cba9aadd292936cc02961"
    )
