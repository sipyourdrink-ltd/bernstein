"""Proof-of-possession binding between a token and an X.509-SVID (issue #5030).

A bearer token is a password with a shorter life: whoever holds the bytes is
the principal, and nothing in the token ties it to the workload it was issued
to. For an agent system that is a sharp edge -- a token leaks through a model's
context, a prompt log, a crash dump, a shared trace, or an artefact the agent
itself wrote, all of them surfaces that are numerous, machine-readable, and
persisted by design.

This module is the certificate half of the fix (RFC 8705 §3): the issuer stamps
the SHA-256 thumbprint of the workload's X.509-SVID leaf into the token's
``cnf`` confirmation claim, and the validator recomputes that thumbprint from
the certificate the caller actually presented. A token replayed from anywhere
that cannot present the same leaf is refused.

The refusal is the artefact. :class:`BindingRefusal` is content-addressed and
names *which* proof failed -- no certificate presented, wrong thumbprint,
expired leaf, unparseable leaf, or an audience that requires a binding the
token does not carry -- together with the SVID that should have been used. The
caller anchors that hash in the HMAC-chained audit log, so a replay is a record
that verifies offline rather than a 401 that leaves no trace.

Two properties are deliberate:

* A token that carries a ``cnf`` claim is *always* checked, whatever the
  deployment configures. The binding lives in the credential, so no flag can
  downgrade an already-bound token back to a bearer token.
* An audience that never opted in is untouched. Binding is opt-in per
  audience, so existing deployments keep working on upgrade.

DPoP (RFC 9449), for clients that hold no certificate, is a separate surface
and is not implemented here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from bernstein.core.identity.spiffe.svid import SvidReference

__all__ = [
    "CNF_CLAIM",
    "CNF_X5T_S256",
    "SVID_ID_CLAIM",
    "BindingRefusal",
    "BindingRefusalCode",
    "confirmation_claim",
    "parse_bound_audiences",
    "verify_token_binding",
    "x5t_s256_from_pem",
    "x5t_s256_from_svid_reference",
]

#: RFC 7800 confirmation claim carrying the proof-of-possession key.
CNF_CLAIM: Final[str] = "cnf"

#: RFC 8705 §3.1 confirmation member: base64url SHA-256 of the leaf DER.
CNF_X5T_S256: Final[str] = "x5t#S256"

#: Claim naming the SPIFFE ID of the SVID the token was bound to.
#:
#: Kept beside ``cnf`` rather than inside it so the confirmation claim stays
#: exactly the RFC 8705 shape an off-the-shelf verifier expects. It is signed
#: with the rest of the payload, so a refusal can name the SVID that should
#: have been presented without trusting anything the caller sent.
SVID_ID_CLAIM: Final[str] = "svid_spiffe_id"

_SHA256_PREFIX: Final[str] = "sha256:"


class BindingRefusalCode(StrEnum):
    """Which proof failed. One value per distinguishable failure."""

    #: The audience requires proof of possession and the token carries none.
    BINDING_REQUIRED = "binding_required"
    #: The token is bound but the caller presented no certificate.
    PROOF_ABSENT = "proof_absent"
    #: A certificate was presented and its thumbprint is not the bound one.
    THUMBPRINT_MISMATCH = "thumbprint_mismatch"
    #: The bound certificate was presented but is outside its validity window.
    BINDING_EXPIRED = "binding_expired"
    #: The presented bytes could not be parsed as an X.509 certificate.
    MALFORMED_CERTIFICATE = "malformed_certificate"
    #: The token's ``cnf`` claim is present but not a usable confirmation.
    MALFORMED_CONFIRMATION = "malformed_confirmation"


@dataclass(frozen=True, slots=True)
class BindingRefusal:
    """A content-addressed record of one refused proof of possession.

    Attributes:
        code: Which check failed.
        audience: The audience the token was minted for (``""`` when none).
        spiffe_id: The SVID the token was bound to, as named by the token.
        expected_thumbprint: The bound ``x5t#S256`` (``""`` when unbound).
        presented_thumbprint: The ``x5t#S256`` of what was presented (``""``
            when nothing parseable was presented).
        session_id: The session the token names, for correlation.
        detail: Human-readable reason, safe to log.
        refused_at: Epoch seconds the refusal was minted.
    """

    code: BindingRefusalCode
    audience: str
    spiffe_id: str
    expected_thumbprint: str
    presented_thumbprint: str
    session_id: str
    detail: str
    refused_at: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict of the refusal.

        Carries identifiers, thumbprints, and the verdict only -- never the
        token, the certificate, or any key material.
        """
        return {
            "refusal_code": self.code.value,
            "audience": self.audience,
            "spiffe_id": self.spiffe_id,
            "expected_thumbprint": self.expected_thumbprint,
            "presented_thumbprint": self.presented_thumbprint,
            "session_id": self.session_id,
            "detail": self.detail,
            "refused_at": self.refused_at,
        }

    def content_hash(self) -> str:
        """Return ``sha256:<hex>`` over the refusal's canonical form.

        The hash is the refusal's identity: it is what the audit chain pins, so
        a verifier holding the record recomputes it and proves the refusal was
        not edited after the fact.
        """
        body = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        return _SHA256_PREFIX + hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------
# Thumbprints
# ---------------------------------------------------------------------------


