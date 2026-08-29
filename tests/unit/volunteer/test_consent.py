"""Tests for volunteer consent receipt: build, verify, tamper, determinism."""

from __future__ import annotations

import base64
import builtins
import hashlib
import json
import socket
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.security.audit_dsse import export_public_key_pem, keyid_from_public_key
from bernstein.core.volunteer.consent import (
    CONSENT_RECEIPT_PREDICATE_TYPE,
    CONSENT_SCHEMA_VERSION,
    GENESIS_ANCHOR,
    ChainLink,
    ConsentReceipt,
    build_consent_receipt,
    parse_consent,
    verify_consent_receipt,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _key(seed_byte: int = 7) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes([seed_byte]) * 32)


def _receipt(
    key: Ed25519PrivateKey,
    *,
    consent_text: str = "I consent to run task 42 under the project manifest and sandbox profile.",
    manifest_digest: str = "a" * 64,
    profile_digest: str = "b" * 64,
    chain_anchor: str = GENESIS_ANCHOR,
    chain_length: int = 1,
) -> ConsentReceipt:
    pub = key.public_key()
    return ConsentReceipt(
        consent_text=consent_text,
        manifest_digest=manifest_digest,
        sandbox_profile_digest=profile_digest,
        donor_keyid=keyid_from_public_key(pub),
        donor_public_key_pem=export_public_key_pem(pub).decode("ascii"),
        created_at="2026-08-21T12:00:00Z",
        chain=ChainLink(anchor=chain_anchor, length=chain_length),
        donor_signature="",  # populated at envelope build time
    )


def _sign_dict(key: Ed25519PrivateKey, receipt_dict: dict[str, Any]) -> Any:
    """Build a validly-signed envelope directly over a (possibly hand-edited)
    receipt dict, so a test can exercise the field checks past the signature."""
    from bernstein.core.security import audit_dsse as ad
    from bernstein.core.volunteer.consent import canonical_bytes as consent_canonical

    canon = consent_canonical(receipt_dict)
    subject = ad.Subject(name="consent.json", digest={"sha256": hashlib.sha256(canon).hexdigest()})
    statement = ad.Statement(
        subjects=[subject],
        predicate_type=CONSENT_RECEIPT_PREDICATE_TYPE,
        predicate={
            "schema_version": CONSENT_SCHEMA_VERSION,
            "receipt_kind": "consent",
            "receipt": receipt_dict,
            "chain": receipt_dict.get("chain"),
        },
    )
    payload = consent_canonical(statement.to_dict())
    sig = key.sign(ad.pae(ad.DSSE_PAYLOAD_TYPE, payload))
    return ad.Envelope(
        payload_type=ad.DSSE_PAYLOAD_TYPE,
        payload_b64=base64.b64encode(payload).decode(),
        signatures=[ad.Signature(keyid=ad.keyid_from_public_key(key.public_key()), sig=base64.b64encode(sig).decode())],
    )


# --------------------------------------------------------------------------- #
# Round-trip: build then verify
# --------------------------------------------------------------------------- #


def test_roundtrip_create_then_verify():
    key = _key()
    env = build_consent_receipt(_receipt(key), signing_key=key)
    v = verify_consent_receipt(env, key.public_key())
    assert v.ok, v.errors
    assert v.receipt["consent_text"] == _receipt(key).consent_text
    assert v.digest and v.keyid == keyid_from_public_key(key.public_key())


def test_serialization_determinism():
    key = _key()
    r = _receipt(key)
    # Ed25519 is deterministic -> the whole envelope is byte-identical.
    e1 = build_consent_receipt(r, signing_key=key)
    e2 = build_consent_receipt(r, signing_key=key)
    assert e1.to_json() == e2.to_json()
    # and the receipt's own canonical bytes re-serialize identically
    assert r.canonical_bytes() == r.canonical_bytes()


def test_parse_roundtrip():
    key = _key()
    env = build_consent_receipt(_receipt(key), signing_key=key)
    reparsed = parse_consent(json.loads(env.to_json()))
    v = verify_consent_receipt(reparsed, key.public_key())
    assert v.ok


def test_verify_is_offline_and_pure(tmp_path):
    key = _key()
    env = build_consent_receipt(_receipt(key), signing_key=key)
    a = verify_consent_receipt(env, key.public_key())
    b = verify_consent_receipt(env, key.public_key())
    assert a.ok and b.ok and a.digest == b.digest
    # tmp_path exists but was never touched by verify
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- #
# Field-level checks: tamper, wrong key, wrong digest
# --------------------------------------------------------------------------- #


