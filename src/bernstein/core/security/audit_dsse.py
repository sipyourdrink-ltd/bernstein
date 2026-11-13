"""DSSE / in-toto v1 envelope wrapper for the audit chain.

Wraps an :class:`bernstein.core.security.article12_bundle.Article12Bundle` (or a
:class:`bernstein.core.security.audit_multitenant.TenantScopedExport`) in a
[DSSE](https://github.com/secure-systems-lab/dsse) envelope whose payload is an
[in-toto attestation v1](https://github.com/in-toto/attestation/blob/main/spec/v1/README.md)
``Statement`` with a custom predicate type.

Why DSSE / in-toto:

* DSSE is the open standard for tamper-evident envelopes (used by Sigstore,
  in-toto, SLSA). The wire format is ``{"payload", "payloadType",
  "signatures"}`` plus the PAE (pre-authentication encoding) signing input -
  every implementation interoperates without bespoke parsing.
* in-toto v1 ``Statement`` lets a third-party verifier read what the artefact
  *is* (the ``subject`` digest) before deciding whether to trust the
  ``predicate`` body - important for an auditor who only wants to confirm
  bundle integrity without parsing bernstein-specific JSON.
* EU AI Act Art. 12(2)(c) and DORA Art. 9(3) want third-party-verifiable
  monitoring. HMAC alone is single-key and operator-trusted; an Ed25519
  signature over a DSSE envelope is verifiable by anyone holding the public
  key - the new ``tools/verify_audit_dsse.py`` does exactly that without
  importing any bernstein code.

Determinism contract:

* Same input bundle bytes → byte-identical envelope payload (canonical JSON,
  sorted keys, no whitespace, fixed field order).
* Same input bundle bytes + same Ed25519 private key → byte-identical
  envelope including signature, because Ed25519 is deterministic by spec
  (RFC 8032 §5.1.6).

Sigstore integration is deliberately out of scope for v1 of this module. The
existing :mod:`bernstein.core.security.sigstore_attestation` module already
covers the per-task Sigstore path; wiring the audit envelope through
Sigstore Fulcio + Rekor is tracked as a v2 follow-up.

Usage::

    from bernstein.core.security.article12_bundle import build_article12_bundle
    from bernstein.core.security.audit_dsse import wrap_bundle, write_envelope

    bundle = build_article12_bundle(audit_dir, since, until)
    envelope = wrap_bundle(bundle, signing_key=key)
    write_envelope(envelope, dest)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import serialization

if TYPE_CHECKING:
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    from bernstein.core.security.article12_bundle import Article12Bundle
    from bernstein.core.security.audit_multitenant import TenantScopedExport

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants - wire-format identifiers
# ---------------------------------------------------------------------------

#: DSSE payload type for the in-toto statement.
DSSE_PAYLOAD_TYPE: str = "application/vnd.in-toto+json"

#: in-toto v1 statement type identifier.
IN_TOTO_STATEMENT_TYPE: str = "https://in-toto.io/Statement/v1"

#: Custom predicate type for the bernstein audit envelope. Versioned so a
#: future v2 (e.g. tenant-scoped export, SCITT-anchored) can co-exist with v1.
BERNSTEIN_AUDIT_PREDICATE_TYPE: str = "https://bernstein.run/attestations/audit/v1"

#: Schema version for the envelope contents (separate from the predicate
#: type to allow non-breaking field additions inside the predicate body).
ENVELOPE_SCHEMA_VERSION: str = "1.0.0"

#: Subject "name" used when the bundle has no on-disk path (in-memory
#: roundtrip / tests).
_DEFAULT_SUBJECT_NAME: str = "audit-bundle"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DSSEError(RuntimeError):
    """Base class for DSSE wrap/verify failures."""


class EnvelopeFormatError(DSSEError):
    """Raised when an envelope is malformed (missing field, bad base64, etc.)."""


class EnvelopeSignatureError(DSSEError):
    """Raised when an envelope signature does not verify."""


class EnvelopeTypeMismatchError(DSSEError):
    """Raised when ``payloadType`` or the in-toto ``_type`` does not match."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Subject:
    """An in-toto statement subject (one ``name`` + one ``{algo: hex}`` digest).

    Attributes:
        name: Logical name of the artefact (e.g. the bundle filename).
        digest: ``{algorithm: hex_digest}`` map. We always emit ``sha256``.
    """

    name: str
    digest: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the in-toto v1 subject shape."""
        return {"name": self.name, "digest": dict(sorted(self.digest.items()))}


@dataclass(frozen=True, slots=True)
class Statement:
    """An in-toto v1 statement payload (pre-DSSE-encoding).

    Attributes:
        subjects: Artefacts being attested.
        predicate_type: URL identifying the predicate schema.
        predicate: Body - schema-defined fields below the predicate type.
    """

    subjects: list[Subject]
    predicate_type: str
    predicate: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the in-toto v1 statement shape (sorted, deterministic)."""
        return {
            "_type": IN_TOTO_STATEMENT_TYPE,
            "predicateType": self.predicate_type,
            "predicate": _sort_keys_recursive(self.predicate),
            "subject": [s.to_dict() for s in self.subjects],
        }


