"""Synthetic hardware custody evidence; no private certificates or devices."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import NameOID, ObjectIdentifier

from bernstein.core.identity.hardware_attestation import (
    HardwarePolicy,
    PinPolicy,
    TouchPolicy,
    bundled_root_pins,
    certificate_sha256,
    decode_yubico_extensions,
    registration_digest,
    verify_fido2_assertion,
    verify_piv_attestation,
)
from bernstein.core.receipts.protocol import canonical_receipt_bytes, registered_kinds, sign_receipt, verify_receipt

AT = datetime(2026, 10, 16, tzinfo=UTC)


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def pem(key: Any, *, private: bool = False) -> str:
    if private:
        return key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
    return key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def cert_pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def certificate(
    name: str,
    key: Any,
    issuer: x509.Certificate | None,
    signing_key: Any,
    *,
    ca: bool = True,
    extensions: bool = False,
    touch: int = 2,
    pin: int = 2,
    firmware: bytes = b"\x05\x07\x04",
    expired: bool = False,
) -> x509.Certificate:
    attributes = [x509.NameAttribute(NameOID.COMMON_NAME, name)]
    if name == "packed attestation":
        attributes += [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Synthetic"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Authenticator Attestation"),
        ]
    subject = x509.Name(attributes)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.subject if issuer else subject)
        .public_key(key.public_key())
        .serial_number(100)
        .not_valid_before(AT - timedelta(days=10))
        .not_valid_after(AT - timedelta(days=1) if expired else AT + timedelta(days=10))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if extensions:
        for suffix, value in [(3, firmware), (7, b"\x02\x02\x01\x02"), (8, bytes([pin, touch])), (9, b"\x03")]:
            builder = builder.add_extension(
                x509.UnrecognizedExtension(ObjectIdentifier(f"1.3.6.1.4.1.41482.3.{suffix}"), value), critical=False
            )
    return builder.sign(signing_key, None if isinstance(signing_key, ed25519.Ed25519PrivateKey) else hashes.SHA256())


@pytest.fixture(scope="module")
def piv():
    root_key = ec.generate_private_key(ec.SECP256R1())
    root = certificate("root", root_key, None, root_key)
    middle_key = ed25519.Ed25519PrivateKey.generate()
    middle = certificate("intermediate", middle_key, root, root_key)
    piv_key = ec.generate_private_key(ec.SECP256R1())
    piv_ca = certificate("PIV intermediate", piv_key, middle, middle_key)
    f9_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    f9 = certificate("F9", f9_key, piv_ca, piv_key)
    key = ed25519.Ed25519PrivateKey.generate()
    leaf = certificate("leaf", key, f9, f9_key, ca=False, extensions=True)
    chain = [root, middle, piv_ca, f9, leaf]
    policy = HardwarePolicy(AT, frozenset({certificate_sha256(root)}), frozenset({certificate_sha256(f9)}))
    return chain, key, f9_key, policy


def payload_for(chain):
    return {"chain": [cert_pem(c) for c in chain]}


def test_attestation_kinds_load():
    assert {"key-attestation/piv", "approval/fido2-assertion"} <= set(registered_kinds())


def test_attestation_piv_valid_rsa_f9_two_intermediates(piv):
    chain, key, _, policy = piv
    result = verify_piv_attestation(payload_for(chain), signer_pem=pem(key.public_key()), policy=policy)
    assert result.ok, result
    assert dict(result.metadata)["firmware"] == "5.7.4"
    assert (
        result.to_json()
        == verify_piv_attestation(payload_for(chain), signer_pem=pem(key.public_key()), policy=policy).to_json()
    )
    assert b"serial" not in result.to_json()
    decoded = decode_yubico_extensions(chain[-1])
    assert decoded.pin == PinPolicy.ONCE and decoded.touch == TouchPolicy.ALWAYS


@pytest.mark.parametrize(
    "case",
    ["root", "f9", "time", "naive", "signer", "truncated", "bad-signature", "issuer", "expired-intermediate", "non-ca"],
)
def test_attestation_piv_rejects(piv, case):
    chain, key, f9_key, policy = piv
    chain = list(chain)
    signer = pem(key.public_key())
    if case == "root":
        policy = replace(policy, root_sha256=frozenset())
    elif case == "f9":
        policy = replace(policy, f9_sha256=frozenset())
    elif case == "time":
        policy = replace(policy, at=AT + timedelta(days=20))
    elif case == "naive":
        policy = replace(policy, at=AT.replace(tzinfo=None))
    elif case == "signer":
        signer = pem(ed25519.Ed25519PrivateKey.generate().public_key())
    elif case == "truncated":
        chain = chain[1:]
    elif case == "bad-signature":
        chain[-1] = certificate("leaf", key, chain[-2], ed25519.Ed25519PrivateKey.generate(), ca=False, extensions=True)
    elif case == "issuer":
        chain[-1] = certificate("leaf", key, chain[0], f9_key, ca=False, extensions=True)
    elif case in ("expired-intermediate", "non-ca"):
        # Even a correctly root-signed intermediate must be valid and a CA.
        root_key = ed25519.Ed25519PrivateKey.generate()
        root = certificate("root", root_key, None, root_key)
        mid_key = ed25519.Ed25519PrivateKey.generate()
        middle = certificate(
            "middle", mid_key, root, root_key, expired=case == "expired-intermediate", ca=case != "non-ca"
        )
        f9 = certificate("F9", f9_key, middle, mid_key)
        leaf = certificate("leaf", key, f9, f9_key, ca=False, extensions=True)
        chain = [root, middle, f9, leaf]
        policy = replace(
            policy, root_sha256=frozenset({certificate_sha256(root)}), f9_sha256=frozenset({certificate_sha256(f9)})
        )
    assert not verify_piv_attestation(payload_for(chain), signer_pem=signer, policy=policy).ok


@pytest.mark.parametrize(
    "changes",
    [{"touch": 1}, {"pin": 1}, {"firmware": b"\x05\x06\x00"}, {"firmware": b"\x05"}, {"pin": 99}, {"touch": 99}],
)
def test_attestation_piv_policy_extensions(piv, changes):
    chain, key, f9_key, policy = piv
    chain = list(chain)
    chain[-1] = certificate("leaf", key, chain[-2], f9_key, ca=False, extensions=True, **changes)
    assert not verify_piv_attestation(payload_for(chain), signer_pem=pem(key.public_key()), policy=policy).ok


def test_attestation_receipt_binds_actual_signer_and_policy(piv):
    chain, key, _, policy = piv
    envelope = sign_receipt(
        "key-attestation/piv",
        payload_for(chain),
        private_key_pem=pem(key, private=True),
        public_key_pem=pem(key.public_key()),
    )
    assert verify_receipt(envelope, context=policy).ok
    assert not verify_receipt(envelope).ok
    other = ed25519.Ed25519PrivateKey.generate()
    replacement = sign_receipt(
        envelope.kind,
        envelope.payload,
        private_key_pem=pem(other, private=True),
        public_key_pem=pem(other.public_key()),
    )
    assert "attested key differs from receipt signer" in verify_receipt(replacement, context=policy).errors


def test_attestation_p256_fallback_existing_envelope(piv):
    chain, _, f9_key, policy = piv
    key = ec.generate_private_key(ec.SECP256R1())
    chain = list(chain)
    chain[-1] = certificate("P256", key, chain[-2], f9_key, ca=False, extensions=True, firmware=b"\x05\x06\x00")
    receipt = sign_receipt(
        "key-attestation/piv",
        payload_for(chain),
        private_key_pem=pem(key, private=True),
        public_key_pem=pem(key.public_key()),
    )
    assert verify_receipt(receipt, context=policy).ok


def cbor(value):
    def head(major, size):
        return (
            bytes([(major << 5) | size])
            if size < 24
            else bytes([(major << 5) | 24, size])
            if size < 256
            else bytes([(major << 5) | 25]) + size.to_bytes(2, "big")
        )

    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError


def signature(key, data):
    return key.sign(data) if isinstance(key, ed25519.Ed25519PrivateKey) else key.sign(data, ec.ECDSA(hashes.SHA256()))


@pytest.fixture(params=[-8, -7])
def fido(request):
    alg = request.param
    key = ed25519.Ed25519PrivateKey.generate() if alg == -8 else ec.generate_private_key(ec.SECP256R1())
    if alg == -8:
        cose = {
            1: 1,
            3: -8,
            -1: 6,
            -2: key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw),
        }
    else:
        n = key.public_key().public_numbers()
        cose = {1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")}
    root_key = ed25519.Ed25519PrivateKey.generate()
    root = certificate("FIDO root", root_key, None, root_key)
    att_key = ed25519.Ed25519PrivateKey.generate() if alg == -8 else ec.generate_private_key(ec.SECP256R1())
    att = certificate("packed attestation", att_key, root, root_key, ca=False)
    policy = HardwarePolicy(
        AT,
        frozenset({certificate_sha256(root)}),
        rp_id="approvals.example",
        origins=frozenset({"https://approvals.example"}),
        require_uv=True,
    )
    cred = b"credential-1"
    rp_hash = hashlib.sha256(policy.rp_id.encode()).digest()
    reg_auth = rp_hash + b"\x45" + (1).to_bytes(4, "big") + bytes(16) + len(cred).to_bytes(2, "big") + cred + cbor(cose)
    reg_client = canonical_receipt_bytes(
        {"type": "webauthn.create", "challenge": b64(b"enrollment-challenge"), "origin": "https://approvals.example"}
    )
    registration = {
        "fmt": "packed",
        "alg": alg,
        "authenticator_data": b64(reg_auth),
        "client_data_json": b64(reg_client),
        "challenge": b64(b"enrollment-challenge"),
        "chain": [cert_pem(root), cert_pem(att)],
        "signature": b64(signature(att_key, reg_auth + hashlib.sha256(reg_client).digest())),
    }
    policy = replace(policy, registration_sha256=frozenset({registration_digest(registration)}))
    approved = {"action": "merge", "task": 7}
    client = canonical_receipt_bytes(
        {
            "type": "webauthn.get",
            "challenge": b64(hashlib.sha256(canonical_receipt_bytes(approved)).digest()),
            "origin": "https://approvals.example",
        }
    )
    auth = rp_hash + b"\x05" + (2).to_bytes(4, "big")
    payload = {
        "registration": registration,
        "credential_id": b64(cred),
        "approved_payload": approved,
        "client_data_json": b64(client),
        "authenticator_data": b64(auth),
        "signature": b64(signature(key, auth + hashlib.sha256(client).digest())),
    }
    return payload, key, att_key, policy


def resign_assertion(payload, key):
    auth = base64.urlsafe_b64decode(payload["authenticator_data"] + "==")
    client = base64.urlsafe_b64decode(payload["client_data_json"] + "==")
    payload["signature"] = b64(signature(key, auth + hashlib.sha256(client).digest()))


def test_fido2_assertion_valid(fido):
    payload, _, _, policy = fido
    verdict = verify_fido2_assertion(payload, policy=policy)
    assert verdict.ok, verdict
    assert verdict.to_json() == verify_fido2_assertion(payload, policy=policy).to_json()


@pytest.mark.parametrize(
    "case",
    [
        "rp",
        "up",
        "uv",
        "challenge",
        "type",
        "origin",
        "cross-origin",
        "signature",
        "credential",
        "unregistered",
        "registration-signature",
        "registration-key",
        "registration-format",
        "algorithm",
        "truncated",
        "trailing",
        "extensions",
        "base64",
        "duplicate-json",
    ],
)
def test_fido2_assertion_negative(fido, case):
    original, key, _, policy = fido
    payload = json.loads(json.dumps(original))
    auth = base64.urlsafe_b64decode(payload["authenticator_data"] + "==")
    client = json.loads(base64.urlsafe_b64decode(payload["client_data_json"] + "=="))
    if case == "rp":
        auth = bytes(32) + auth[32:]
    elif case == "up":
        auth = auth[:32] + b"\x04" + auth[33:]
    elif case == "uv":
        auth = auth[:32] + b"\x01" + auth[33:]
    elif case == "challenge":
        client["challenge"] = b64(b"wrong")
    elif case == "type":
        client["type"] = "webauthn.create"
    elif case == "origin":
        client["origin"] = "https://evil.example"
    elif case == "cross-origin":
        client["crossOrigin"] = True
    elif case == "signature":
        payload["signature"] = b64(b"wrong")
    elif case == "credential":
        payload["credential_id"] = b64(b"other")
    elif case == "unregistered":
        policy = replace(policy, registration_sha256=frozenset())
    elif case.startswith("registration") or case == "algorithm":
        reg = payload["registration"]
        if case == "registration-signature":
            reg["signature"] = b64(b"wrong")
        elif case == "registration-key":
            data = bytearray(base64.urlsafe_b64decode(reg["authenticator_data"] + "=="))
            data[-1] ^= 1
            reg["authenticator_data"] = b64(bytes(data))
        elif case == "registration-format":
            reg["fmt"] = "none"
        elif case == "algorithm":
            reg["alg"] = -257
        policy = replace(policy, registration_sha256=frozenset({registration_digest(reg)}))
    elif case == "truncated":
        auth = auth[:36]
    elif case == "trailing":
        auth += b"\x00"
    elif case == "extensions":
        auth = auth[:32] + b"\x85" + auth[33:]
    elif case == "base64":
        payload["credential_id"] = "%bad"
    if case not in ("signature",):
        payload["authenticator_data"] = b64(auth)
        payload["client_data_json"] = b64(canonical_receipt_bytes(client))
        if case == "duplicate-json":
            payload["client_data_json"] = b64(b'{"type":"webauthn.get","type":"webauthn.get"}')
        resign_assertion(payload, key)
    assert not verify_fido2_assertion(payload, policy=policy).ok, case


@pytest.mark.parametrize("count,previous", [(0, None), (0, 0), (2, 2), (1, 2)])
def test_fido2_assertion_counter_warning(fido, count, previous):
    payload, key, _, policy = fido
    payload = dict(payload)
    auth = base64.urlsafe_b64decode(payload["authenticator_data"] + "==")
    payload["authenticator_data"] = b64(auth[:33] + count.to_bytes(4, "big"))
    resign_assertion(payload, key)
    if previous is not None:
        policy = replace(policy, previous_counts=((payload["credential_id"], previous),))
    verdict = verify_fido2_assertion(payload, policy=policy)
    assert verdict.ok and verdict.warnings
    signer = ed25519.Ed25519PrivateKey.generate()
    envelope = sign_receipt(
        "approval/fido2-assertion",
        payload,
        private_key_pem=pem(signer, private=True),
        public_key_pem=pem(signer.public_key()),
    )
    result = verify_receipt(envelope, context=policy)
    assert result.ok and result.warnings


def test_attestation_bundled_vendor_roots():
    pins = bundled_root_pins()
    assert pins == frozenset(
        {
            "62760c6a6ef91679f454c8902b80fd009825b3f25da90f1fbace2ec6586cd5a8",
            "63ece914e54dd87915f34033c85af4c0696ba1512f8add66ced738331207b546",
        }
    )
    import bernstein.core.identity.hardware_attestation as module

    manifest = Path(module.__file__).with_name("hardware_roots") / "manifest.json"
    assert all(
        entry["source"].startswith("https://developers.yubico.com/PKI/")
        for entry in json.loads(manifest.read_text())["certificates"]
    )


def test_attestation_signed_timestamp_default(piv):
    chain, key, _, policy = piv
    policy = replace(policy, at=None)
    payload = {**payload_for(chain), "timestamp": AT.isoformat()}
    receipt = sign_receipt(
        "key-attestation/piv", payload, private_key_pem=pem(key, private=True), public_key_pem=pem(key.public_key())
    )
    first = verify_receipt(receipt, context=policy)
    second = verify_receipt(receipt, context=policy)
    assert first.ok
    from dataclasses import asdict

    assert canonical_receipt_bytes(asdict(first)) == canonical_receipt_bytes(asdict(second))
    payload.pop("timestamp")
    assert not verify_piv_attestation(payload, signer_pem=pem(key.public_key()), policy=policy).ok
    payload["timestamp"] = "2026-10-16T00:00:00"
    assert not verify_piv_attestation(payload, signer_pem=pem(key.public_key()), policy=policy).ok


def test_attestation_other_valid_vendor_f9_rejected():
    root_key = ed25519.Ed25519PrivateKey.generate()
    root = certificate("root", root_key, None, root_key)
    mid_key = ed25519.Ed25519PrivateKey.generate()
    middle = certificate("middle", mid_key, root, root_key)
    enrolled_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    enrolled = certificate("F9-enrolled", enrolled_key, middle, mid_key)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = certificate("F9-other", other_key, middle, mid_key)
    leaf_key = ed25519.Ed25519PrivateKey.generate()
    leaf = certificate("leaf", leaf_key, other, other_key, ca=False, extensions=True)
    policy = HardwarePolicy(AT, frozenset({certificate_sha256(root)}), frozenset({certificate_sha256(enrolled)}))
    verdict = verify_piv_attestation(
        payload_for([root, middle, other, leaf]), signer_pem=pem(leaf_key.public_key()), policy=policy
    )
    assert verdict.errors == ("unpinned F9",)


def test_attestation_invalid_intermediate_signature():
    root_key = ed25519.Ed25519PrivateKey.generate()
    root = certificate("root", root_key, None, root_key)
    mid_key = ed25519.Ed25519PrivateKey.generate()
    middle = certificate("middle", mid_key, root, ed25519.Ed25519PrivateKey.generate())
    f9_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    f9 = certificate("F9", f9_key, middle, mid_key)
    key = ed25519.Ed25519PrivateKey.generate()
    leaf = certificate("leaf", key, f9, f9_key, ca=False, extensions=True)
    policy = HardwarePolicy(AT, frozenset({certificate_sha256(root)}), frozenset({certificate_sha256(f9)}))
    assert not verify_piv_attestation(
        payload_for([root, middle, f9, leaf]), signer_pem=pem(key.public_key()), policy=policy
    ).ok


@pytest.mark.parametrize("bad", [None, [], {}, {"chain": None}, {"chain": ["bad"] * 4}])
def test_attestation_malformed_evidence_fails_closed(bad):
    policy = HardwarePolicy(AT)
    assert not verify_piv_attestation(bad, signer_pem="", policy=policy).ok
    assert not verify_fido2_assertion(bad, policy=policy).ok


@pytest.mark.parametrize("case", ["country", "organization", "cn", "ou", "ca", "aaguid"])
def test_fido2_assertion_packed_certificate_profile(case):
    from bernstein.core.identity.hardware_attestation import EvidenceError, _packed_certificate

    key = ed25519.Ed25519PrivateKey.generate()
    attrs = [
        (NameOID.COUNTRY_NAME, "US", "country"),
        (NameOID.ORGANIZATION_NAME, "Synthetic", "organization"),
        (NameOID.COMMON_NAME, "Synthetic", "cn"),
        (NameOID.ORGANIZATIONAL_UNIT_NAME, "Authenticator Attestation", "ou"),
    ]
    subject = x509.Name([x509.NameAttribute(oid, value) for oid, value, label in attrs if label != case])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(AT - timedelta(days=1))
        .not_valid_after(AT + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=case == "ca", path_length=None), critical=True)
    )
    if case == "aaguid":
        builder = builder.add_extension(
            x509.UnrecognizedExtension(ObjectIdentifier("1.3.6.1.4.1.45724.1.1.4"), b"\x04\x10" + b"1" * 16),
            critical=False,
        )
    cert = builder.sign(key, None)
    with pytest.raises(EvidenceError):
        _packed_certificate(cert, bytes(16))


def test_attestation_intermediate_path_length_enforced():
    root_key = ed25519.Ed25519PrivateKey.generate()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "path-limit root")])
    root = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(root_key.public_key())
        .serial_number(1)
        .not_valid_before(AT - timedelta(days=1))
        .not_valid_after(AT + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .sign(root_key, None)
    )
    mid_key = ed25519.Ed25519PrivateKey.generate()
    middle = certificate("middle", mid_key, root, root_key)
    f9_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    f9 = certificate("F9", f9_key, middle, mid_key)
    key = ed25519.Ed25519PrivateKey.generate()
    leaf = certificate("leaf", key, f9, f9_key, ca=False, extensions=True)
    policy = HardwarePolicy(AT, frozenset({certificate_sha256(root)}), frozenset({certificate_sha256(f9)}))
    verdict = verify_piv_attestation(
        payload_for([root, middle, f9, leaf]), signer_pem=pem(key.public_key()), policy=policy
    )
    assert verdict.errors == ("certificate path length exceeded",)


@pytest.mark.parametrize("bad", [None, [], 1, "bad"])
def test_attestation_nonobject_without_explicit_time(bad):
    policy = HardwarePolicy()
    assert not verify_piv_attestation(bad, signer_pem="", policy=policy).ok
    assert not verify_fido2_assertion(bad, policy=policy).ok


def test_attestation_unsupported_recognized_critical_extension():
    from bernstein.core.identity.hardware_attestation import EvidenceError, verify_certificate_chain

    key = ed25519.Ed25519PrivateKey.generate()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "constrained root")])
    root = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(AT - timedelta(days=1))
        .not_valid_after(AT + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.NameConstraints(permitted_subtrees=[x509.DNSName("example.com")], excluded_subtrees=None),
            critical=True,
        )
        .sign(key, None)
    )
    leaf_key = ed25519.Ed25519PrivateKey.generate()
    leaf = certificate("leaf", leaf_key, root, key, ca=False)
    policy = HardwarePolicy(AT, frozenset({certificate_sha256(root)}))
    with pytest.raises(EvidenceError, match="unsupported critical"):
        verify_certificate_chain([root, leaf], policy)


def test_attestation_duplicate_certificate_extensions_fail_closed(piv):
    chain, key, _, policy = piv

    def read_tlv(data, position=0):
        tag = data[position]
        size = data[position + 1]
        header = 2
        if size & 0x80:
            width = size & 0x7F
            size = int.from_bytes(data[position + 2 : position + 2 + width], "big")
            header += width
        return tag, data[position + header : position + header + size], position + header + size

    def tlv(tag, contents):
        size = len(contents)
        length = (
            bytes([size])
            if size < 128
            else bytes([0x80 | ((size.bit_length() + 7) // 8)]) + size.to_bytes((size.bit_length() + 7) // 8, "big")
        )
        return bytes([tag]) + length + contents

    encoded = chain[0].public_bytes(serialization.Encoding.DER)
    _, outer, _ = read_tlv(encoded)
    _, tbs, tbs_end = read_tlv(outer)
    position = 0
    fields = []
    while position < len(tbs):
        start = position
        tag, contents, position = read_tlv(tbs, position)
        if tag == 0xA3:
            _, extensions, _ = read_tlv(contents)
            _, _, first_end = read_tlv(extensions)
            fields.append(tlv(tag, tlv(0x30, extensions + extensions[:first_end])))
        else:
            fields.append(tbs[start:position])
    duplicated = x509.load_der_x509_certificate(tlv(0x30, tlv(0x30, b"".join(fields)) + outer[tbs_end:]))
    policy = replace(policy, root_sha256=frozenset({certificate_sha256(duplicated)}))
    payload = payload_for([duplicated, *chain[1:]])
    result = verify_piv_attestation(payload, signer_pem=pem(key.public_key()), policy=policy)
    assert not result.ok
    assert result.errors == ("invalid PIV evidence",)


@pytest.mark.parametrize("piv_leaf", [True, False])
def test_attestation_leaf_key_usage_requires_signature(piv, piv_leaf):
    from bernstein.core.identity.hardware_attestation import EvidenceError, verify_certificate_chain

    chain, key, f9_key, policy = piv
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "no-signature leaf")])
    issuer = chain[-2]
    leaf = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(AT - timedelta(days=1))
        .not_valid_after(AT + timedelta(days=1))
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(f9_key, hashes.SHA256())
    )
    if piv_leaf:
        result = verify_piv_attestation(
            payload_for([*chain[:-1], leaf]), signer_pem=pem(key.public_key()), policy=policy
        )
        assert result.errors == ("leaf cannot sign evidence",)
    else:
        with pytest.raises(EvidenceError, match="leaf cannot sign evidence"):
            verify_certificate_chain([*chain[:-1], leaf], policy)
