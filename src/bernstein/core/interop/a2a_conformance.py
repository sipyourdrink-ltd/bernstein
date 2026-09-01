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

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.interop.a2a_card import (
    CAPABILITY_CARD_TYP,
    CapabilityCard,
    card_public_key_fingerprint,
    verify_capability_card,
)
from bernstein.core.security.agent_card_signer import (
    AGENT_CARD_V1_TYP,
    canonicalize_jcs,
    ed25519_pem_from_jwk,
    verify_detached_jws_over_canonical,
)
from bernstein.core.security.canonical import canonical_bytes

if TYPE_CHECKING:
    from bernstein.core.interop.a2a_card import SignedCapabilityCard

__all__ = [
    "AGENT_CARD_V1_PROTOCOL_VERSION",
    "AGENT_CARD_V1_TYP",
    "ConformanceCheck",
    "ConformanceReport",
    "canonical_report_bytes",
    "check_agent_card_v1_conformance",
    "check_card_conformance",
    "jwk_fingerprint",
    "report_hash",
    "resolve_jwk",
]

#: The A2A v1.0 ``protocolVersion`` string a conformant agent card must carry.
AGENT_CARD_V1_PROTOCOL_VERSION: str = "1.0"


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


# ===========================================================================
# A2A v1.0 agent-card conformance profile (#2525)
# ---------------------------------------------------------------------------
# The self-check above audits Bernstein's own ``a2a-capability+jws`` profile.
# The v1.0 *agent-card* profile is a different artefact: the card served at
# ``/.well-known/agent.json`` carries a ``signatures[]`` array of detached JWS
# objects (RFC 7515 A.5) over the JCS-canonical body with ``signatures``
# stripped (RFC 8785), each resolvable to a JWK at
# ``/.well-known/agent.json/keys``. This suite pins that profile in both
# directions: it proves the card *we* emit verifies the way a peer will check
# it, and it verifies an *inbound* v1.0 card produced by any implementation.
#
# The report is a deterministic projection of ``(card_bytes, jwks_bytes, now)``:
# two operators on two hosts feeding identical inputs obtain a byte-identical
# report (:func:`canonical_report_bytes`), so a third party can anchor the
# report hash and prove which checks ran and what they returned.
# ===========================================================================

#: Fields the v1.0 agent-card body must carry (beyond ``name``/``url``).
_REQUIRED_V1_LIST_FIELDS: tuple[str, ...] = ("supportedInterfaces", "securitySchemes")


def canonical_report_bytes(report: ConformanceReport) -> bytes:
    """Return the deterministic JSON bytes of a report (sorted keys, no spaces).

    Two hosts that run the suite over identical card bytes, JWKS, and ``now``
    produce byte-identical output here regardless of platform, so the bytes can
    be hashed and anchored as the immutable evidence of *which checks ran and
    what they returned* (AC: determinism + verifiability).
    """
    return canonical_bytes(report.to_dict())


def report_hash(report: ConformanceReport) -> str:
    """Return the ``sha256:`` digest over :func:`canonical_report_bytes`."""
    return "sha256:" + hashlib.sha256(canonical_report_bytes(report)).hexdigest()


def jwk_fingerprint(jwk: dict[str, Any]) -> str:
    """Return the ``sha256:`` fingerprint of an OKP Ed25519 JWK's public key.

    Computed over the raw 32-byte public key (the JWK ``x`` coordinate) so it
    matches :func:`bernstein.core.interop.a2a_card.card_public_key_fingerprint`
    for the same key material - the trusted-issuer identifier is stable whether
    the key is presented as PEM or JWK.
    """
    pem = ed25519_pem_from_jwk(jwk)
    return card_public_key_fingerprint(pem)


def resolve_jwk(jwks: dict[str, Any], kid: str) -> dict[str, Any] | None:
    """Return the JWK whose ``kid`` matches, or ``None`` when unresolvable.

    The JWKS served by ``/.well-known/agent.json/keys`` lists the current key
    first and any archived key still inside the rotation grace window after it,
    so a ``kid`` minted before a rotation resolves here for the whole window.
    """
    if not isinstance(jwks, dict):
        return None
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        return None
    for jwk in keys:
        if isinstance(jwk, dict) and jwk.get("kid") == kid:
            return jwk
    return None