def _x5t_s256_from_der(der: bytes) -> str:
    """Return the RFC 8705 ``x5t#S256`` of a DER-encoded certificate."""
    digest = hashlib.sha256(der).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def x5t_s256_from_pem(cert_pem: bytes) -> str:
    """Return the ``x5t#S256`` of the leaf certificate in *cert_pem*.

    Raises:
        ValueError: If the leaf certificate cannot be parsed.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate(cert_pem)
    return _x5t_s256_from_der(cert.public_bytes(serialization.Encoding.DER))


def x5t_s256_from_svid_reference(reference: SvidReference) -> str:
    """Return the ``x5t#S256`` for an already-projected SVID reference.

    :class:`~bernstein.core.identity.spiffe.svid.SvidReference` content-addresses
    the leaf as ``sha256:<hex>``; RFC 8705 spells the same digest base64url, so
    this re-encodes rather than re-hashing and the token binds exactly the leaf
    the card and the audit chain already anchor.

    Raises:
        ValueError: If the reference does not carry a ``sha256:<hex>`` handle.
    """
    handle = reference.x509_svid_sha256
    if not handle.startswith(_SHA256_PREFIX):
        msg = f"SVID reference is not sha256 content-addressed: {handle!r}"
        raise ValueError(msg)
    try:
        digest = bytes.fromhex(handle[len(_SHA256_PREFIX) :])
    except ValueError as exc:
        msg = f"SVID reference carries a malformed digest: {handle!r}"
        raise ValueError(msg) from exc
    if len(digest) != hashlib.sha256().digest_size:
        msg = f"SVID reference digest is not SHA-256 sized: {handle!r}"
        raise ValueError(msg)
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def confirmation_claim(thumbprint: str) -> dict[str, str]:
    """Return the RFC 8705 ``cnf`` value for a certificate thumbprint."""
    return {CNF_X5T_S256: thumbprint}


# ---------------------------------------------------------------------------
# Audiences
# ---------------------------------------------------------------------------


def parse_bound_audiences(raw: str) -> frozenset[str]:
    """Parse a comma-separated audience list into a set of trimmed names."""
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _token_audiences(claims: dict[str, Any]) -> tuple[str, ...]:
    """Return the ``aud`` claim as a tuple, accepting the string or list form."""
    aud: Any = claims.get("aud")
    if isinstance(aud, str):
        return (aud,) if aud else ()
    if isinstance(aud, list):
        entries = cast("list[Any]", aud)
        return tuple(str(entry) for entry in entries if str(entry))
    return ()


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _confirmation_thumbprint(claims: dict[str, Any]) -> tuple[str, str | None]:
    """Return ``(thumbprint, error)`` read out of the token's ``cnf`` claim."""
    cnf: Any = claims.get(CNF_CLAIM)
    if cnf is None:
        return "", None
    if not isinstance(cnf, dict):
        return "", "cnf claim is not a confirmation object"
    thumbprint: Any = cast("dict[str, Any]", cnf).get(CNF_X5T_S256, "")
    if not isinstance(thumbprint, str) or not thumbprint:
        return "", f"cnf claim carries no {CNF_X5T_S256} confirmation"
    return thumbprint, None


def verify_token_binding(
    claims: dict[str, Any],
    *,
    presented_cert_pem: bytes | None,
    bound_audiences: Iterable[str] = (),
    clock: Callable[[], float] = time.time,
) -> BindingRefusal | None:
    """Check a token's proof of possession. ``None`` means the token is usable.

    Args:
        claims: The already-signature-verified token claims. Callers must not
            reach here with an unverified payload: the refusal is recorded, and
            recording a refusal for a forged token would let an unauthenticated
            caller write into the audit chain.
        presented_cert_pem: The client certificate the caller presented on this
            request, or ``None`` when the transport supplied none.
        bound_audiences: Audiences that require a proof of possession. An
            audience outside this set is unaffected unless the token itself
            carries a ``cnf`` claim.
        clock: Injectable time source, for tests.

    Returns:
        A :class:`BindingRefusal` naming the failed check, or ``None`` when the
        token may be used on this connection.
    """
    audiences = _token_audiences(claims)
    audience = audiences[0] if audiences else ""
    spiffe_id = str(claims.get(SVID_ID_CLAIM, "") or "")
    session_id = str(claims.get("session_id", "") or "")
    required = bool(set(audiences) & set(bound_audiences))

    def refuse(code: BindingRefusalCode, detail: str, presented: str = "") -> BindingRefusal:
        return BindingRefusal(
            code=code,
            audience=audience,
            spiffe_id=spiffe_id,
            expected_thumbprint=expected,
            presented_thumbprint=presented,
            session_id=session_id,
            detail=detail,
            refused_at=clock(),
        )

    expected, confirmation_error = _confirmation_thumbprint(claims)
    if confirmation_error is not None:
        return refuse(BindingRefusalCode.MALFORMED_CONFIRMATION, confirmation_error)

    if not expected:
        if required:
            return refuse(
                BindingRefusalCode.BINDING_REQUIRED,
                f"audience {audience!r} requires proof of possession and the token carries none",
            )
        return None

    if presented_cert_pem is None:
        return refuse(
            BindingRefusalCode.PROOF_ABSENT,
            "token is bound to a certificate but the request presented none",
        )

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    try:
        cert = x509.load_pem_x509_certificate(presented_cert_pem)
        presented = _x5t_s256_from_der(cert.public_bytes(serialization.Encoding.DER))
    except Exception:
        return refuse(
            BindingRefusalCode.MALFORMED_CERTIFICATE,
            "presented client certificate could not be parsed",
        )

    if not hmac.compare_digest(presented, expected):
        return refuse(
            BindingRefusalCode.THUMBPRINT_MISMATCH,
            "presented certificate is not the one the token was bound to",
            presented=presented,
        )

    if cert.not_valid_after_utc.timestamp() <= clock():
        return refuse(
            BindingRefusalCode.BINDING_EXPIRED,
            "the bound certificate is outside its validity window",
            presented=presented,
        )

    return None
