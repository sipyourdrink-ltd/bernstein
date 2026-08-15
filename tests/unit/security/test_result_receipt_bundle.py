"""Result receipt bundle: the #3870 test matrix, offline and side-effect free.

    1. round-trip create-then-verify
    2. patch tamper detected
    3. gate-log tamper detected
    4. wrong-key signature rejected
    5. serialization determinism (byte-identical re-serialize)

plus chain continuity and the field-level-error guarantee the acceptance
criteria call for.
"""

from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.security.audit_dsse import export_public_key_pem, keyid_from_public_key
from bernstein.core.security.result_receipt_bundle import (
    GENESIS_ANCHOR,
    ChainLink,
    GateResult,
    ResultBundle,
    TaskRef,
    build_result_bundle,
    parse_bundle,
    verify_result_bundle,
)


def _key(seed_byte: int = 7) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes([seed_byte]) * 32)


def _bundle(key: Ed25519PrivateKey, *, patch: str = "diff --git a/x b/x\n+hello\n") -> ResultBundle:
    pub = key.public_key()
    return ResultBundle(
        task=TaskRef(repo="sipyourdrink-ltd/bernstein", commit_sha="abc123def456", issue_number=3870),
        patch=patch,
        gates=(
            GateResult(command="pytest -q", exit_code=0, log="42 passed in 3.1s\n"),
            GateResult(command="ruff check", exit_code=0, log="All checks passed!\n"),
        ),
        manifest_sha256="0" * 64,
        adapter_id="adapter.default.v3",
        model_id="claude-x",
        sandbox_profile="restricted-net-off",
        selection_receipt="sel-receipt-9f2",
        created_at="2026-08-15T00:00:00Z",
        worker_keyid=keyid_from_public_key(pub),
        worker_public_key_pem=export_public_key_pem(pub).decode("ascii"),
        chain=ChainLink(anchor=GENESIS_ANCHOR, length=1),
    )


def test_roundtrip_create_then_verify():
    key = _key()
    env = build_result_bundle(_bundle(key), signing_key=key)
    v = verify_result_bundle(env, key.public_key())
    assert v.ok, v.errors
    assert v.bundle["task"]["issue_number"] == 3870
    assert v.digest and v.keyid == keyid_from_public_key(key.public_key())


def test_patch_tamper_detected_field_level():
    key = _key()
    env = build_result_bundle(_bundle(key), signing_key=key)
    # tamper the embedded patch inside the signed payload, then re-encode the
    # payload without re-signing -- the attacker cannot forge the signature.
    payload = json.loads(base64.b64decode(env.payload_b64))
    payload["predicate"]["bundle"]["patch"] += "\n+malicious\n"
    tampered = env.__class__(
        payload_type=env.payload_type,
        payload_b64=base64.b64encode(json.dumps(payload).encode()).decode(),
        signatures=env.signatures,
    )
    v = verify_result_bundle(tampered, key.public_key())
    assert not v.ok
    # signature breaks first (whole-payload integrity); the envelope error names it
    assert any("envelope" in e.field or "patch" in e.field for e in v.errors)


def test_patch_field_hash_mismatch_is_field_level():
    # a bundle whose patch_sha256 disagrees with its patch (no signature involved)
    # must produce a patch-field error, proving the field-level check itself works.
    key = _key()
    b = _bundle(key)
    bundle_dict = b.to_dict()
    bundle_dict["patch"] = bundle_dict["patch"] + "tampered"
    # forge a self-consistent envelope over the tampered dict but keep the OLD
    # patch_sha256, isolating the field check.
    bundle_dict["patch_sha256"] = b.patch_sha256  # stale hash
    stmt_payload = _sign_dict(key, bundle_dict)
    v = verify_result_bundle(stmt_payload, key.public_key())
    assert not v.ok
    assert any(e.field == "patch" for e in v.errors)


