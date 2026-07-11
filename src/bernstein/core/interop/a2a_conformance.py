"""A2A signed-card conformance self-check.

An operator who issues a capability card needs to prove, offline, that
the card they emit round-trips and verifies the same way a peer will
check it: canonicalise the body (RFC 8785 JCS), verify the detached JWS
(RFC 7515) with the Ed25519 key the card carries, and confirm the
required fields, expiry, and issuer are well-formed.

This is a self-contained audit of the artefact we already ship. It leans
on the Ed25519-signed identity lever: every check below is meaningful
only because the card is signed with an Ed25519 key whose public half
travels in the card, so a verifier can reproduce the signing input and
authenticate it without a side channel. Strip the signature and the
conformance report degrades to a schema lint; the signature check is the
point.

The self-check is read-only and deterministic: given the same signed
card and the same ``now`` it always returns the same verdict. It reuses
the primitives in :mod:`bernstein.core.interop.a2a_card` rather than
re-implementing verification, so a card that passes here passes for the
same reasons a peer's ``bernstein interop a2a verify`` accepts it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.interop.a2a_card import (
    CAPABILITY_CARD_TYP,
    CapabilityCard,
    card_public_key_fingerprint,
    verify_capability_card,
)

if TYPE_CHECKING:
    from bernstein.core.interop.a2a_card import SignedCapabilityCard

__all__ = [
    "ConformanceCheck",
    "ConformanceReport",
    "check_card_conformance",
]


@dataclass(frozen=True)
class ConformanceCheck:
    """A single named conformance check with a pass/fail verdict.

    Attributes:
        name: Stable check id (for instance ``"jcs_canonical"``).
        passed: Whether the check passed.
        detail: Human-readable explanation of the verdict.
    """

    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of the check."""
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class ConformanceReport:
    """Aggregated result of a card conformance self-check.

    Attributes:
        ok: True iff every check passed.
        checks: The individual checks, in evaluation order.
        issuer: Issuer id from the card (``""`` if unreadable).
        kid: Key id from the card (``""`` if unreadable).
        fingerprint: ``sha256:`` fingerprint of the card key (``""`` if
            the key could not be parsed).
    """

    ok: bool
    checks: list[ConformanceCheck] = field(default_factory=list)
    issuer: str = ""
    kid: str = ""
    fingerprint: str = ""

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of the report."""
        return {
            "ok": self.ok,
            "issuer": self.issuer,
            "kid": self.kid,
            "fingerprint": self.fingerprint,
            "checks": [c.to_dict() for c in self.checks],
        }


# Fields a conformant card body must carry as non-empty strings.
_REQUIRED_STRING_FIELDS: tuple[str, ...] = (
    "schema_version",
    "issuer",
    "name",
    "public_key_pem",
    "kid",
)


def check_card_conformance(
    signed: SignedCapabilityCard,
    *,
    now: float | None = None,
) -> ConformanceReport:
    """Run the offline conformance self-check over a signed capability card.

    Checks performed, in order:

    1. ``required_fields`` - the body carries the mandatory non-empty
       fields and a well-formed policy block (via
       :meth:`CapabilityCard.validate_body`).
    2. ``jcs_canonical`` - the body re-canonicalises under RFC 8785 JCS
       without error (the exact bytes the signature is computed over).
    3. ``signature`` - the detached JWS verifies against the Ed25519 key
       the card carries, with the capability-card ``typ`` and ``EdDSA``
       alg, ignoring expiry (expiry is a separate check).
    4. ``expiry`` - the card is not expired relative to ``now``.
    5. ``issuer`` - the issuer id is a non-empty string.

    The report ``ok`` is the AND of every check. The function never
    raises on malformed input; a parse failure becomes a failed check.

    Args:
        signed: The signed capability card to audit.
        now: Optional override for the current time (testing / replay).

    Returns:
        A :class:`ConformanceReport`.
    """
    checks: list[ConformanceCheck] = []

    # --- 1. Required fields + policy block --------------------------------
    body = signed.card.to_body()
    fields_ok = True
    fields_detail = "all required fields present and well-formed"
    try:
        CapabilityCard.validate_body(body)
        missing = [f for f in _REQUIRED_STRING_FIELDS if not str(body.get(f, ""))]
        if missing:
            fields_ok = False
            fields_detail = f"missing/empty required field(s): {', '.join(missing)}"
    except ValueError as exc:
        fields_ok = False
        fields_detail = f"card body failed validation: {exc}"
    checks.append(ConformanceCheck("required_fields", fields_ok, fields_detail))

    # --- 2. JCS canonicalisation ------------------------------------------
    jcs_ok = True
    jcs_detail = "body re-canonicalises under RFC 8785 JCS"
    try:
        from bernstein.core.security.agent_card_signer import canonicalize_jcs

        canonicalize_jcs(body)
    except (TypeError, ValueError) as exc:
        jcs_ok = False
        jcs_detail = f"JCS canonicalisation failed: {exc}"
    checks.append(ConformanceCheck("jcs_canonical", jcs_ok, jcs_detail))

    # --- 3. Detached JWS signature (expiry excluded) ----------------------
    sig_ok = verify_capability_card(signed, check_expiry=False, now=now)
    sig_detail = (
        f"detached Ed25519 JWS verifies ({CAPABILITY_CARD_TYP})"
        if sig_ok
        else "detached JWS signature is invalid, malformed, or not Ed25519/capability-card typ"
    )
    checks.append(ConformanceCheck("signature", sig_ok, sig_detail))

    # --- 4. Expiry --------------------------------------------------------
    expired = signed.card.is_expired(now=now)
    if signed.card.expires_at <= 0:
        expiry_ok = True
        expiry_detail = "card carries no expiry (expires_at <= 0); never-expiring cards are discouraged"
    else:
        expiry_ok = not expired
        expiry_detail = (
            f"card is within its validity window (expires_at={signed.card.expires_at})"
            if expiry_ok
            else f"card is expired (expires_at={signed.card.expires_at})"
        )
    checks.append(ConformanceCheck("expiry", expiry_ok, expiry_detail))

    # --- 5. Issuer --------------------------------------------------------
    issuer = str(body.get("issuer", ""))
    issuer_ok = bool(issuer)
    issuer_detail = f"issuer='{issuer}'" if issuer_ok else "issuer is empty"
    checks.append(ConformanceCheck("issuer", issuer_ok, issuer_detail))

    # --- Fingerprint (best-effort, informational) -------------------------
    fingerprint = ""
    try:
        fingerprint = card_public_key_fingerprint(signed.card.public_key_pem)
    except (ValueError, TypeError):
        fingerprint = ""

    ok = all(c.passed for c in checks)
    return ConformanceReport(
        ok=ok,
        checks=checks,
        issuer=issuer,
        kid=str(body.get("kid", "")),
        fingerprint=fingerprint,
    )
