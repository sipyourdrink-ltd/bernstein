"""Shared document substrate for the volunteer protocol.

Every volunteer document (candidacy, result receipt, dispute, etc.) is
wrapped in a DSSE / in-toto v1 envelope so that any third party holding the
signer's public key can verify provenance and integrity without importing
bernstein code.

This module is the **one** place the sign-and-verify dance lives for the
volunteer protocol.  Individual document types define their own dataclasses
and predicate bodies, but they all call :func:`sign_document` to produce an
:func:`Envelope` and :func:`verify_document` to check one.

Design decisions
----------------

* **One shared helper, not five copies.**  The sibling protocols
  (``a2a``, ``acp``) each have their own sign/verify logic because they
  arrived independently.  The volunteer protocol ships later and can learn
  from that: a single shared module prevents the "three-step copy" drift
  where each new document type re-implements ``canonical_bytes`` +
  ``Statement`` + ``Envelope`` with subtly different serialisation.

* **Reuses** ``audit_dsse`` **wholesale.**  ``Envelope``, ``Statement``,
  ``Subject``, ``Signature``, ``pae``, ``keyid_from_public_key``, and
  ``DSSE_PAYLOAD_TYPE`` are imported directly — no reimplemented DSSE
  types.

* **Predicate type is per-protocol, not per-document.**  All volunteer
  documents share one predicate type URL (``VOLUNTEER_DOCUMENT_PREDICATE
  _TYPE``).  The ``document_kind`` field inside the predicate body
  distinguishes candidacy from result-receipt from dispute, so a verifier
  can filter without re-parsing the full payload.

* **Deterministic.**  Same canonical dict → same bytes → same signature.
  Ed25519 is deterministic by spec (RFC 8032 §5.1.6), and
  :func:`canonical_bytes` guarantees the serialisation is byte-stable.

Schema version
--------------

``VOLUNTEER_DOCUMENT_SCHEMA_VERSION`` is bumped when the envelope predicate
field set changes in a backward-incompatible way.  Additive fields that a
legacy verifier can carry-and-ignore do **not** bump the version, because
the field is already inside the predicate body and participates in the
canonical hash.  This follows the convention established by
:mod:`bernstein.core.security.result_receipt_bundle` (``BUNDLE_SCHEMA_VERSION``)
and :mod:`bernstein.core.security.audit_dsse` (``ENVELOPE_SCHEMA_VERSION``).

Future document types should import the constant from here rather than
defining their own, so a single ``VOLUNTEER_DOCUMENT_SCHEMA_VERSION`` is
visible across the entire volunteer protocol surface.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.security.audit_dsse import (
    DSSE_PAYLOAD_TYPE,
    Envelope,
    Signature,
    Statement,
    Subject,
    keyid_from_public_key,
    pae,
    verify_envelope,
)

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Schema version for the volunteer document envelope predicate.  Shared by
#: every document kind inside this protocol — candidacy, result receipt,
#: dispute, etc.  Bump on breaking predicate-shape changes only; additive
#: fields that a legacy verifier can carry-and-ignore do not require a bump
#: (see module docstring).
VOLUNTEER_DOCUMENT_SCHEMA_VERSION: str = "1.0.0"

#: Predicate type for volunteer protocol documents.  A single URL covers
#: all document kinds; the ``document_kind`` field inside the predicate body
#: distinguishes them, so a verifier can filter without re-parsing.
VOLUNTEER_DOCUMENT_PREDICATE_TYPE: str = "https://bernstein.run/attestations/volunteer/v1"


# ---------------------------------------------------------------------------
# Canonical serialisation
# ---------------------------------------------------------------------------


def _sort_keys_recursive(value: Any) -> Any:
    """Recursively reorder dict keys so canonical JSON is byte-stable.

    ``json.dumps(sort_keys=True)`` already sorts top-level keys, but inside
    a free-form predicate we want lexicographic order at every depth so
    repeated canonicalisations of the same input produce identical bytes.
    """
    if isinstance(value, dict):
        return {k: _sort_keys_recursive(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_sort_keys_recursive(v) for v in value]
    return value


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Deterministic JSON bytes: recursively sorted keys, compact separators, UTF-8.

    Matches :func:`audit_dsse._canonical_json`'s discipline and
    :func:`result_receipt_bundle.canonical_bytes` so two serialisations of
    the same dict byte-agree.  This is the property the determinism tests
    assert.

    Args:
        payload: The dictionary to serialise.

    Returns:
        UTF-8 encoded canonical JSON bytes.
    """
    return json.dumps(
        _sort_keys_recursive(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def canonical_hash(payload: dict[str, Any]) -> str:
    """SHA-256 hex digest of :func:`canonical_bytes`.

    Args:
        payload: The dictionary to hash.

    Returns:
        Lowercase hex-encoded SHA-256 digest.
    """
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------


def sign_document(
    document: dict[str, Any],
    *,
    signing_key: Ed25519PrivateKey,
    document_kind: str,
    subject_name: str | None = None,
    keyid: str | None = None,
) -> Envelope:
    """Wrap a canonical document dict in a signed DSSE envelope.

    The caller provides the document body (a canonical-ready dict); this
    function handles the in-toto statement framing, PAE signing, and
    envelope construction.  The result is wire-compatible with every other
    envelope in bernstein (audit, result-receipt, multitenant export).

    Args:
        document: Canonical document dict.  Will be serialised via
            :func:`canonical_bytes` — callers must *not* pre-serialise.
        signing_key: Ed25519 private key used to sign the PAE input.
        document_kind: Discriminator inside the predicate body (e.g.
            ``"candidacy"``, ``"result-receipt"``, ``"dispute"``).  Allows a
            verifier to filter by document kind without re-parsing the full
            payload.
        subject_name: Optional override for the in-toto subject name.
            Defaults to ``"volunteer-{document_kind}"``.
        keyid: Optional override; defaults to
            ``keyid_from_public_key(signing_key.public_key())``.

    Returns:
        A signed :class:`Envelope` ready to be persisted or transmitted.
    """
    doc_bytes = canonical_bytes(document)
    digest = hashlib.sha256(doc_bytes).hexdigest()

    resolved_subject = subject_name or f"volunteer-{document_kind}"
    subject = Subject(name=resolved_subject, digest={"sha256": digest})

    predicate = {
        "schema_version": VOLUNTEER_DOCUMENT_SCHEMA_VERSION,
        "document_kind": document_kind,
        "document": document,
    }
    statement = Statement(
        subjects=[subject],
        predicate_type=VOLUNTEER_DOCUMENT_PREDICATE_TYPE,
        predicate=predicate,
    )

    payload = canonical_bytes(statement.to_dict())
    pae_bytes = pae(DSSE_PAYLOAD_TYPE, payload)
    signature = signing_key.sign(pae_bytes)

    resolved_keyid = keyid or keyid_from_public_key(signing_key.public_key())
    return Envelope(
        payload_type=DSSE_PAYLOAD_TYPE,
        payload_b64=base64.b64encode(payload).decode("ascii"),
        signatures=[
            Signature(
                keyid=resolved_keyid,
                sig=base64.b64encode(signature).decode("ascii"),
            )
        ],
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DocumentVerification:
    """Outcome of :func:`verify_document`.

    Attributes:
        ok: ``True`` iff the envelope signature verified, the predicate
            type matched, and the embedded document re-serialised to the
            attested subject digest.
        document: The embedded document dict (populated even on failure
            when the envelope was parseable, so the caller can inspect the
            payload).
        document_kind: The ``document_kind`` from the predicate body.
        keyid: keyid of the signature that successfully verified (empty on
            failure).
        errors: Human-readable failure messages (empty when ``ok``).
    """

    ok: bool
    document: dict[str, Any] = field(default_factory=dict)
    document_kind: str = ""
    keyid: str = ""
    errors: tuple[str, ...] = ()


def verify_document(
    envelope: Envelope,
    public_key: Ed25519PublicKey,
    *,
    expected_document_kind: str | None = None,
) -> DocumentVerification:
    """Verify a volunteer protocol document envelope.

    Checks, collecting errors:

    1. DSSE signature verifies against ``public_key`` and the predicate
       type is ``VOLUNTEER_DOCUMENT_PREDICATE_TYPE`` (delegated to
       :func:`audit_dsse.verify_envelope`).
    2. The embedded document re-serialises to the attested subject digest
       (internal hash consistency).
    3. Optionally, ``document_kind`` matches ``expected_document_kind``.

    Args:
        envelope: Parsed envelope (typically from
            :func:`audit_dsse.load_envelope`).
        public_key: Ed25519 public key the signer used.
        expected_document_kind: When supplied, the ``document_kind`` field
            must match this value.

    Returns:
        :class:`DocumentVerification` with ``ok`` flag and details.
    """
    errors: list[str] = []

    env_v = verify_envelope(
        envelope,
        public_key,
        expected_predicate_type=VOLUNTEER_DOCUMENT_PREDICATE_TYPE,
    )
    if not env_v.ok:
        return DocumentVerification(ok=False, errors=tuple(env_v.errors))

    statement = env_v.statement
    raw_predicate = statement.get("predicate", {})
    predicate_dict = raw_predicate if isinstance(raw_predicate, dict) else {}
    if not isinstance(raw_predicate, dict):
        return DocumentVerification(
            ok=False,
            errors=(f"predicate is {type(raw_predicate).__name__}, expected dict",),
        )

    raw_document = predicate_dict.get("document", {})
    document = raw_document if isinstance(raw_document, dict) else {}
    if not isinstance(raw_document, dict):
        errors.append(
            f"document is {type(raw_document).__name__}, expected dict",
        )

    # Internal hash consistency: the embedded document must reproduce the
    # subject digest byte-for-byte.
    raw_subject = statement.get("subject", [])
    attested_digest = ""
    if isinstance(raw_subject, list) and raw_subject:
        first_subject = raw_subject[0]
        if isinstance(first_subject, dict):
            digest_dict = first_subject.get("digest", {})
            if isinstance(digest_dict, dict):
                attested_digest = digest_dict.get("sha256", "")

    if document and attested_digest:
        recomputed = hashlib.sha256(canonical_bytes(document)).hexdigest()
        if recomputed != attested_digest:
            errors.append(
                f"embedded document hashes to {recomputed}, envelope attests {attested_digest}",
            )

    # document_kind check.
    document_kind = predicate_dict.get("document_kind", "")
    if expected_document_kind is not None and document_kind != expected_document_kind:
        errors.append(
            f"document_kind is {document_kind!r}, expected {expected_document_kind!r}",
        )

    return DocumentVerification(
        ok=not errors,
        document=document,
        document_kind=document_kind,
        keyid=env_v.keyid,
        errors=tuple(errors),
    )
