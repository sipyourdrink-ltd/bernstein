"""COSE_Sign1 (RFC 9052) wire primitives, shared by every receipt family.

Why this module exists
----------------------
The audit receipt has emitted COSE_Sign1 since it was written
(:mod:`bernstein.core.security.audit_receipt`), and the run receipt needs the
same envelope over different bytes (#6207). The encoding is fixed by RFC 9052,
so there is one correct way to produce it and no reason for a second copy to
drift: a receipt family that hand-rolled its own ``Sig_structure`` would still
verify against itself and fail against everyone else.

What callers choose, and what they do not
-----------------------------------------
A caller supplies the **payload**, the **content type** that domain-separates
its envelope from the others the same key signs, the **key id** a verifier
reads out of the protected header, and the signer. Everything else - the tag,
the algorithm, the header labels, the ``Sig_structure`` layout, and the
canonical CBOR mode - is the standard, and is not negotiable here.

Determinism
-----------
``cbor2`` deterministic mode (``canonical=True``) for every encode, and RFC
8032 Ed25519 for the signature. For a fixed payload and key the envelope bytes
are byte-identical across independent runs, which is what lets a ``.cose`` file
be committed as a format vector. No wall-clock value enters the bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import cbor2

if TYPE_CHECKING:
    from bernstein.core.security.lineage_kms import KMSAdapter

__all__ = [
    "COSE_ALG_EDDSA",
    "COSE_LABEL_ALG",
    "COSE_LABEL_CONTENT_TYPE",
    "COSE_LABEL_KID",
    "COSE_SIGN1_TAG",
    "CoseError",
    "CoseSign1",
    "build_cose_sign1_bytes",
    "cose_sign1_signing_input",
    "decode_cose_sign1",
]


class CoseError(ValueError):
    """The bytes handed in are not a COSE_Sign1 this project would have made."""


#: COSE_Sign1 CBOR tag (RFC 9052 section 2).
COSE_SIGN1_TAG: int = 18

#: COSE ``alg`` header value for EdDSA (RFC 9053 / IANA COSE Algorithms).
COSE_ALG_EDDSA: int = -8

#: COSE header labels (RFC 9052 section 3.1).
COSE_LABEL_ALG: int = 1
COSE_LABEL_CONTENT_TYPE: int = 3
COSE_LABEL_KID: int = 4

#: RFC 9052 section 4.4 context string for a single-signer envelope.
_SIG_CONTEXT: str = "Signature1"


def cose_sign1_signing_input(protected_bstr: bytes, payload: bytes) -> bytes:
    """The ``Sig_structure`` bytes a COSE_Sign1 signature is computed over.

    RFC 9052 section 4.4. Exposed because the verifier must rebuild this from
    the *decoded* envelope rather than from anything the producer kept, so that
    a mismatch between what was signed and what shipped is detectable.
    """
    return cbor2.dumps([_SIG_CONTEXT, protected_bstr, b"", payload], canonical=True)


def build_cose_sign1_bytes(
    *,
    payload: bytes,
    content_type: str,
    key_id: str,
    kms_adapter: KMSAdapter,
) -> bytes:
    """Build a COSE_Sign1 envelope with an embedded payload.

    Args:
        payload: The bytes to sign, embedded in the envelope so the file is
            self-contained and can be registered with a transparency service
            on its own.
        content_type: Advertised in the protected header. Domain-separates
            this envelope from every other one the same key signs.
        key_id: Written to the ``kid`` header for a verifier to key on.
        kms_adapter: Ed25519 signer (:class:`~bernstein.core.security.lineage_kms.KMSAdapter`).

    Returns:
        The canonical CBOR encoding of the tagged COSE_Sign1 structure.
    """
    protected_map: dict[int, Any] = {
        COSE_LABEL_ALG: COSE_ALG_EDDSA,
        COSE_LABEL_CONTENT_TYPE: content_type,
        COSE_LABEL_KID: key_id.encode("utf-8"),
    }
    protected_bstr = cbor2.dumps(protected_map, canonical=True)
    signature = kms_adapter.sign(cose_sign1_signing_input(protected_bstr, payload))
    envelope = cbor2.CBORTag(
        COSE_SIGN1_TAG,
        [protected_bstr, {}, payload, signature],
    )
    return cbor2.dumps(envelope, canonical=True)


@dataclass(frozen=True)
class CoseSign1:
    """A decoded COSE_Sign1, with the fields a verifier needs.

    ``protected_bstr`` is kept as the original byte string rather than the
    decoded map: the signature covers those exact bytes, so re-encoding the map
    to check the signature would be a different (and possibly equal-looking)
    input.
    """

    protected_bstr: bytes
    payload: bytes
    signature: bytes
    alg: int | None
    content_type: str | None
    key_id: str | None

    @property
    def signing_input(self) -> bytes:
        """The bytes this envelope's signature must verify against."""
        return cose_sign1_signing_input(self.protected_bstr, self.payload)


def decode_cose_sign1(cose_bytes: bytes) -> CoseSign1:
    """Decode a tagged COSE_Sign1 envelope.

    Raises:
        CoseError: The input is not CBOR, not tag 18, not a 4-element array,
            carries a detached (``nil``) payload, or has an unreadable
            protected header. Every one of these is a malformed input rather
            than a verification failure, and the caller is expected to report
            it as such.
    """
    try:
        tagged = cbor2.loads(cose_bytes)
    except Exception as exc:  # cbor2 raises several unrelated types
        raise CoseError(f"not valid CBOR: {exc}") from exc

    if not isinstance(tagged, cbor2.CBORTag):
        raise CoseError(f"not a tagged CBOR value (got {type(tagged).__name__})")
    if tagged.tag != COSE_SIGN1_TAG:
        raise CoseError(f"not a COSE_Sign1: expected tag {COSE_SIGN1_TAG}, got {tagged.tag}")
    # cbor2 decodes a CBOR array to a tuple, not a list (and the unprotected
    # map to a frozendict), so accept either rather than the type we happened
    # to encode from.
    if not isinstance(tagged.value, (list, tuple)) or len(tagged.value) != 4:
        raise CoseError("COSE_Sign1 must be a 4-element array")

    protected_bstr, _unprotected, payload, signature = tagged.value
    if not isinstance(protected_bstr, bytes):
        raise CoseError("protected header must be a byte string")
    if payload is None:
        raise CoseError("detached payload (nil) - this project embeds the payload")
    if not isinstance(payload, bytes) or not isinstance(signature, bytes):
        raise CoseError("payload and signature must be byte strings")

    alg: int | None = None
    content_type: str | None = None
    key_id: str | None = None
    if protected_bstr:
        try:
            protected = cbor2.loads(protected_bstr)
        except Exception as exc:
            raise CoseError(f"unreadable protected header: {exc}") from exc
        if not isinstance(protected, dict):
            raise CoseError("protected header must decode to a map")
        raw_alg = protected.get(COSE_LABEL_ALG)
        alg = raw_alg if isinstance(raw_alg, int) else None
        raw_ct = protected.get(COSE_LABEL_CONTENT_TYPE)
        content_type = raw_ct if isinstance(raw_ct, str) else None
        raw_kid = protected.get(COSE_LABEL_KID)
        if isinstance(raw_kid, bytes):
            key_id = raw_kid.decode("utf-8", errors="replace")
        elif isinstance(raw_kid, str):
            key_id = raw_kid

    return CoseSign1(
        protected_bstr=protected_bstr,
        payload=payload,
        signature=signature,
        alg=alg,
        content_type=content_type,
        key_id=key_id,
    )
