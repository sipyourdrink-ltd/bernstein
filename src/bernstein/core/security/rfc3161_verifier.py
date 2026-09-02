"""RFC 3161 TimeStampToken cryptographic chain validation.

The multi-tenant audit-chain export (PR #1175) ships an optional RFC 3161
TimeStampToken alongside the bundle but defers cryptographic chain
validation to the operator's external toolchain (``openssl ts -verify``).
v2 closes that gap with a self-contained verifier that:

1. Parses the token (either a bare ``TimeStampToken`` per RFC 3161 §2.4.2
   or the wrapping ``TimeStampResp`` returned by the TSA HTTP endpoint).
2. Walks the embedded TSA certificate chain, building it against the
   operator-supplied trust bundle (``--rfc3161-trusted-tsa-bundle``).
3. Verifies the TSA's CMS SignerInfo signature over the SignedAttributes
   block (RFC 5652 §5.4) - this is what binds the TSA's identity to the
   ``TSTInfo``.
4. Confirms ``TSTInfo.messageImprint == sha256(payload)`` (or whatever
   digest the TSA chose; we currently allow SHA-256 / SHA-384 / SHA-512).

References:

* RFC 3161 - Internet X.509 Public Key Infrastructure Time-Stamp Protocol.
* RFC 5652 - Cryptographic Message Syntax (CMS).
* RFC 5816 - ESSCertIDv2 (signing-cert anchor in CMS SignedAttributes).

Design notes:

* We use ``asn1crypto`` for ASN.1 parsing (MIT-licensed, pure Python, ~250KB).
  The alternative - vendoring a minimal DER parser - was rejected because
  RFC 3161 chains are CMS SignedData with optional authenticated attributes,
  and a hand-rolled parser large enough to support that surface would be
  fragile and easy to get subtly wrong.
* Trust-anchor walking uses ``cryptography.x509.verification`` (Rust-backed,
  audited). We hand it the embedded TSA cert + intermediates and pin the
  trust root to whatever the operator supplied in the bundle.
* The signature-over-SignedAttributes step uses ``cryptography``'s public-key
  primitives directly because the cryptography PKCS7 verifier does not
  surface RFC 3161 / CMS-with-authenticated-attributes paths.
* We deliberately do **not** check the TSA cert's ``Extended Key Usage``
  policy bit (``id-kp-timeStamping``) here - the trust bundle is the
  operator's policy decision. We surface the EKU in the verification
  result so callers can enforce it themselves.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID
from cryptography.x509.verification import (
    Criticality,
    ExtensionPolicy,
    PolicyBuilder,
    Store,
    VerificationError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.types import (
        CertificatePublicKeyTypes,
    )

logger = logging.getLogger(__name__)


class _Asn1Node(Protocol):
    def __getitem__(self, key: str) -> Any: ...


#: Recognized message-imprint hash OIDs -> ``hashlib`` constructor name.
_HASH_OID_TO_NAME: dict[str, str] = {
    "1.3.14.3.2.26": "sha1",
    "2.16.840.1.101.3.4.2.1": "sha256",
    "2.16.840.1.101.3.4.2.2": "sha384",
    "2.16.840.1.101.3.4.2.3": "sha512",
}

#: Hash OIDs strong enough to anchor an audit slice. SHA-1 is parsed but
#: rejected at verify time because it is no longer collision-resistant.
_ACCEPTED_HASH_OIDS: frozenset[str] = frozenset(
    {
        "2.16.840.1.101.3.4.2.1",
        "2.16.840.1.101.3.4.2.2",
        "2.16.840.1.101.3.4.2.3",
    },
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


def _empty_str_list() -> list[str]:
    return []


@dataclass(frozen=True, slots=True)
class RFC3161Verification:
    """Outcome of :func:`verify_rfc3161_token`.

    Attributes:
        ok: ``True`` iff every verification step passed.
        errors: Human-readable failure messages (empty when ``ok``).
        gen_time: Timestamp the TSA recorded for the ``messageImprint``.
            ``None`` when parsing failed before this step.
        tsa_subject: Subject DN of the TSA certificate that signed the
            token, or ``None`` when parsing failed.
        hash_algorithm: Name of the hash algorithm used in the
            ``messageImprint`` (``sha256`` / ``sha384`` / ``sha512``).
        eku_timestamping: ``True`` when the TSA cert advertises the
            ``id-kp-timeStamping`` extended key usage. Surfaced for
            callers that want to enforce the policy bit.
        warnings: Non-fatal messages for compatibility diagnostics.
    """

    ok: bool
    errors: list[str] = field(default_factory=_empty_str_list)
    gen_time: datetime | None = None
    tsa_subject: str | None = None
    hash_algorithm: str | None = None
    eku_timestamping: bool = False
    warnings: list[str] = field(default_factory=_empty_str_list)


# ---------------------------------------------------------------------------
# Trust bundle loader
# ---------------------------------------------------------------------------


def load_trusted_tsa_certs(bundle_path: Path) -> list[x509.Certificate]:
    """Load PEM/DER X.509 certs from a trust bundle file.

    Accepts either a single PEM/DER cert or a concatenated PEM bundle
    (``cat tsa.crt cacert.crt > bundle.pem``). We do not honour OS trust
    stores - operators must explicitly pin TSA roots they accept.

    Args:
        bundle_path: Path to the bundle file.

    Returns:
        Non-empty list of parsed certificates.

    Raises:
        ValueError: When the file is missing, empty, or contains no
            parseable certificates.
    """
    if not bundle_path.is_file():
        raise ValueError(f"trusted TSA bundle not found: {bundle_path}")
    raw = bundle_path.read_bytes()
    if not raw.strip():
        raise ValueError(f"trusted TSA bundle is empty: {bundle_path}")
    certs: list[x509.Certificate] = []
    if b"-----BEGIN CERTIFICATE-----" in raw:
        certs.extend(x509.load_pem_x509_certificates(raw))
    else:
        # Single DER blob.
        certs.append(x509.load_der_x509_certificate(raw))
    if not certs:
        raise ValueError(f"trusted TSA bundle contained no certificates: {bundle_path}")
    return certs


# ---------------------------------------------------------------------------
# Token parsing
# ---------------------------------------------------------------------------


def _try_parse_response_or_token(
    token_bytes: bytes,
    tsp_module: Any,
    cms_module: Any,
) -> _Asn1Node:
    """Parse *token_bytes* as either ``TimeStampResp`` or bare ``ContentInfo``.

    Returns the wrapping ``ContentInfo``. Raises :class:`ValueError` when:

    * The TSA explicitly refused the request (``status != granted``) - we
      do NOT fall through to a bare-token attempt because the response
      structure is recognisable; the operator needs to know the TSA said
      no.
    * Both shapes fail to parse.
    """
    try:
        resp = tsp_module.TimeStampResp.load(token_bytes)
        status = resp["status"]["status"].native
    except (ValueError, KeyError, TypeError) as exc:
        # Not a TimeStampResp shape - fall through to ContentInfo.
        try:
            return cms_module.ContentInfo.load(token_bytes)
        except (ValueError, KeyError, TypeError) as exc2:
            raise ValueError(
                f"could not parse RFC 3161 token: {exc2} (and not a TimeStampResp: {exc})",
            ) from exc2
    if status not in {"granted", "granted_with_mods"}:
        raise ValueError(f"TSA returned non-granted status: {status!r}")
    return resp["time_stamp_token"]


def _parse_token(token_bytes: bytes) -> tuple[_Asn1Node, _Asn1Node, list[x509.Certificate], _Asn1Node]:
    """Extract SignedData, TSTInfo, embedded certs, and the SignerInfo.

    Accepts either:

    * A bare ``TimeStampToken`` (CMS ``ContentInfo`` wrapping ``SignedData``).
      Produced by ``openssl cms -in <token> ...`` style flows.
    * A full ``TimeStampResp`` (RFC 3161 §2.4.2) - what a TSA returns from
      the HTTP endpoint. We reach into ``response.timeStampToken``.

    Returns:
        ``(signed_data, tst_info, embedded_certs, signer_info)``.

    Raises:
        ValueError: When parsing fails or the token is malformed.
    """
    # Lazy import: keeps the dep optional at import time when no caller
    # actually wires RFC 3161 verification.
    try:
        from asn1crypto import cms, tsp  # type: ignore[reportMissingTypeStubs]
    except ImportError as exc:
        raise ValueError(
            "asn1crypto is required for RFC 3161 verification. Reinstall bernstein or `pip install asn1crypto`.",
        ) from exc

    # Two acceptable input shapes:
    # 1. ``TimeStampResp`` (RFC 3161 §2.4.2) - what a TSA's HTTP endpoint
    #    returns. We descend into ``response.timeStampToken``.
    # 2. Bare ``TimeStampToken`` (CMS ContentInfo) - the form an operator
    #    extracts via ``openssl cms`` for archival storage.
    # We try TimeStampResp first; if the parser disagrees, we fall back to
    # ContentInfo. Explicit "non-granted" errors must surface to the caller
    # so we can distinguish "wrong shape" from "TSA refused the request".
    content_info = _try_parse_response_or_token(token_bytes, tsp, cms)

    if content_info["content_type"].native != "signed_data":
        raise ValueError("RFC 3161 token is not a CMS SignedData")
    signed_data = content_info["content"]
    encap = signed_data["encap_content_info"]
    if encap["content_type"].native != "tst_info":
        raise ValueError(
            f"unexpected eContentType: {encap['content_type'].native!r}",
        )
    tst_info = encap["content"].parsed
    raw_certs = cast("list[Any]", signed_data["certificates"] or [])
    embedded_certs: list[x509.Certificate] = []
    for choice in raw_certs:
        # CMS ChoiceOfCertificate - only X.509 certs interest us.
        cert = choice.chosen
        cert_der = cast(bytes, cert.dump())
        embedded_certs.append(x509.load_der_x509_certificate(cert_der))
    signer_infos = signed_data["signer_infos"]
    if len(signer_infos) != 1:
        raise ValueError(
            f"expected exactly one SignerInfo, got {len(signer_infos)}",
        )
    return signed_data, tst_info, embedded_certs, signer_infos[0]


# ---------------------------------------------------------------------------
# Signing-cert resolution
# ---------------------------------------------------------------------------


def _signing_cert(
    signer_info: _Asn1Node,
    embedded_certs: list[x509.Certificate],
) -> x509.Certificate:
    """Find the cert in ``embedded_certs`` that matches ``signer_info.sid``.

    SignerInfo.sid is either ``IssuerAndSerialNumber`` or
    ``SubjectKeyIdentifier``; we handle both.
    """
    sid = signer_info["sid"]
    sid_kind = sid.name
    if sid_kind == "issuer_and_serial_number":
        target_issuer_dn = sid.chosen["issuer"].chosen.dump()
        target_serial = int(sid.chosen["serial_number"].native)
        for cert in embedded_certs:
            if cert.serial_number == target_serial and cert.issuer.public_bytes() == target_issuer_dn:
                return cert
    elif sid_kind == "subject_key_identifier":
        target_ski = bytes(sid.chosen.native)
        for cert in embedded_certs:
            try:
                ski_ext = cert.extensions.get_extension_for_class(
                    x509.SubjectKeyIdentifier,
                )
            except x509.ExtensionNotFound:
                continue
            if ski_ext.value.digest == target_ski:
                return cert
    raise ValueError(
        f"could not match signing cert from SignerInfo.sid (kind={sid_kind!r}) "
        f"against {len(embedded_certs)} embedded cert(s)",
    )


# ---------------------------------------------------------------------------
# CMS SignedAttributes signature verification
# ---------------------------------------------------------------------------


def _verify_signed_attrs_signature(
    signer_info: _Asn1Node,
    signing_cert: x509.Certificate,
    tst_info_bytes: bytes,
) -> None:
    """Confirm the SignerInfo signature is valid for the SignedAttributes.

    Per RFC 5652 §5.4, when ``signed_attrs`` is present, the signature
    covers the DER encoding of the SignedAttributes (with a SET-OF tag,
    not the IMPLICIT [0] tag carried in the SignerInfo). When it is
    absent, the signature is over the eContent directly.

    We additionally verify the ``message-digest`` signed attribute equals
    ``hash(eContent)``: an attacker who flipped the eContent without
    re-signing would still need to corrupt the message-digest attribute,
    and the SignedAttributes signature would catch that.

    Raises:
        ValueError: When the SignerInfo is malformed or the digest
            mismatches.
        InvalidSignature: When the signature does not validate.
    """
    digest_alg = signer_info["digest_algorithm"]["algorithm"].native
    sig_alg = signer_info["signature_algorithm"]["algorithm"].native
    sig_bytes = bytes(signer_info["signature"].native)
    signed_attrs = signer_info["signed_attrs"]

    hash_obj = _hash_for_oid_or_name(digest_alg)
    if hash_obj is None:
        raise ValueError(f"unsupported digest algorithm: {digest_alg!r}")

    if signed_attrs and len(signed_attrs):
        # Rebuild the message-digest signed attribute and compare.
        message_digest = None
        for attr in signed_attrs:
            if attr["type"].native == "message_digest":
                message_digest = bytes(attr["values"][0].native)
                break
        if message_digest is None:
            raise ValueError("SignedAttributes missing message_digest attribute")
        actual = hashlib.new(hash_obj.name, tst_info_bytes).digest()
        if message_digest != actual:
            raise ValueError(
                "CMS message_digest attribute does not match TSTInfo digest",
            )
        # Per RFC 5652 §5.4, the signature covers the DER encoding of the
        # SignedAttributes as a SET-OF, NOT with the IMPLICIT [0] tag that
        # appears in SignerInfo. asn1crypto's .untag() drops the IMPLICIT
        # context tag and serialises with the universal SET tag (0x31),
        # which is exactly the wire format the TSA signed.
        payload_to_verify = signed_attrs.untag().dump(force=True)
    else:
        payload_to_verify = tst_info_bytes

    public_key = signing_cert.public_key()
    _verify_with_public_key(public_key, sig_alg, sig_bytes, payload_to_verify, hash_obj)


def _hash_for_oid_or_name(name_or_oid: str) -> hashes.HashAlgorithm | None:
    """Map digest OID/name to a ``cryptography.hazmat`` hash instance.

    Only SHA-256 / SHA-384 / SHA-512 are accepted for signature
    verification. Legacy SHA-1 identifiers are recognized elsewhere only
    so the verifier can report a clear message-imprint policy error.
    """
    table: dict[str, hashes.HashAlgorithm] = {
        "sha256": hashes.SHA256(),
        "sha384": hashes.SHA384(),
        "sha512": hashes.SHA512(),
        "2.16.840.1.101.3.4.2.1": hashes.SHA256(),
        "2.16.840.1.101.3.4.2.2": hashes.SHA384(),
        "2.16.840.1.101.3.4.2.3": hashes.SHA512(),
    }
    return table.get(name_or_oid.lower())


def _verify_with_public_key(
    public_key: CertificatePublicKeyTypes,
    sig_alg: str,
    sig_bytes: bytes,
    payload: bytes,
    hash_obj: hashes.HashAlgorithm,
) -> None:
    """Verify ``sig_bytes`` over ``payload`` using ``public_key``.

    Supports the algorithms FreeTSA and most commercial TSAs use:
    RSA + PKCS1v15, ECDSA, RSA-PSS. Raises :class:`InvalidSignature` on
    mismatch and :class:`ValueError` on unsupported algorithms.
    """
    alg_lower = sig_alg.lower()
    if isinstance(public_key, rsa.RSAPublicKey):
        if "pss" in alg_lower:
            public_key.verify(
                sig_bytes,
                payload,
                padding.PSS(mgf=padding.MGF1(hash_obj), salt_length=padding.PSS.MAX_LENGTH),
                hash_obj,
            )
            return
        # Default: RSA PKCS#1 v1.5.
        public_key.verify(sig_bytes, payload, padding.PKCS1v15(), hash_obj)
        return
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        public_key.verify(sig_bytes, payload, ec.ECDSA(hash_obj))
        return
    raise ValueError(
        f"unsupported TSA signing key type: {type(public_key).__name__} (alg={sig_alg!r})",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_rfc3161_token(
    token_bytes: bytes,
    payload_hash: bytes,
    trusted_tsa_certs: list[x509.Certificate],
    *,
    verification_time: datetime | None = None,
) -> RFC3161Verification:
    """Cryptographically verify an RFC 3161 TimeStampToken.

    The verifier walks four checks; each can fail independently:

    1. ASN.1 parse - recover SignedData / TSTInfo / embedded certs.
    2. Trust path - anchor the embedded TSA cert against
       ``trusted_tsa_certs`` using ``cryptography.x509.verification``.
    3. CMS signature - confirm the TSA's SignerInfo signature over
       SignedAttributes (and that ``message-digest`` matches eContent).
    4. Message imprint - confirm
       ``TSTInfo.messageImprint == sha256(payload)``.

    Args:
        token_bytes: Either a bare ``TimeStampToken`` or a full
            ``TimeStampResp`` (the TSA HTTP response).
        payload_hash: Raw hash bytes of the payload that should match
            ``TSTInfo.messageImprint.hashedMessage``. Length depends on
            the algorithm the TSA chose; we cross-check.
        trusted_tsa_certs: Non-empty list of operator-trusted X.509
            certificates. Use :func:`load_trusted_tsa_certs` to load
            from disk.
        verification_time: Wall-clock time at which to validate the cert
            chain. Defaults to the TSA's ``genTime`` so a token whose TSA
            cert has since expired still verifies in the past.

    Returns:
        :class:`RFC3161Verification` with ``ok`` and per-step diagnostics.
    """
    errors: list[str] = []
    warnings: list[str] = []
    gen_time: datetime | None = None
    tsa_subject: str | None = None
    hash_alg_name: str | None = None
    eku_timestamping = False

    if not trusted_tsa_certs:
        return RFC3161Verification(
            ok=False,
            errors=["trusted TSA cert list is empty"],
        )

    # Step 1 - parse.
    try:
        signed_data, tst_info, embedded_certs, signer_info = _parse_token(token_bytes)
    except ValueError as exc:
        return RFC3161Verification(ok=False, errors=[f"parse: {exc}"])

    # Step 4 (early) - message imprint. Fails fast before chain walk.
    try:
        mi = tst_info["message_imprint"]
        hash_oid = mi["hash_algorithm"]["algorithm"].dotted
        embedded_hash = bytes(mi["hashed_message"].native)
        hash_alg_name = _HASH_OID_TO_NAME.get(hash_oid, hash_oid)
        if hash_oid not in _ACCEPTED_HASH_OIDS:
            errors.append(
                f"messageImprint uses unsupported or weak hash algorithm: {hash_alg_name}",
            )
        if embedded_hash != payload_hash:
            errors.append(
                "messageImprint mismatch: TSA token covers a different payload",
            )
    except (KeyError, AttributeError, ValueError, TypeError) as exc:
        errors.append(f"messageImprint: {exc}")

    try:
        gen_time = tst_info["gen_time"].native
    except (KeyError, AttributeError):
        errors.append("TSTInfo missing genTime")

    # Step 2 - trust path.
    try:
        signing_cert = _signing_cert(signer_info, embedded_certs)
        tsa_subject = signing_cert.subject.rfc4514_string()
        eku_timestamping = _has_timestamping_eku(signing_cert)
    except ValueError as exc:
        errors.append(f"trust: {exc}")
        signing_cert = None

    if signing_cert is not None:
        chain_time = verification_time or gen_time or datetime.now(tz=signing_cert.not_valid_after_utc.tzinfo)
        try:
            _walk_chain(
                signing_cert=signing_cert,
                embedded_certs=embedded_certs,
                trust_anchors=trusted_tsa_certs,
                verification_time=chain_time,
            )
        except VerificationError as exc:
            errors.append(f"trust: chain walk failed: {exc}")
        except ValueError as exc:
            errors.append(f"trust: {exc}")

    # Step 3 - CMS signature.
    if signing_cert is not None:
        try:
            tst_info_bytes = bytes(
                signed_data["encap_content_info"]["content"].contents,
            )
            _verify_signed_attrs_signature(signer_info, signing_cert, tst_info_bytes)
        except (InvalidSignature, ValueError) as exc:
            errors.append(f"signature: {exc}")

    return RFC3161Verification(
        ok=not errors,
        errors=errors,
        gen_time=gen_time,
        tsa_subject=tsa_subject,
        hash_algorithm=hash_alg_name,
        eku_timestamping=eku_timestamping,
        warnings=warnings,
    )


def _has_timestamping_eku(cert: x509.Certificate) -> bool:
    """Return True iff *cert* lists ``id-kp-timeStamping`` in its EKU."""
    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    except x509.ExtensionNotFound:
        return False
    return ExtendedKeyUsageOID.TIME_STAMPING in eku.value


def _walk_chain(
    *,
    signing_cert: x509.Certificate,
    embedded_certs: list[x509.Certificate],
    trust_anchors: list[x509.Certificate],
    verification_time: datetime,
) -> None:
    """Anchor *signing_cert* against *trust_anchors* via embedded intermediates.

    Uses ``cryptography.x509.verification`` (Rust-backed). The verifier
    is configured with the trust store and the embedded chain
    (intermediates) is fed in. Time-stamping EKU enforcement is left to
    the caller - :class:`PolicyBuilder` does not surface that hook in
    the public API yet.

    Raises:
        VerificationError: When no path to a trust anchor is found.
        ValueError: When the public key type is unsupported or the
            certificate cannot be parsed.
    """
    intermediates = [
        c for c in embedded_certs if c.fingerprint(hashes.SHA256()) != signing_cert.fingerprint(hashes.SHA256())
    ]
    store = Store(trust_anchors)
    # The webpki defaults reject many real-world TSA chains because:
    # 1. The EE (TSA leaf) cert does not carry CABF SAN constraints -
    #    TSA certs are CMS signers, not TLS endpoints.
    # 2. Many TSA intermediates ship basicConstraints as non-critical
    #    (FreeTSA is the canonical example), which webpki rejects.
    # We require basicConstraints to be PRESENT on CAs (so untrusted
    # leaves cannot impersonate intermediates) but we are AGNOSTIC about
    # criticality. The leaf policy is fully permissive - the time-stamping
    # EKU is surfaced on :class:`RFC3161Verification` so the caller can
    # enforce policy explicitly when needed.
    ca_policy = ExtensionPolicy.permit_all().require_present(
        x509.BasicConstraints,
        Criticality.AGNOSTIC,
        None,
    )
    ee_policy = ExtensionPolicy.permit_all()
    verifier = (
        PolicyBuilder()
        .store(store)
        .time(verification_time)
        .extension_policies(ca_policy=ca_policy, ee_policy=ee_policy)
    ).build_client_verifier()
    # ``verify`` raises VerificationError on failure; we use the client
    # verifier (no SAN required) because TSA certs do not carry server
    # SANs. The time-stamping EKU is checked via ``_has_timestamping_eku``
    # by the caller, since the verifier focuses on path-building.
    verifier.verify(signing_cert, intermediates)


@dataclass(frozen=True, slots=True)
class RFC3161Imprint:
    """What a token *claims*, recovered without any trust decision.

    Parsing is not verifying: this says which digest the token covers and
    when the TSA says it did so, but nothing about who signed it. Callers
    that need an identity must still run :func:`verify_rfc3161_token` with
    operator-supplied trust anchors. The split exists because binding a
    token to the bytes it covers is useful on its own - an offline install
    with no trust bundle can still refuse a token issued over something
    else.

    Attributes:
        hash_algorithm: ``sha256`` / ``sha384`` / ``sha512``.
        hashed_message: Raw ``messageImprint.hashedMessage`` bytes.
        gen_time: ``TSTInfo.genTime``, or ``None`` when absent.
    """

    hash_algorithm: str
    hashed_message: bytes
    gen_time: datetime | None


def read_token_imprint(token_bytes: bytes) -> RFC3161Imprint:
    """Return the messageImprint and genTime *token_bytes* carries.

    Args:
        token_bytes: A bare ``TimeStampToken`` or a full ``TimeStampResp``.

    Returns:
        The parsed :class:`RFC3161Imprint`.

    Raises:
        ValueError: When the token does not parse, the messageImprint is
            malformed, or its hash algorithm is not one we accept as
            strong enough to anchor an audit history.
    """
    _signed_data, tst_info, _certs, _signer_info = _parse_token(token_bytes)
    try:
        imprint = tst_info["message_imprint"]
        hash_oid = imprint["hash_algorithm"]["algorithm"].dotted
        hashed_message = bytes(imprint["hashed_message"].native)
    except (KeyError, AttributeError, ValueError, TypeError) as exc:
        raise ValueError(f"messageImprint: {exc}") from exc
    hash_name = str(_HASH_OID_TO_NAME.get(hash_oid, hash_oid))
    if hash_oid not in _ACCEPTED_HASH_OIDS:
        raise ValueError(
            f"messageImprint uses unsupported or weak hash algorithm: {hash_name}",
        )
    try:
        gen_time = cast("datetime | None", tst_info["gen_time"].native)
    except (KeyError, AttributeError):
        gen_time = None
    return RFC3161Imprint(
        hash_algorithm=hash_name,
        hashed_message=hashed_message,
        gen_time=gen_time,
    )


def hash_payload_for_tsa(payload: bytes, *, algorithm: str = "sha256") -> bytes:
    """Hash *payload* with the requested algorithm for messageImprint compare.

    Helper so callers do not have to import hashlib themselves and so we
    centralise the algorithm whitelist used by :func:`verify_rfc3161_token`.

    Args:
        payload: Raw bytes the TSA timestamped.
        algorithm: One of ``sha256`` / ``sha384`` / ``sha512``. Defaults
            to SHA-256, matching the bundle's ``head_sha256`` anchor.

    Returns:
        Raw digest bytes.

    Raises:
        ValueError: When *algorithm* is not in the accepted list.
    """
    if algorithm not in {"sha256", "sha384", "sha512"}:
        raise ValueError(
            f"unsupported messageImprint algorithm: {algorithm!r} (must be sha256/sha384/sha512)",
        )
    return hashlib.new(algorithm, payload).digest()


__all__ = [
    "RFC3161Imprint",
    "RFC3161Verification",
    "hash_payload_for_tsa",
    "load_trusted_tsa_certs",
    "read_token_imprint",
    "verify_rfc3161_token",
]