def _sign_dict(key, bundle_dict):
    """Build a validly-signed envelope directly over a (possibly hand-edited)
    bundle dict, so a test can exercise the field checks past the signature."""
    import base64 as b64

    from bernstein.core.security import audit_dsse as ad
    from bernstein.core.security import result_receipt_bundle as rb

    canon = rb.canonical_bytes(bundle_dict)
    subject = ad.Subject(name="t.json", digest={"sha256": rb._sha256_hex(canon)})
    statement = ad.Statement(
        subjects=[subject],
        predicate_type=rb.RESULT_RECEIPT_PREDICATE_TYPE,
        predicate={
            "schema_version": rb.BUNDLE_SCHEMA_VERSION,
            "bundle_kind": "result-receipt",
            "bundle": bundle_dict,
            "chain": bundle_dict["chain"],
        },
    )
    payload = rb.canonical_bytes(statement.to_dict())
    sig = key.sign(ad.pae(ad.DSSE_PAYLOAD_TYPE, payload))
    return ad.Envelope(
        payload_type=ad.DSSE_PAYLOAD_TYPE,
        payload_b64=b64.b64encode(payload).decode(),
        signatures=[ad.Signature(keyid=ad.keyid_from_public_key(key.public_key()), sig=b64.b64encode(sig).decode())],
    )


def test_gate_log_tamper_detected_field_level():
    key = _key()
    b = _bundle(key)
    bundle_dict = b.to_dict()
    bundle_dict["gates"][0]["log"] = "42 passed in 3.1s\n(secretly 0 passed)\n"  # stale log_sha256
    env = _sign_dict(key, bundle_dict)
    v = verify_result_bundle(env, key.public_key())
    assert not v.ok
    assert any(e.field == "gates[0].log" for e in v.errors)


def test_wrong_key_signature_rejected():
    key = _key(7)
    env = build_result_bundle(_bundle(key), signing_key=key)
    wrong = _key(9).public_key()
    v = verify_result_bundle(env, wrong)
    assert not v.ok
    assert any(e.field == "envelope" for e in v.errors)


def test_serialization_determinism():
    key = _key()
    b = _bundle(key)
    # Ed25519 is deterministic -> the whole envelope is byte-identical.
    e1 = build_result_bundle(b, signing_key=key)
    e2 = build_result_bundle(b, signing_key=key)
    assert e1.to_json() == e2.to_json()
    # and the bundle's own canonical bytes re-serialize identically
    assert b.canonical_bytes() == b.canonical_bytes()


def test_chain_continuity():
    key = _key()
    first = _bundle(key)
    env1 = build_result_bundle(first, signing_key=key)
    v1 = verify_result_bundle(env1, key.public_key())
    assert v1.ok

    # the successor links to the first bundle's digest
    pub = key.public_key()
    second = ResultBundle(
        task=first.task,
        patch="diff --git a/y b/y\n+two\n",
        gates=first.gates,
        manifest_sha256="1" * 64,
        adapter_id=first.adapter_id,
        model_id=first.model_id,
        sandbox_profile=first.sandbox_profile,
        selection_receipt="sel-2",
        created_at="2026-08-15T01:00:00Z",
        worker_keyid=first.worker_keyid,
        worker_public_key_pem=first.worker_public_key_pem,
        chain=ChainLink(anchor=first.digest, length=2),
    )
    env2 = build_result_bundle(second, signing_key=key)
    good = verify_result_bundle(env2, pub, expected_prev_digest=first.digest)
    assert good.ok, good.errors
    # a broken link is a field-level error
    bad = verify_result_bundle(env2, pub, expected_prev_digest="deadbeef")
    assert not bad.ok
    assert any(e.field == "chain.anchor" for e in bad.errors)


def test_verify_is_offline_and_pure(tmp_path):
    # verification touches no network and returns the same result twice.
    key = _key()
    env = build_result_bundle(_bundle(key), signing_key=key)
    a = verify_result_bundle(env, key.public_key())
    b = verify_result_bundle(env, key.public_key())
    assert a.ok and b.ok and a.digest == b.digest


def test_parse_roundtrip():
    key = _key()
    env = build_result_bundle(_bundle(key), signing_key=key)
    reparsed = parse_bundle(json.loads(env.to_json()))
    v = verify_result_bundle(reparsed, key.public_key())
    assert v.ok