def test_consent_text_tamper_detected():
    key = _key()
    r = _receipt(key)
    receipt_dict = r.to_dict()
    receipt_dict["consent_text"] += "\n(secretly longer)"
    env = _sign_dict(key, receipt_dict)
    v = verify_consent_receipt(env, key.public_key())
    assert not v.ok
    assert any("consent text" in e for e in v.errors)


def test_consent_text_hash_mismatch():
    """A receipt whose consent_text_sha256 disagrees with its consent_text."""
    key = _key()
    r = _receipt(key)
    receipt_dict = r.to_dict()
    receipt_dict["consent_text"] = receipt_dict["consent_text"] + " tampered"
    receipt_dict["consent_text_sha256"] = r.consent_text_sha256  # stale hash
    env = _sign_dict(key, receipt_dict)
    v = verify_consent_receipt(env, key.public_key())
    assert not v.ok
    assert any("consent text" in e for e in v.errors)


def test_wrong_key_signature_rejected():
    key = _key(7)
    env = build_consent_receipt(_receipt(key), signing_key=key)
    wrong = _key(9).public_key()
    v = verify_consent_receipt(env, wrong)
    assert not v.ok
    assert v.errors  # signature failed


def test_donor_keyid_mismatch():
    """The signer's keyid must match the receipt's donor.keyid."""
    key = _key(7)
    r = _receipt(key)
    receipt_dict = r.to_dict()
    receipt_dict["donor"]["keyid"] = "deadbeef" * 4
    env = _sign_dict(key, receipt_dict)
    v = verify_consent_receipt(env, key.public_key())
    assert not v.ok
    assert any("signature is by" in e for e in v.errors)


# --------------------------------------------------------------------------- #
# Manifest / profile digest checks
# --------------------------------------------------------------------------- #


def test_manifest_digest_check_visible_in_verdict():
    """An unchecked field reported as verified is the defect #3911's analogue."""
    key = _key()
    r = _receipt(key)
    env = build_consent_receipt(r, signing_key=key)

    unchecked = verify_consent_receipt(env, key.public_key())
    checked = verify_consent_receipt(env, key.public_key(), expected_manifest_digest=r.manifest_digest)

    assert unchecked.ok and checked.ok
    assert unchecked.errors == () and checked.errors == ()
    assert unchecked.manifest_digest_checked is False
    assert checked.manifest_digest_checked is True
    assert unchecked != checked


def test_manifest_digest_mismatch_refused():
    key = _key()
    r = _receipt(key, manifest_digest="real" + "0" * 60)
    env = build_consent_receipt(r, signing_key=key)
    v = verify_consent_receipt(env, key.public_key(), expected_manifest_digest="wrong" + "0" * 60)
    assert not v.ok
    assert any("manifest_digest" in e for e in v.errors)


def test_profile_digest_check_visible_in_verdict():
    key = _key()
    r = _receipt(key)
    env = build_consent_receipt(r, signing_key=key)

    unchecked = verify_consent_receipt(env, key.public_key())
    checked = verify_consent_receipt(env, key.public_key(), expected_profile_digest=r.sandbox_profile_digest)

    assert unchecked.ok and checked.ok
    assert unchecked.errors == () and checked.errors == ()
    assert unchecked.profile_digest_checked is False
    assert checked.profile_digest_checked is True
    assert unchecked != checked


def test_profile_digest_mismatch_refused():
    key = _key()
    r = _receipt(key, profile_digest="real" + "0" * 60)
    env = build_consent_receipt(r, signing_key=key)
    v = verify_consent_receipt(env, key.public_key(), expected_profile_digest="wrong" + "0" * 60)
    assert not v.ok
    assert any("sandbox_profile_digest" in e for e in v.errors)


def test_failed_signature_does_not_report_digests_as_checked():
    key = _key()
    r = _receipt(key)
    env = build_consent_receipt(r, signing_key=key)
    v = verify_consent_receipt(
        env,
        _key(9).public_key(),
        expected_manifest_digest=r.manifest_digest,
        expected_profile_digest=r.sandbox_profile_digest,
    )
    assert not v.ok
    assert v.errors  # signature failed
    assert v.manifest_digest_checked is False
    assert v.profile_digest_checked is False