@dataclass(frozen=True, slots=True)
class Signature:
    """A single DSSE signature entry.

    Attributes:
        keyid: Caller-chosen identifier for the signing key (typically the
            sha256 of the public key DER bytes).
        sig: Base64-encoded raw signature bytes.
    """

    keyid: str
    sig: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the DSSE signature shape."""
        return {"keyid": self.keyid, "sig": self.sig}


@dataclass(frozen=True, slots=True)
class Envelope:
    """A DSSE envelope.

    Attributes:
        payload_type: Always :data:`DSSE_PAYLOAD_TYPE` for this module.
        payload_b64: Base64-encoded payload bytes (the in-toto statement
            JSON, encoded once to UTF-8).
        signatures: One or more DSSE signatures over the PAE input.
    """

    payload_type: str
    payload_b64: str
    signatures: list[Signature] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the DSSE wire format."""
        return {
            "payload": self.payload_b64,
            "payloadType": self.payload_type,
            "signatures": [s.to_dict() for s in self.signatures],
        }

    def to_json(self) -> bytes:
        """Return canonical JSON bytes - sorted keys, comma+colon separators."""
        return _canonical_json(self.to_dict())

    @property
    def payload_bytes(self) -> bytes:
        """Decode the base64 payload back to raw bytes."""
        return base64.b64decode(self.payload_b64)

    @property
    def statement(self) -> dict[str, Any]:
        """Parse the embedded payload as JSON. Raises on malformed JSON."""
        return json.loads(self.payload_bytes.decode("utf-8"))


# ---------------------------------------------------------------------------
# DSSE PAE encoding (mandated by the spec)
# ---------------------------------------------------------------------------


