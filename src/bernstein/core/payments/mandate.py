"""``SpendMandate`` - an operator-issued, Ed25519-signed spending authorization.

A mandate is a bounded authority for outbound value transfer. Bernstein never
moves money; a mandate is the *authorization record* an agent presents when it
transacts against an external rail, and every attempt under it (allowed or
refused) becomes a chain-anchored receipt (see :mod:`.receipt`).

Signing reuses the Ed25519 keypair the install already holds for agent-card
federation (the keystore in :mod:`bernstein.core.security.agent_card_keystore`);
no new key surface is introduced. The signature is a detached JWS (RFC 7515 §A.5
+ RFC 7797, EdDSA) over the JCS-canonical mandate body, produced by the same
:func:`bernstein.core.lineage.identity.sign_detached` primitive that signs
lineage entries, so a mandate and the receipts it authorizes are signed by one
consistent operator identity.

Presence mode (the structural distinction)
-------------------------------------------
A mandate is issued in one of two presence modes, and the mode changes *what the
signature covers*:

* ``human_present`` -- the operator signs a **concrete transaction envelope**:
  an exact amount and an exact recipient. There is no separate per-transaction
  cap (the amount is the cap), and cumulative spend under the mandate cannot
  exceed that one concrete amount. The operator was in the loop for this exact
  transaction.
* ``delegated`` -- the operator pre-signs a **bounded envelope** the agent then
  transacts under: a maximum amount, an expiry, and optionally a per-transaction
  cap and an allowed-category set. The agent may make several transactions as
  long as their cumulative total stays inside the bound.

The mode is a signed field, so flipping it after issuance invalidates the
signature. Every receipt records which mode authorized it.

Money and text encoding
------------------------
Amounts are carried as string-encoded integer nano-units and text fields must be
NFC; both go through :mod:`bernstein.core.payments._canonical` so no float ever
enters the signed body and a non-NFC recipient/category is rejected, never
normalized.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from bernstein.core.lineage.identity import AgentCard, sign_detached, verify_detached
from bernstein.core.payments._canonical import (
    require_nfc,
    to_nano_units,
    validate_currency,
)
from bernstein.core.security.agent_card_signer import canonicalize_jcs

__all__ = [
    "MANDATE_JWS_TYP",
    "PresenceMode",
    "SpendMandate",
    "mandate_kid",
]

#: Mandate schema version. Bump only on a wire-format change.
MANDATE_VERSION: int = 1

#: ``typ`` recorded conceptually for the detached JWS over a mandate body. The
#: detached-JWS primitive we reuse does not stamp ``typ`` (it carries ``alg``,
#: ``kid``, ``b64``); this constant documents the intended payload class for a
#: reader and is used by the pass-through adapter's envelope metadata.
MANDATE_JWS_TYP: str = "spend-mandate+jws"


class PresenceMode(StrEnum):
    """Whether the operator signed a concrete transaction or a bounded envelope."""

    #: Operator signed an exact amount + recipient (in the loop for this tx).
    HUMAN_PRESENT = "human_present"
    #: Operator pre-signed a bounded envelope the agent transacts under.
    DELEGATED = "delegated"


def mandate_kid(public_key_pem: bytes | str) -> str:
    """Return a stable key id for a mandate signing key.

    Derived from the public key so it is deterministic and self-describing: a
    verifier resolving a mandate's ``kid`` can confirm it matches the embedded
    public key. Mirrors the ``kid`` shape the agent-card signer uses (a stable
    per-key identifier), scoped with a ``mandate-`` prefix.
    """
    raw = public_key_pem.encode("utf-8") if isinstance(public_key_pem, str) else public_key_pem
    return "mandate-" + hashlib.sha256(raw).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class SpendMandate:
    """An Ed25519-signed, content-addressed spending authorization.

    Instances are immutable. Construct via :meth:`issue` (which encodes amounts,
    guards text, and signs) rather than the raw constructor; the constructor
    fields are the on-disk wire shape used by :meth:`from_dict`.
    """

    v: int
    presence_mode: str
    max_amount_nanos: str
    currency: str
    recipient: str
    not_after: int
    issued_at: int
    nonce: str
    kid: str
    public_key_pem: str
    per_tx_cap_nanos: str | None = None
    allowed_categories: tuple[str, ...] | None = None
    signature: str = ""

    # -- construction -------------------------------------------------------

    @classmethod
    def issue(
        cls,
        *,
        private_key_pem: str,
        public_key_pem: str,
        kid: str,
        presence_mode: PresenceMode | str,
        max_amount: str,
        currency: str,
        recipient: str,
        not_after: int,
        issued_at: int,
        nonce: str,
        per_tx_cap: str | None = None,
        allowed_categories: tuple[str, ...] | None = None,
    ) -> SpendMandate:
        """Encode, validate, and sign a mandate.

        Args:
            private_key_pem: Ed25519 PKCS#8 PEM used to sign (operator keystore).
            public_key_pem: SPKI PEM embedded in the mandate for offline verify.
            kid: Key id for the JWS header; use :func:`mandate_kid`.
            presence_mode: ``human_present`` or ``delegated``.
            max_amount: Cap as a decimal string in major units; for
                ``human_present`` this is the exact authorized amount.
            currency: ISO-4217-style uppercase code.
            recipient: Opaque payee id (must be NFC).
            not_after: Unix-seconds expiry.
            issued_at: Unix-seconds issuance time.
            nonce: Uniqueness nonce so two identical-scope mandates differ.
            per_tx_cap: Optional per-transaction cap (``delegated`` only).
            allowed_categories: Optional signed set of permitted category labels.

        Raises:
            ValueError: On an unknown presence mode, non-NFC text, a bad
                currency, or a ``human_present`` mandate carrying a per-tx cap.
        """
        mode = presence_mode.value if isinstance(presence_mode, PresenceMode) else str(presence_mode)
        if mode not in {m.value for m in PresenceMode}:
            raise ValueError(f"unknown presence_mode: {mode!r}")

        if mode == PresenceMode.HUMAN_PRESENT.value and per_tx_cap is not None:
            raise ValueError("human_present mandate binds a concrete amount and must not carry a per_tx_cap")

        currency = validate_currency(currency)
        recipient = require_nfc(recipient, field="recipient")
        max_amount_nanos = to_nano_units(max_amount)
        per_tx_cap_nanos = to_nano_units(per_tx_cap) if per_tx_cap is not None else None
        cats: tuple[str, ...] | None = None
        if allowed_categories is not None:
            cats = tuple(require_nfc(c, field="allowed_category") for c in allowed_categories)

        unsigned = cls(
            v=MANDATE_VERSION,
            presence_mode=mode,
            max_amount_nanos=max_amount_nanos,
            currency=currency,
            recipient=recipient,
            not_after=int(not_after),
            issued_at=int(issued_at),
            nonce=str(nonce),
            kid=kid,
            public_key_pem=public_key_pem,
            per_tx_cap_nanos=per_tx_cap_nanos,
            allowed_categories=cats,
            signature="",
        )
        sig = sign_detached(unsigned._signing_bytes(), private_key_pem, kid=kid)
        return unsigned._with_signature(sig)

    def _with_signature(self, signature: str) -> SpendMandate:
        return SpendMandate(
            v=self.v,
            presence_mode=self.presence_mode,
            max_amount_nanos=self.max_amount_nanos,
            currency=self.currency,
            recipient=self.recipient,
            not_after=self.not_after,
            issued_at=self.issued_at,
            nonce=self.nonce,
            kid=self.kid,
            public_key_pem=self.public_key_pem,
            per_tx_cap_nanos=self.per_tx_cap_nanos,
            allowed_categories=self.allowed_categories,
            signature=signature,
        )

    # -- canonical forms ----------------------------------------------------

    def _signing_body(self) -> dict[str, object]:
        """Return the dict the signature covers (everything except ``signature``).

        The signed body includes ``presence_mode``, the full scope, ``kid`` and
        ``public_key_pem`` -- so the signature binds the key to the mandate and
        flipping any of them (including the presence mode) breaks verification.
        Optional fields are dropped when unset so an omitted ``per_tx_cap`` /
        ``allowed_categories`` canonicalises identically regardless of how the
        instance was reconstructed.
        """
        body: dict[str, object] = {
            "v": self.v,
            "presence_mode": self.presence_mode,
            "max_amount_nanos": self.max_amount_nanos,
            "currency": self.currency,
            "recipient": self.recipient,
            "not_after": self.not_after,
            "issued_at": self.issued_at,
            "nonce": self.nonce,
            "kid": self.kid,
            "public_key_pem": self.public_key_pem,
        }
        if self.per_tx_cap_nanos is not None:
            body["per_tx_cap_nanos"] = self.per_tx_cap_nanos
        if self.allowed_categories is not None:
            body["allowed_categories"] = list(self.allowed_categories)
        return body

    def _signing_bytes(self) -> bytes:
        return canonicalize_jcs(self._signing_body())

    def to_dict(self) -> dict[str, object]:
        """Return the full wire dict (signed body + ``signature``)."""
        return self._signing_body() | {"signature": self.signature}

    @classmethod
    def from_dict(cls, row: dict[str, object]) -> SpendMandate:
        """Reconstruct a mandate from its wire dict (inverse of :meth:`to_dict`)."""
        cats_raw = row.get("allowed_categories")
        cats = tuple(str(c) for c in cats_raw) if isinstance(cats_raw, list) else None
        ptc = row.get("per_tx_cap_nanos")
        return cls(
            v=int(str(row["v"])),
            presence_mode=str(row["presence_mode"]),
            max_amount_nanos=str(row["max_amount_nanos"]),
            currency=str(row["currency"]),
            recipient=str(row["recipient"]),
            not_after=int(str(row["not_after"])),
            issued_at=int(str(row["issued_at"])),
            nonce=str(row["nonce"]),
            kid=str(row["kid"]),
            public_key_pem=str(row["public_key_pem"]),
            per_tx_cap_nanos=None if ptc is None else str(ptc),
            allowed_categories=cats,
            signature=str(row.get("signature", "")),
        )

    def mandate_hash(self) -> str:
        """Return the ``sha256:``-prefixed content address of the signed mandate.

        Hashes the full wire dict (including the signature), so the identity of a
        mandate is the identity of exactly the signed bytes an agent presents.
        """
        return "sha256:" + hashlib.sha256(canonicalize_jcs(self.to_dict())).hexdigest()

    # -- verification -------------------------------------------------------

    def verify_signature(self) -> bool:
        """Return ``True`` iff the embedded signature verifies over the body.

        Offline: uses the mandate's own embedded public key and kid. A verifier
        that additionally wants to trust *whose* key this is checks the kid /
        public key against the operator's keystore out of band.
        """
        if not self.signature:
            return False
        card = AgentCard(agent_id="operator", kid=self.kid, public_key_pem=self.public_key_pem)
        return verify_detached(self._signing_bytes(), self.signature, card)