def _strip_signatures(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the card body with ``signatures`` removed (the JWS signing input)."""
    return {k: v for k, v in payload.items() if k != "signatures"}


def check_agent_card_v1_conformance(
    payload: dict[str, Any],
    *,
    jwks: dict[str, Any],
    now: float | None = None,
) -> ConformanceReport:
    """Run the full A2A v1.0 agent-card conformance profile over ``payload``.

    Checks performed, in fixed evaluation order (a stable order keeps the
    report deterministic):

    1. ``required_v1_fields`` - ``protocolVersion == "1.0"``, non-empty
       ``name`` and ``url``, and non-empty ``supportedInterfaces`` /
       ``securitySchemes`` lists.
    2. ``signatures_present`` - ``signatures`` is a non-empty list and every
       entry carries non-empty ``kid``, ``alg``, ``typ``, ``jws`` fields.
    3. ``jcs_canonical`` - the body with ``signatures`` stripped
       re-canonicalises under RFC 8785 JCS (the exact bytes each JWS attests).
    4. ``jws_header`` - every ``signatures[].jws`` protected header is
       ``EdDSA`` / ``agent-card+jws`` and its ``kid`` matches the entry.
    5. ``kid_resolves`` - every ``kid`` resolves to an OKP/Ed25519 JWK in the
       supplied JWKS (including any archived key inside the grace window).
    6. ``signature`` - every detached JWS verifies against its resolved key
       over the JCS body.
    7. ``expiry`` - when the card carries a positive ``expiresAt``/``expires_at``
       it is not past relative to ``now`` (a card without one passes).

    The function never raises on malformed input; a parse failure becomes a
    failed check. ``report.ok`` is the AND of every check.

    Args:
        payload: The parsed agent-card JSON (the bytes served at
            ``/.well-known/agent.json``).
        jwks: The parsed JWKS (``{"keys": [...]}``) served at
            ``/.well-known/agent.json/keys``.
        now: Optional current-time override for deterministic replay.

    Returns:
        A :class:`ConformanceReport` whose ``kid`` / ``fingerprint`` describe
        the first signature's resolved key.
    """
    checks: list[ConformanceCheck] = []
    ref_now = time.time() if now is None else now

    if not isinstance(payload, dict):
        checks.append(ConformanceCheck("required_v1_fields", False, "agent card payload must be a JSON object"))
        return ConformanceReport(ok=False, checks=checks)

    # --- 1. Required v1.0 fields ------------------------------------------
    field_failures: list[str] = []
    if str(payload.get("protocolVersion", "")) != AGENT_CARD_V1_PROTOCOL_VERSION:
        field_failures.append(f"protocolVersion must be {AGENT_CARD_V1_PROTOCOL_VERSION!r}")
    for scalar in ("name", "url"):
        if not str(payload.get(scalar, "")):
            field_failures.append(f"missing/empty {scalar!r}")
    for list_field in _REQUIRED_V1_LIST_FIELDS:
        value = payload.get(list_field)
        if not isinstance(value, list) or not value:
            field_failures.append(f"{list_field!r} must be a non-empty list")
    fields_ok = not field_failures
    checks.append(
        ConformanceCheck(
            "required_v1_fields",
            fields_ok,
            "all v1.0 fields present" if fields_ok else "; ".join(field_failures),
        )
    )

    # --- 2. signatures[] present and well-shaped --------------------------
    signatures = payload.get("signatures")
    sig_entries: list[dict[str, Any]] = []
    sigs_ok = True
    sigs_detail = "signatures[] present and well-formed"
    if not isinstance(signatures, list) or not signatures:
        sigs_ok = False
        sigs_detail = "signatures must be a non-empty list"
    else:
        for idx, sig in enumerate(signatures):
            if not isinstance(sig, dict):
                sigs_ok = False
                sigs_detail = f"signatures[{idx}] is not an object"
                break
            missing = [k for k in ("kid", "alg", "typ", "jws") if not str(sig.get(k, ""))]
            if missing:
                sigs_ok = False
                sigs_detail = f"signatures[{idx}] missing/empty {', '.join(missing)}"
                break
            sig_entries.append(sig)
    checks.append(ConformanceCheck("signatures_present", sigs_ok, sigs_detail))

    # --- 3. JCS canonicalisation of the signing body ----------------------
    body = _strip_signatures(payload)
    jcs_ok = True
    jcs_detail = "body (signatures stripped) re-canonicalises under RFC 8785 JCS"
    canonical: bytes = b""
    try:
        canonical = canonicalize_jcs(body)
    except (TypeError, ValueError) as exc:
        jcs_ok = False
        jcs_detail = f"JCS canonicalisation failed: {exc}"
    checks.append(ConformanceCheck("jcs_canonical", jcs_ok, jcs_detail))

    # --- 4. JWS protected-header requirements -----------------------------
    header_ok = True
    header_detail = "every signature header is EdDSA/agent-card+jws with a matching kid"
    if not sig_entries:
        header_ok = False
        header_detail = "no well-formed signatures to check headers for"
    for idx, sig in enumerate(sig_entries):
        parsed = _parse_jws_header(str(sig["jws"]))
        if parsed is None:
            header_ok = False
            header_detail = f"signatures[{idx}] JWS header is malformed or not detached"
            break
        if parsed.get("alg") != "EdDSA":
            header_ok = False
            header_detail = f"signatures[{idx}] alg must be EdDSA, got {parsed.get('alg')!r}"
            break
        if parsed.get("typ") != AGENT_CARD_V1_TYP:
            header_ok = False
            header_detail = f"signatures[{idx}] typ must be {AGENT_CARD_V1_TYP!r}, got {parsed.get('typ')!r}"
            break
        if not parsed.get("kid") or parsed.get("kid") != sig["kid"]:
            header_ok = False
            header_detail = f"signatures[{idx}] header kid does not match the entry kid"
            break
    checks.append(ConformanceCheck("jws_header", header_ok, header_detail))

    # --- 5. kid resolution against the JWKS -------------------------------
    resolved: list[tuple[dict[str, Any], dict[str, Any]]] = []
    kid_ok = True
    kid_detail = "every signature kid resolves to an OKP/Ed25519 JWK"
    if not sig_entries:
        kid_ok = False
        kid_detail = "no well-formed signatures to resolve"
    for idx, sig in enumerate(sig_entries):
        jwk = resolve_jwk(jwks, str(sig["kid"]))
        if jwk is None:
            kid_ok = False
            kid_detail = f"signatures[{idx}] kid {sig['kid']!r} does not resolve in the JWKS"
            break
        try:
            ed25519_pem_from_jwk(jwk)
        except ValueError as exc:
            kid_ok = False
            kid_detail = f"signatures[{idx}] JWK is not a valid OKP/Ed25519 key: {exc}"
            break
        resolved.append((sig, jwk))
    checks.append(ConformanceCheck("kid_resolves", kid_ok, kid_detail))

    # --- 6. Detached JWS signature ----------------------------------------
    sig_ok = jcs_ok and bool(resolved) and len(resolved) == len(sig_entries)
    sig_detail = "every detached JWS verifies against its resolved JWK over the JCS body"
    if not resolved:
        sig_ok = False
        sig_detail = "no resolved signatures to verify"
    elif jcs_ok:
        for idx, (sig, jwk) in enumerate(resolved):
            try:
                public_pem = ed25519_pem_from_jwk(jwk)
            except ValueError as exc:  # pragma: no cover - guarded by kid_resolves
                sig_ok = False
                sig_detail = f"signatures[{idx}] key unusable: {exc}"
                break
            if not verify_detached_jws_over_canonical(
                canonical, str(sig["jws"]), public_pem, expected_typ=AGENT_CARD_V1_TYP
            ):
                sig_ok = False
                sig_detail = f"signatures[{idx}] detached JWS does not verify over the JCS body"
                break
    else:
        sig_ok = False
        sig_detail = "cannot verify signatures: body did not canonicalise"
    checks.append(ConformanceCheck("signature", sig_ok, sig_detail))

    # --- 7. Expiry (optional field) ---------------------------------------
    expiry_ok, expiry_detail = _check_v1_expiry(payload, ref_now)
    checks.append(ConformanceCheck("expiry", expiry_ok, expiry_detail))

    # --- Report metadata --------------------------------------------------
    first_kid = str(sig_entries[0]["kid"]) if sig_entries else ""
    fingerprint = ""
    if resolved:
        try:
            fingerprint = jwk_fingerprint(resolved[0][1])
        except ValueError:  # pragma: no cover - guarded above
            fingerprint = ""

    ok = all(c.passed for c in checks)
    return ConformanceReport(
        ok=ok,
        checks=checks,
        issuer=str(payload.get("name", "")),
        kid=first_kid,
        fingerprint=fingerprint,
    )


def _parse_jws_header(detached_jws: str) -> dict[str, Any] | None:
    """Return the decoded protected header of a detached compact JWS, or None."""
    import base64

    parts = detached_jws.split(".")
    if len(parts) != 3 or parts[1]:
        # Must be detached (empty payload segment).
        return None
    header_b64 = parts[0]
    pad = -len(header_b64) % 4
    try:
        raw = base64.urlsafe_b64decode(header_b64 + ("=" * pad))
        header = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return header if isinstance(header, dict) else None


def _check_v1_expiry(payload: dict[str, Any], ref_now: float) -> tuple[bool, str]:
    """Return the expiry verdict for an optional ``expiresAt``/``expires_at`` field."""
    raw = payload.get("expiresAt", payload.get("expires_at"))
    if raw is None:
        return True, "card carries no expiry (never-expiring cards are discouraged)"
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return False, f"expiry field is not a number: {raw!r}"
    expires_at = float(raw)
    if expires_at <= 0:
        return True, "card carries no expiry (expiresAt <= 0)"
    if ref_now > expires_at:
        return False, f"card is expired (expiresAt={expires_at})"
    return True, f"card is within its validity window (expiresAt={expires_at})"