def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE Pre-Authentication Encoding (PAE).

    The DSSE spec requires every signature to be computed over::

        DSSEv1 SPACE LEN(type) SPACE type SPACE LEN(payload) SPACE payload

    where ``LEN`` is the ASCII-decimal byte length of the payload. The PAE
    prevents downgrade attacks that swap the payload type around a fixed
    payload.

    Args:
        payload_type: The DSSE ``payloadType`` field.
        payload: The raw payload bytes that get base64-encoded into the
            envelope. Sign over the bytes themselves, not the base64.

    Returns:
        The bytes that the signer must sign.
    """
    type_bytes = payload_type.encode("utf-8")
    return (
        b"DSSEv1 "
        + str(len(type_bytes)).encode("ascii")
        + b" "
        + type_bytes
        + b" "
        + str(len(payload)).encode("ascii")
        + b" "
        + payload
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _canonical_json(payload: dict[str, Any]) -> bytes:
    """Return deterministic JSON: sorted keys, compact separators, UTF-8."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sort_keys_recursive(value: Any) -> Any:
    """Recursively reorder dict keys so canonical JSON is byte-stable.

    ``json.dumps(sort_keys=True)`` already sorts top-level keys, but inside
    a free-form predicate we want lexicographic order at every depth so
    repeated wraps of the same input produce identical bytes.
    """
    if isinstance(value, dict):
        return {k: _sort_keys_recursive(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_sort_keys_recursive(v) for v in value]
    return value


def keyid_from_public_key(public_key: Ed25519PublicKey) -> str:
    """Compute a stable key id from an Ed25519 public key.

    Strategy: ``sha256`` of the SubjectPublicKeyInfo DER bytes, hex-encoded.
    Any consumer holding the same public key derives the same id.
    """
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def export_public_key_pem(public_key: Ed25519PublicKey) -> bytes:
    """Export an Ed25519 public key as PEM (used by the standalone verifier)."""
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


# ---------------------------------------------------------------------------
# Build / wrap
# ---------------------------------------------------------------------------


def _bundle_subject(
    *,
    name: str,
    bundle_bytes: bytes,
) -> Subject:
    """Build the in-toto subject for a bundle's bytes."""
    return Subject(name=name, digest={"sha256": hashlib.sha256(bundle_bytes).hexdigest()})


def _build_predicate(
    *,
    bundle_dict: dict[str, Any],
    chain_anchor: str,
    chain_length: int,
    bundle_kind: str,
) -> dict[str, Any]:
    """Assemble the audit-predicate body.

    Args:
        bundle_dict: ``Article12Bundle.to_dict()`` (or equivalent for
            multitenant exports). Captured so a verifier can spot-check the
            envelope describes the bundle without unzipping it.
        chain_anchor: HMAC of the last event in the chain (or the genesis
            sentinel when the bundle is empty).
        chain_length: Number of events covered by the chain.
        bundle_kind: ``article12`` or ``multitenant``.

    Returns:
        Dict suitable as the in-toto ``predicate`` body.
    """
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "bundle_kind": bundle_kind,
        "bundle": bundle_dict,
        "chain": {
            "anchor": chain_anchor,
            "length": chain_length,
        },
    }


def wrap_bundle(
    bundle: Article12Bundle | TenantScopedExport,
    *,
    signing_key: Ed25519PrivateKey,
    bundle_bytes: bytes | None = None,
    keyid: str | None = None,
    subject_name: str | None = None,
) -> Envelope:
    """Wrap an audit bundle in a DSSE envelope signed with Ed25519.

    Args:
        bundle: The bundle being attested. Either an :class:`Article12Bundle`
            (read from ``bernstein.core.security.article12_bundle``) or a
            :class:`TenantScopedExport` (from ``audit_multitenant``).
        signing_key: Ed25519 private key used to sign the PAE input. Caller
            owns key lifecycle.
        bundle_bytes: Raw bytes of the on-disk bundle. Required when the
            input is an :class:`Article12Bundle` whose ``archive_path`` is
            ``None`` (in-memory build); :class:`TenantScopedExport` always
            carries ``bundle_bytes`` so this can be omitted.
        keyid: Optional override. None or an empty string derives the ID
            from the signing key's public key.
        subject_name: Optional override. None or an empty string uses the
            archive filename if present, or a name derived from the
            Article 12 bundle ID or the tenant ID.

    Returns:
        A signed :class:`Envelope` ready to be persisted via
        :func:`write_envelope`.

    Raises:
        DSSEError: If the bundle has no resolvable bytes (in-memory bundle
            with no ``bundle_bytes`` argument).
    """
    # Lazy imports to keep this module a leaf-importable surface for tools
    # that do not need the Article12 / multitenant dependency graph.
    from bernstein.core.security.article12_bundle import Article12Bundle
    from bernstein.core.security.audit_multitenant import TenantScopedExport

    if isinstance(bundle, TenantScopedExport):
        resolved_bytes = bundle_bytes if bundle_bytes is not None else bundle.bundle_bytes
        bundle_dict = {
            "bundle_kind": "multitenant",
            "tenant_id": bundle.tenant_id,
            "since": bundle.since,
            "until": bundle.until,
            "event_count": bundle.event_count,
            "head_hmac": bundle.head_hmac,
            "head_sha256": bundle.head_sha256,
            "signature_kind": bundle.signature_kind,
            "sha256": bundle.sha256,
        }
        chain_anchor = bundle.head_hmac
        chain_length = bundle.event_count
        bundle_kind = "multitenant"
        default_name = (
            bundle.bundle_path.name if bundle.bundle_path is not None else f"audit-multitenant-{bundle.tenant_id}.json"
        )
    elif isinstance(bundle, Article12Bundle):
        resolved_bytes = _resolve_article12_bytes(bundle, bundle_bytes)
        bundle_dict = bundle.to_dict()
        chain_anchor = bundle.chain_anchor
        chain_length = bundle.event_count
        bundle_kind = "article12"
        default_name = (
            bundle.archive_path.name if bundle.archive_path is not None else f"article12_{bundle.bundle_id}.zip"
        )
    else:  # pragma: no cover - guarded for future bundle kinds
        msg = f"Unsupported bundle type: {type(bundle).__name__}"
        raise DSSEError(msg)

    name = subject_name or default_name
    subject = _bundle_subject(name=name, bundle_bytes=resolved_bytes)
    predicate = _build_predicate(
        bundle_dict=bundle_dict,
        chain_anchor=chain_anchor,
        chain_length=chain_length,
        bundle_kind=bundle_kind,
    )
    statement = Statement(
        subjects=[subject],
        predicate_type=BERNSTEIN_AUDIT_PREDICATE_TYPE,
        predicate=predicate,
    )

    payload = _canonical_json(statement.to_dict())
    pae_bytes = pae(DSSE_PAYLOAD_TYPE, payload)
    signature = signing_key.sign(pae_bytes)

    resolved_keyid = keyid or keyid_from_public_key(signing_key.public_key())
    return Envelope(
        payload_type=DSSE_PAYLOAD_TYPE,
        payload_b64=base64.b64encode(payload).decode("ascii"),
        signatures=[Signature(keyid=resolved_keyid, sig=base64.b64encode(signature).decode("ascii"))],
    )