def test_chain_anchor_check_visible_in_verdict():
    key = _key()
    first = _receipt(key)
    second = _receipt(key, chain_anchor=first.digest, chain_length=2)
    env = build_consent_receipt(second, signing_key=key)

    unchecked = verify_consent_receipt(env, key.public_key())
    checked = verify_consent_receipt(env, key.public_key(), expected_prev_digest=first.digest)

    assert unchecked.ok and checked.ok
    assert unchecked.prev_digest_checked is False
    assert checked.prev_digest_checked is True
    assert unchecked != checked


def test_chain_anchor_mismatch_refused():
    key = _key()
    first = _receipt(key)
    second = _receipt(key, chain_anchor=first.digest, chain_length=2)
    env = build_consent_receipt(second, signing_key=key)
    v = verify_consent_receipt(env, key.public_key(), expected_prev_digest="deadbeef" * 10)
    assert not v.ok
    assert any("chain.anchor" in e for e in v.errors)


def test_failed_signature_does_not_report_chain_as_checked():
    key = _key()
    first = _receipt(key)
    second = _receipt(key, chain_anchor=first.digest, chain_length=2)
    env = build_consent_receipt(second, signing_key=key)
    v = verify_consent_receipt(env, _key(9).public_key(), expected_prev_digest=first.digest)
    assert not v.ok
    assert v.errors  # signature failed
    assert v.prev_digest_checked is False


def test_malformed_chain_link_does_not_report_continuity_as_checked():
    key = _key()
    first = _receipt(key)
    for label, mutate in (
        ("anchor missing", lambda d: d["chain"].pop("anchor")),
        ("length missing", lambda d: d["chain"].pop("length")),
        ("chain absent", lambda d: d.pop("chain")),
        ("length not an int", lambda d: d["chain"].__setitem__("length", "2")),
        ("length below one", lambda d: d["chain"].__setitem__("length", 0)),
    ):
        receipt_dict = _receipt(key, chain_anchor=first.digest, chain_length=2).to_dict()
        mutate(receipt_dict)
        v = verify_consent_receipt(_sign_dict(key, receipt_dict), key.public_key(), expected_prev_digest=first.digest)
        assert not v.ok, label
        assert v.prev_digest_checked is False, label
        assert any("chain" in e for e in v.errors), label


@pytest.mark.parametrize(
    ("chain", "type_name"),
    [
        ("anchor and length", "str"),
        (["anchor", "length"], "list"),
        (None, "NoneType"),
        (42, "int"),
        ("", "str"),
        ([], "list"),
    ],
    ids=["str", "list", "null", "int", "str-without-words", "empty-list"],
)
def test_a_non_mapping_chain_is_refused_rather_than_raised(chain: Any, type_name: str):
    key = _key()
    receipt_dict = _receipt(key).to_dict()
    receipt_dict["chain"] = chain
    v = verify_consent_receipt(_sign_dict(key, receipt_dict), key.public_key())
    assert v.ok is False
    assert any("chain" in e for e in v.errors)


# --------------------------------------------------------------------------- #
# Open-no-files-and-no-sockets: verify is pure
# --------------------------------------------------------------------------- #


def test_verification_opens_no_files_and_no_sockets(monkeypatch: pytest.MonkeyPatch):
    key = _key()
    r = _receipt(key)
    env = build_consent_receipt(r, signing_key=key)

    def _no_open(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"verification opened a file: {args!r}")

    def _no_connect(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"verification opened a socket: {args!r}")

    monkeypatch.setattr(builtins, "open", _no_open)
    monkeypatch.setattr(socket.socket, "connect", _no_connect)

    v = verify_consent_receipt(
        env,
        key.public_key(),
        expected_manifest_digest=r.manifest_digest,
        expected_profile_digest=r.sandbox_profile_digest,
        expected_prev_digest=None,
    )
    assert v.ok and v.manifest_digest_checked and v.profile_digest_checked


# --------------------------------------------------------------------------- #
# Persistence: write / load round-trip
# --------------------------------------------------------------------------- #


def test_write_load_roundtrip(tmp_path: pytest.TempPathFixture):
    key = _key()
    env = build_consent_receipt(_receipt(key), signing_key=key)
    path = tmp_path / ".sdd" / "runtime" / "volunteer" / "consent.json"
    written = build_consent_receipt(_receipt(key), signing_key=key)  # noqa: F841
    # write_consent delegates to audit_dsse.write_envelope, which is covered
    # by test_result_receipt_bundle's parse_roundtrip; exercise the public
    # API shape here without retesting audit_dsse.
    from bernstein.core.volunteer.consent import load_consent, write_consent

    write_consent(env, path)
    reloaded = load_consent(path)
    assert reloaded.to_json() == env.to_json()
