"""Offline hardware custody evidence. Trust and application policy are external.

No device access, network, clock, key generation or TRACE appraisal occurs here.
All returned errors are fixed strings: certificate subjects/serials never leak.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, cast

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.x509.oid import ExtensionOID, NameOID, ObjectIdentifier

from bernstein.core.receipts.protocol import ReceiptEnvelope, canonical_receipt_bytes, register_receipt_kind


class EvidenceError(ValueError):
    """Untrusted evidence failed a structural or cryptographic check."""


class PinPolicy(IntEnum):
    NEVER = 1
    ONCE = 2
    ALWAYS = 3


class TouchPolicy(IntEnum):
    NEVER = 1
    ALWAYS = 2
    CACHED = 3


class FormFactor(IntEnum):
    USB_A_KEYCHAIN = 1
    USB_A_NANO = 2
    USB_C_KEYCHAIN = 3
    USB_C_NANO = 4
    USB_C_LIGHTNING = 5
    USB_A_BIO = 6
    USB_C_BIO = 7


@dataclass(frozen=True)
class YubicoExtensions:
    firmware: tuple[int, int, int]
    serial: int
    pin: PinPolicy
    touch: TouchPolicy
    form_factor: FormFactor
    fips: bool = False
    cspn: bool = False

    def public_metadata(self) -> dict[str, Any]:
        """Return only non-identifying device policy metadata."""
        return {
            "firmware": ".".join(map(str, self.firmware)),
            "pin_policy": self.pin.name,
            "touch_policy": self.touch.name,
            "form_factor": self.form_factor.name,
        }


@dataclass(frozen=True)
class EvidenceVerdict:
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: tuple[tuple[str, Any], ...] = ()

    def to_json(self) -> bytes:
        """Stable public verdict bytes, independent of local time and state."""
        return canonical_receipt_bytes(
            {
                "ok": self.ok,
                "errors": list(self.errors),
                "warnings": list(self.warnings),
                "metadata": dict(self.metadata),
            }
        )


@dataclass(frozen=True)
class HardwarePolicy:
    """Operator-owned trust inputs; never construct these from receipt claims.

    ``at`` is timezone-aware; when omitted, a signed payload ``timestamp``
    supplies the reproducible appraisal instant. This is historical validity,
    not proof that the token or certificate remains valid today. F9 pins identify the enrolled
    device attestation certificate, preventing substituted manufacturer keys.
    Registration pins bind subsequent assertions to enrolled packed evidence.
    """

    at: datetime | None = None
    root_sha256: frozenset[str] = field(default_factory=lambda: bundled_root_pins())
    f9_sha256: frozenset[str] = frozenset()
    rp_id: str = ""
    origins: frozenset[str] = frozenset()
    require_uv: bool = False
    approval: bool = True
    registration_sha256: frozenset[str] = frozenset()
    previous_counts: tuple[tuple[str, int], ...] = ()


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise EvidenceError(reason)


def _unb64(value: Any) -> bytes:
    _require(isinstance(value, str), "invalid base64url")
    _require(len(value) <= 1_048_576, "oversize evidence")
    try:
        result = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise EvidenceError("invalid base64url") from None
    _require(base64.urlsafe_b64encode(result).rstrip(b"=").decode() == value, "noncanonical base64url")
    return result


def _spki(key: Any) -> bytes:
    return cast("bytes", key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))


def _cert(value: Any) -> x509.Certificate:
    _require(isinstance(value, str), "invalid certificate")
    _require(len(value) <= 65536, "oversize certificate")
    return x509.load_pem_x509_certificate(value.encode("ascii"))


def certificate_sha256(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


def _verify_signature(key: Any, signature: bytes, data: bytes, algorithm: Any) -> None:
    if isinstance(key, rsa.RSAPublicKey):
        _require(algorithm is not None, "missing RSA digest")
        key.verify(signature, data, padding.PKCS1v15(), algorithm)
    elif isinstance(key, ec.EllipticCurvePublicKey):
        _require(algorithm is not None, "missing ECDSA digest")
        key.verify(signature, data, ec.ECDSA(algorithm))
    elif isinstance(key, ed25519.Ed25519PublicKey):
        key.verify(signature, data)
    else:
        raise EvidenceError("unsupported signature algorithm")


def verify_certificate_chain(chain: list[x509.Certificate], policy: HardwarePolicy) -> None:
    """Verify ordered root-to-leaf path and external root pins, at explicit time.

    This is a deliberately narrow supplied-path verifier, not an ambient PKI
    builder. No AIA fetching, OS roots, name matching heuristics or revocation
    claims. All issuers must be CAs; path length/key usage are enforced.
    """
    _require(policy.at is not None, "explicit verification time required")
    assert policy.at is not None
    _require(
        policy.at.tzinfo is not None and policy.at.utcoffset() is not None, "timezone-aware verification time required"
    )
    _require(len(chain) >= 2, "incomplete certificate chain")
    _require(certificate_sha256(chain[0]) in policy.root_sha256, "unpinned root")
    _require(chain[0].issuer == chain[0].subject, "root is not self-issued")
    for index, cert in enumerate(chain):
        _require(
            cert.not_valid_before_utc <= policy.at <= cert.not_valid_after_utc, "certificate outside validity interval"
        )
        # Unknown critical extensions must not be silently ignored.
        _require(
            not any(
                ext.critical and ext.oid not in {ExtensionOID.BASIC_CONSTRAINTS, ExtensionOID.KEY_USAGE}
                for ext in cert.extensions
            ),
            "unsupported critical certificate extension",
        )
        if index < len(chain) - 1:
            constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
            _require(constraints.ca, "issuer is not a CA")
            if constraints.path_length is not None:
                _require(len(chain) - index - 2 <= constraints.path_length, "certificate path length exceeded")
            try:
                usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
                _require(usage.key_cert_sign, "issuer cannot sign certificates")
            except x509.ExtensionNotFound:
                pass
        else:
            try:
                usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
                _require(usage.digital_signature, "leaf cannot sign evidence")
            except x509.ExtensionNotFound:
                pass
        issuer = chain[max(0, index - 1)]
        _require(cert.issuer == issuer.subject, "certificate issuer mismatch")
        _verify_signature(
            issuer.public_key(), cert.signature, cert.tbs_certificate_bytes, cert.signature_hash_algorithm
        )


def decode_yubico_extensions(cert: x509.Certificate) -> YubicoExtensions:
    """Decode raw firmware/policy/form octets and the DER INTEGER serial."""

    def raw(suffix: int, length: int) -> bytes:
        extension = cert.extensions.get_extension_for_oid(ObjectIdentifier(f"1.3.6.1.4.1.41482.3.{suffix}")).value
        _require(isinstance(extension, x509.UnrecognizedExtension), "invalid Yubico extension type")
        assert isinstance(extension, x509.UnrecognizedExtension)
        value = extension.value
        _require(len(value) == length, "invalid Yubico extension length")
        return value

    firmware = raw(3, 3)
    serial_extension = cert.extensions.get_extension_for_oid(ObjectIdentifier("1.3.6.1.4.1.41482.3.7")).value
    _require(isinstance(serial_extension, x509.UnrecognizedExtension), "invalid serial extension")
    assert isinstance(serial_extension, x509.UnrecognizedExtension)
    encoded = serial_extension.value
    _require(
        3 <= len(encoded) <= 7 and encoded[0] == 2 and encoded[1] == len(encoded) - 2, "invalid serial DER integer"
    )
    integer = encoded[2:]
    _require(
        not integer[0] & 0x80 and not (len(integer) > 1 and integer[0] == 0 and not integer[1] & 0x80),
        "invalid serial DER integer",
    )
    serial = int.from_bytes(integer, "big")
    _require(serial <= 0xFFFFFFFF, "serial exceeds uint32")
    policies = raw(8, 2)
    factor = raw(9, 1)[0]
    return YubicoExtensions(
        (firmware[0], firmware[1], firmware[2]),
        serial,
        PinPolicy(policies[0]),
        TouchPolicy(policies[1]),
        FormFactor(factor & 0x0F),
        bool(factor & 0x80),
        bool(factor & 0x40),
    )


def _resolve_time(payload: Mapping[str, Any], policy: HardwarePolicy) -> HardwarePolicy:
    if policy.at is not None:
        return policy
    timestamp = payload.get("timestamp")
    _require(isinstance(timestamp, str), "explicit time or signed receipt timestamp required")
    assert isinstance(timestamp, str)
    at = datetime.fromisoformat(timestamp)
    _require(at.tzinfo is not None and at.utcoffset() is not None, "timezone-aware verification time required")
    return replace(policy, at=at)


def _piv(payload: Mapping[str, Any], signer_pem: str, policy: HardwarePolicy) -> EvidenceVerdict:
    policy = _resolve_time(payload, policy)
    assert policy.at is not None
    raw_chain = payload["chain"]
    _require(
        isinstance(raw_chain, list) and 4 <= len(cast("list[Any]", raw_chain)) <= 8,
        "PIV requires root/intermediates/F9/leaf",
    )
    chain = [_cert(item) for item in cast("list[Any]", raw_chain)]
    _require(certificate_sha256(chain[-2]) in policy.f9_sha256, "unpinned F9")
    leaf, f9 = chain[-1], chain[-2]
    _require(isinstance(f9.public_key(), rsa.RSAPublicKey), "RSA F9 required")
    verify_certificate_chain(chain, policy)
    extensions = decode_yubico_extensions(leaf)
    if policy.approval:
        _require(
            extensions.pin != PinPolicy.NEVER and extensions.touch != TouchPolicy.NEVER,
            "approval requires PIN and touch",
        )
    key = leaf.public_key()
    _require(
        isinstance(key, ed25519.Ed25519PublicKey)
        or (isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1)),
        "unsupported PIV key",
    )
    if isinstance(key, ed25519.Ed25519PublicKey):
        _require(extensions.firmware >= (5, 7, 0), "Ed25519 requires firmware 5.7 or newer")
    signer = serialization.load_pem_public_key(signer_pem.encode("ascii"))
    _require(_spki(key) == _spki(signer), "attested key differs from receipt signer")
    return EvidenceVerdict(True, metadata=tuple(extensions.public_metadata().items()))


_FAILURES = (
    EvidenceError,
    ValueError,
    TypeError,
    KeyError,
    IndexError,
    UnicodeError,
    InvalidSignature,
    UnsupportedAlgorithm,
    x509.ExtensionNotFound,
    x509.DuplicateExtension,
    RecursionError,
)


def verify_piv_attestation(payload: Any, *, signer_pem: str, policy: HardwarePolicy) -> EvidenceVerdict:
    """Verify PIV evidence without disclosing serial or certificate contents."""
    try:
        _require(isinstance(payload, Mapping), "PIV evidence must be an object")
        return _piv(payload, signer_pem, policy)
    except _FAILURES as exc:
        return EvidenceVerdict(False, (str(exc) if isinstance(exc, EvidenceError) else "invalid PIV evidence",))


# A small bounded CBOR reader covers only WebAuthn's definite-length data
# model. No third-party dependency, indefinite lengths, tags or floats.
def _cbor(data: bytes, offset: int = 0, depth: int = 0) -> tuple[Any, int]:
    _require(depth < 16 and offset < len(data), "invalid CBOR")
    initial = data[offset]
    offset += 1
    major, extra = initial >> 5, initial & 31
    if extra < 24:
        size = extra
    else:
        width = {24: 1, 25: 2, 26: 4, 27: 8}.get(extra)
        _require(width is not None, "unsupported CBOR length")
        assert width is not None
        _require(offset + width <= len(data), "truncated CBOR")
        size = int.from_bytes(data[offset : offset + width], "big")
        offset += width
    _require(size <= 1_048_576, "oversize CBOR")
    if major in (0, 1):
        return size if major == 0 else -1 - size, offset
    if major in (2, 3):
        _require(offset + size <= len(data), "truncated CBOR")
        value = data[offset : offset + size]
        return (value if major == 2 else value.decode("utf-8")), offset + size
    if major == 4:
        items: list[Any] = []
        for _ in range(size):
            value, offset = _cbor(data, offset, depth + 1)
            items.append(value)
        return items, offset
    if major == 5:
        result: dict[Any, Any] = {}
        for _ in range(size):
            key, offset = _cbor(data, offset, depth + 1)
            _require(isinstance(key, (str, int, bytes)) and key not in result, "invalid or duplicate CBOR map key")
            value, offset = _cbor(data, offset, depth + 1)
            result[key] = value
        return result, offset
    raise EvidenceError("unsupported CBOR type")


def _cose_key(value: Any) -> Any:
    _require(isinstance(value, dict), "invalid COSE key")
    if value.get(3) == -8:
        _require(
            value.get(1) == 1 and value.get(-1) == 6 and isinstance(value.get(-2), bytes) and len(value[-2]) == 32,
            "invalid Ed25519 COSE key",
        )
        return ed25519.Ed25519PublicKey.from_public_bytes(value[-2])
    if value.get(3) == -7:
        _require(value.get(1) == 2 and value.get(-1) == 1, "invalid ES256 COSE key")
        _require(
            isinstance(value.get(-2), bytes)
            and isinstance(value.get(-3), bytes)
            and len(value[-2]) == len(value[-3]) == 32,
            "invalid ES256 coordinates",
        )
        return ec.EllipticCurvePublicNumbers(
            int.from_bytes(value[-2], "big"), int.from_bytes(value[-3], "big"), ec.SECP256R1()
        ).public_key()
    raise EvidenceError("unsupported COSE algorithm")


def _webauthn_signature(key: Any, alg: Any, signature: bytes, data: bytes) -> None:
    if alg == -8 and isinstance(key, ed25519.Ed25519PublicKey):
        key.verify(signature, data)
    elif alg == -7 and isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1):
        key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
    else:
        raise EvidenceError("signature algorithm/key mismatch")


def _json_unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "duplicate client data key")
        result[key] = value
    return result


def _client(raw: bytes, kind: str, challenge: bytes, policy: HardwarePolicy) -> None:
    parsed: Any = json.loads(raw, object_pairs_hook=_json_unique)
    _require(isinstance(parsed, dict), "invalid client data")
    document = cast("dict[str, Any]", parsed)
    _require(document.get("type") == kind, "wrong WebAuthn type")
    _require(_unb64(document.get("challenge")) == challenge, "challenge mismatch")
    _require(document.get("origin") in policy.origins and bool(policy.origins), "origin mismatch")
    _require(document.get("crossOrigin", False) is False, "cross-origin ceremony refused")


def _auth_header(data: bytes, policy: HardwarePolicy, *, registration: bool) -> int:
    _require(len(data) >= 37, "truncated authenticator data")
    _require(bool(policy.rp_id) and data[:32] == hashlib.sha256(policy.rp_id.encode()).digest(), "RP ID hash mismatch")
    flags = data[32]
    _require(bool(flags & 1), "user presence required")
    _require(not policy.require_uv or bool(flags & 4), "user verification required")
    _require(bool(flags & 0x40) == registration, "unexpected attested credential flag")
    _require(not flags & 0x80, "authenticator extensions unsupported")
    _require(not flags & 0x22 and (not flags & 0x10 or bool(flags & 0x08)), "invalid authenticator flags")
    if not registration:
        _require(len(data) == 37, "unexpected assertion authenticator data")
    return int.from_bytes(data[33:37], "big")


def registration_digest(registration: Mapping[str, Any]) -> str:
    """Digest retained registration evidence for independent enrollment policy."""
    return hashlib.sha256(canonical_receipt_bytes(registration)).hexdigest()


def _packed_certificate(cert: x509.Certificate, aaguid: bytes) -> None:
    _require(cert.version == x509.Version.v3, "packed certificate must be v3")
    for oid in (NameOID.COUNTRY_NAME, NameOID.ORGANIZATION_NAME, NameOID.COMMON_NAME):
        _require(bool(cert.subject.get_attributes_for_oid(oid)), "missing packed certificate subject attribute")
    ous = cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)
    _require(len(ous) == 1 and ous[0].value == "Authenticator Attestation", "invalid packed certificate OU")
    _require(
        not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca, "packed certificate must not be CA"
    )
    try:
        extension = cert.extensions.get_extension_for_oid(ObjectIdentifier("1.3.6.1.4.1.45724.1.1.4"))
    except x509.ExtensionNotFound:
        return
    _require(
        not extension.critical and isinstance(extension.value, x509.UnrecognizedExtension), "invalid AAGUID extension"
    )
    assert isinstance(extension.value, x509.UnrecognizedExtension)
    _require(extension.value.value == b"\x04\x10" + aaguid, "AAGUID mismatch")


def _registration(registration: Mapping[str, Any], policy: HardwarePolicy) -> tuple[bytes, Any, int]:
    _require(registration_digest(registration) in policy.registration_sha256, "registration not enrolled")
    auth = _unb64(registration["authenticator_data"])
    _auth_header(auth, policy, registration=True)
    _require(len(auth) >= 55, "truncated attested credential data")
    credential_length = int.from_bytes(auth[53:55], "big")
    _require(0 < credential_length <= 1023 and len(auth) >= 55 + credential_length, "invalid credential length")
    credential_id = auth[55 : 55 + credential_length]
    cose, end = _cbor(auth, 55 + credential_length)
    _require(end == len(auth), "trailing registration data")
    key = _cose_key(cose)
    client = _unb64(registration["client_data_json"])
    # The enrollment challenge is externally pinned as part of the retained
    # registration digest; it is not reused as an approval challenge.
    _client(client, "webauthn.create", _unb64(registration["challenge"]), policy)
    _require(registration.get("fmt") == "packed", "packed attestation required")
    chain_raw = registration["chain"]
    _require(isinstance(chain_raw, list) and 2 <= len(cast("list[Any]", chain_raw)) <= 8, "invalid registration chain")
    chain = [_cert(item) for item in cast("list[Any]", chain_raw)]
    verify_certificate_chain(chain, policy)
    _packed_certificate(chain[-1], auth[37:53])
    _webauthn_signature(
        chain[-1].public_key(),
        registration["alg"],
        _unb64(registration["signature"]),
        auth + hashlib.sha256(client).digest(),
    )
    return credential_id, key, cose[3]


def _fido(payload: Mapping[str, Any], policy: HardwarePolicy) -> EvidenceVerdict:
    policy = _resolve_time(payload, policy)
    registration = payload["registration"]
    _require(isinstance(registration, dict), "invalid registration")
    credential, key, algorithm = _registration(registration, policy)
    _require(_unb64(payload["credential_id"]) == credential, "credential ID mismatch")
    approved = payload["approved_payload"]
    _require(isinstance(approved, dict), "approved payload must be an object")
    client = _unb64(payload["client_data_json"])
    _client(client, "webauthn.get", hashlib.sha256(canonical_receipt_bytes(approved)).digest(), policy)
    auth = _unb64(payload["authenticator_data"])
    count = _auth_header(auth, policy, registration=False)
    _webauthn_signature(key, algorithm, _unb64(payload["signature"]), auth + hashlib.sha256(client).digest())
    previous = dict(policy.previous_counts).get(base64.urlsafe_b64encode(credential).rstrip(b"=").decode())
    warnings = (
        ("signature counter did not increase",) if count == 0 or (previous is not None and count <= previous) else ()
    )
    return EvidenceVerdict(
        True,
        warnings=warnings,
        metadata=(("sign_count", count), ("user_present", True), ("user_verified", bool(auth[32] & 4))),
    )


def verify_fido2_assertion(payload: Any, *, policy: HardwarePolicy) -> EvidenceVerdict:
    """Verify retained packed enrollment and one approval assertion offline."""
    try:
        _require(isinstance(payload, Mapping), "FIDO2 evidence must be an object")
        return _fido(payload, policy)
    except _FAILURES as exc:
        return EvidenceVerdict(False, (str(exc) if isinstance(exc, EvidenceError) else "invalid FIDO2 evidence",))


def bundled_root_pins() -> frozenset[str]:
    """Load and verify bundled vendor PEMs against their DER manifest."""
    directory = Path(__file__).with_name("hardware_roots")
    manifest = json.loads((directory / "manifest.json").read_text())
    pins: set[str] = set()
    for entry in manifest["certificates"]:
        cert = x509.load_pem_x509_certificate((directory / entry["file"]).read_bytes())
        digest = certificate_sha256(cert)
        _require(digest == entry["sha256_der"], "bundled root digest mismatch")
        pins.add(digest)
    return frozenset(pins)


def _context(envelope: ReceiptEnvelope, policy: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(policy, HardwarePolicy):
        return ("external HardwarePolicy required",), ()
    if envelope.kind == "key-attestation/piv":
        verdict = verify_piv_attestation(envelope.payload, signer_pem=envelope.public_key_pem, policy=policy)
    else:
        verdict = verify_fido2_assertion(envelope.payload, policy=policy)
    return verdict.errors, verdict.warnings


def _payload(payload: Mapping[str, Any]) -> tuple[str, ...]:
    return ()


register_receipt_kind("key-attestation/piv", payload_check=_payload, context_check=_context)
register_receipt_kind("approval/fido2-assertion", payload_check=_payload, context_check=_context)