def _resolve_article12_bytes(
    bundle: Article12Bundle,
    explicit_bytes: bytes | None,
) -> bytes:
    """Return the on-disk bundle bytes for an :class:`Article12Bundle`.

    Order: explicit override > on-disk read. Raises if neither is available.
    """
    if explicit_bytes is not None:
        return explicit_bytes
    if bundle.archive_path is None:
        msg = (
            "Article12Bundle has no archive_path and no explicit bundle_bytes - "
            "build with write=True or pass bundle_bytes to wrap_bundle()"
        )
        raise DSSEError(msg)
    return bundle.archive_path.read_bytes()


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnvelopeVerification:
    """Outcome of :func:`verify_envelope`.

    Attributes:
        ok: True iff the envelope signature verified and the payload type
            matched.
        statement: Parsed in-toto statement when readable (empty when the
            envelope was malformed before signature checking).
        keyid: keyid of the signature that successfully verified, or empty
            when verification failed.
        errors: Human-readable failure messages (empty when ``ok``).
    """

    ok: bool
    statement: dict[str, Any] = field(default_factory=dict)
    keyid: str = ""
    errors: list[str] = field(default_factory=list)


def verify_envelope(
    envelope: Envelope,
    public_key: Ed25519PublicKey,
    *,
    expected_payload_type: str = DSSE_PAYLOAD_TYPE,
    expected_predicate_type: str = BERNSTEIN_AUDIT_PREDICATE_TYPE,
) -> EnvelopeVerification:
    """Verify an envelope's signature and embedded statement type.

    Verification is intentionally narrow:

    * Payload type matches :data:`DSSE_PAYLOAD_TYPE`.
    * At least one signature verifies against ``public_key`` over the PAE.
    * Embedded statement carries ``_type=`` :data:`IN_TOTO_STATEMENT_TYPE`.
    * Embedded ``predicateType`` matches ``expected_predicate_type``.

    HMAC chain verification is delegated to whoever holds the chain key
    (e.g. the audit log owner) - the standalone verifier composes both.

    Args:
        envelope: Parsed envelope (typically from :func:`load_envelope`).
        public_key: Ed25519 public key the signer used.
        expected_payload_type: Override for non-default payload types.
        expected_predicate_type: Override for non-default predicate types.

    Returns:
        :class:`EnvelopeVerification` with ``ok`` flag and details.
    """
    errors: list[str] = []
    if envelope.payload_type != expected_payload_type:
        errors.append(
            f"payloadType mismatch: expected {expected_payload_type!r}, got {envelope.payload_type!r}",
        )
        return EnvelopeVerification(ok=False, errors=errors)

    try:
        payload = envelope.payload_bytes
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        return EnvelopeVerification(ok=False, errors=[f"payload base64 decode failed: {exc}"])

    pae_bytes = pae(envelope.payload_type, payload)
    verified_keyid = ""
    for sig in envelope.signatures:
        try:
            sig_bytes = base64.b64decode(sig.sig)
        except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
            errors.append(f"signature base64 decode failed for keyid={sig.keyid!r}: {exc}")
            continue
        try:
            public_key.verify(sig_bytes, pae_bytes)
            verified_keyid = sig.keyid
            break
        except Exception as exc:
            errors.append(f"signature verify failed for keyid={sig.keyid!r}: {exc}")

    if not verified_keyid:
        return EnvelopeVerification(ok=False, errors=errors)

    try:
        statement = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return EnvelopeVerification(ok=False, errors=[f"payload JSON decode failed: {exc}"])

    if statement.get("_type") != IN_TOTO_STATEMENT_TYPE:
        return EnvelopeVerification(
            ok=False,
            statement=statement,
            keyid=verified_keyid,
            errors=[f"statement _type mismatch: expected {IN_TOTO_STATEMENT_TYPE!r}, got {statement.get('_type')!r}"],
        )
    if statement.get("predicateType") != expected_predicate_type:
        return EnvelopeVerification(
            ok=False,
            statement=statement,
            keyid=verified_keyid,
            errors=[
                f"predicateType mismatch: expected {expected_predicate_type!r}, got {statement.get('predicateType')!r}",
            ],
        )

    return EnvelopeVerification(ok=True, statement=statement, keyid=verified_keyid)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_envelope(envelope: Envelope, path: Path) -> Path:
    """Persist an envelope to disk as canonical JSON.

    Args:
        envelope: The envelope to write.
        path: Output path. Parent directories are created.

    Returns:
        ``path`` for chaining.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(envelope.to_json())
    return path


def load_envelope(path: Path) -> Envelope:
    """Load an envelope from disk and validate the wire-format keys.

    Args:
        path: Path to a file produced by :func:`write_envelope`.

    Returns:
        Parsed :class:`Envelope`.

    Raises:
        EnvelopeFormatError: If a required field is missing or has the
            wrong shape.
    """
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"envelope at {path} is not valid UTF-8 JSON: {exc}"
        raise EnvelopeFormatError(msg) from exc

    return parse_envelope(data)


def parse_envelope(data: dict[str, Any]) -> Envelope:
    """Validate a dict matches the DSSE wire format and return an Envelope.

    Args:
        data: Already-parsed JSON dict.

    Returns:
        :class:`Envelope` with typed fields.

    Raises:
        EnvelopeFormatError: If any required field is missing.
    """
    if not isinstance(data, dict):
        msg = f"envelope must be a JSON object, got {type(data).__name__}"
        raise EnvelopeFormatError(msg)

    payload_b64 = data.get("payload")
    payload_type = data.get("payloadType")
    raw_signatures = data.get("signatures")

    if not isinstance(payload_b64, str):
        msg = "envelope is missing 'payload' (must be a base64 string)"
        raise EnvelopeFormatError(msg)
    if not isinstance(payload_type, str):
        msg = "envelope is missing 'payloadType' (must be a string)"
        raise EnvelopeFormatError(msg)
    if not isinstance(raw_signatures, list) or not raw_signatures:
        msg = "envelope is missing 'signatures' (must be a non-empty list)"
        raise EnvelopeFormatError(msg)

    signatures: list[Signature] = []
    for idx, sig_obj in enumerate(raw_signatures):
        if not isinstance(sig_obj, dict):
            msg = f"signatures[{idx}] must be an object"
            raise EnvelopeFormatError(msg)
        keyid = sig_obj.get("keyid", "")
        sig = sig_obj.get("sig", "")
        if not isinstance(keyid, str) or not isinstance(sig, str):
            msg = f"signatures[{idx}] must carry string 'keyid' and 'sig'"
            raise EnvelopeFormatError(msg)
        signatures.append(Signature(keyid=keyid, sig=sig))

    return Envelope(payload_type=payload_type, payload_b64=payload_b64, signatures=signatures)
