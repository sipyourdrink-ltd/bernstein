"""Card-to-SVID binding as a verifiable, chain-anchored receipt (issue #2363).

When a SPIRE agent issues an SVID for a Bernstein workload, the mapping between
the platform identity (the SVID) and the card identity (the agent card) must
itself be verifiable after the fact. :func:`bind_svid_to_card` makes that
mapping a *receipt*: it re-derives the SPIFFE ID deterministically from the
install identity, refuses any SVID whose ID disagrees, stamps the reference onto
the card, and appends a ``spiffe.svid_binding`` event into the HMAC-chained
audit log. The event carries the binding's content hash, so a verifier holding
the chain and the install public key can prove offline -- via
:func:`verify_binding` and :func:`verify_binding_against_event` -- that a card
was bound to exactly this SVID and that neither has been altered since.

Strip the audit chain and the binding loses its meaning, not just its log: the
receipt is the artefact, and its identity is the content hash the chain pins.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.identity.spiffe.spiffe_id import (
    SpiffeIdError,
    derive_spiffe_id_from_key,
    install_segment,
)
from bernstein.core.identity.spiffe.svid import SvidReference

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.core.security.agent_identity import AgentIdentityCard
    from bernstein.core.security.audit import AuditEvent
    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = [
    "BindingError",
    "SvidBinding",
    "bind_svid_to_card",
    "verify_binding",
    "verify_binding_against_event",
]


class BindingError(ValueError):
    """Raised when an SVID cannot be bound to a card (identity mismatch)."""


@dataclass(frozen=True, slots=True)
class SvidBinding:
    """A verifiable record binding an agent card to an X.509-SVID.

    Attributes:
        agent_id: The bound agent card's id.
        spiffe_id: The SPIFFE ID both the card and the SVID carry.
        install_id: The install fingerprint segment the SPIFFE ID derives from.
        card_hash: The card's ``card_hash`` at binding time.
        trust_domain: The trust domain the SPIFFE ID lives in.
        svid_reference: The private-key-free SVID projection.
        bound_at: Epoch seconds the binding was minted.
        prev_chain_digest: The audit-chain head captured when the binding event
            was appended; ``""`` before the binding is recorded.
    """

    agent_id: str
    spiffe_id: str
    install_id: str
    card_hash: str
    trust_domain: str
    svid_reference: SvidReference
    bound_at: float
    prev_chain_digest: str = field(default="")

    def _identity_payload(self) -> dict[str, Any]:
        """Return the canonical, chain-state-free identity of the binding."""
        return {
            "agent_id": self.agent_id,
            "spiffe_id": self.spiffe_id,
            "install_id": self.install_id,
            "card_hash": self.card_hash,
            "trust_domain": self.trust_domain,
            "svid_reference": self.svid_reference.to_dict(),
            "bound_at": self.bound_at,
        }

    def content_hash(self) -> str:
        """Return ``sha256:<hex>`` over the binding's canonical identity.

        Excludes :attr:`prev_chain_digest` (chain state, not identity) so the
        hash is a stable function of the binding itself. Any change to the
        SPIFFE ID, install fingerprint, card hash, or SVID reference changes
        this value, which the audit event pins.
        """
        body = json.dumps(self._identity_payload(), sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(body).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict of the binding."""
        payload = self._identity_payload()
        payload["prev_chain_digest"] = self.prev_chain_digest
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SvidBinding:
        """Rebuild a :class:`SvidBinding` from its :meth:`to_dict` form."""
        return cls(
            agent_id=str(data["agent_id"]),
            spiffe_id=str(data["spiffe_id"]),
            install_id=str(data["install_id"]),
            card_hash=str(data["card_hash"]),
            trust_domain=str(data["trust_domain"]),
            svid_reference=SvidReference.from_dict(data["svid_reference"]),
            bound_at=float(data["bound_at"]),
            prev_chain_digest=str(data.get("prev_chain_digest", "")),
        )


def bind_svid_to_card(
    *,
    card: AgentIdentityCard,
    svid_reference: SvidReference,
    install_public_key_pem: bytes,
    trust_domain: str,
    chain: AuditChainStore,
    actor: str = "workload_identity",
    clock: Callable[[], float] = time.time,
) -> tuple[AgentIdentityCard, SvidBinding, AuditEvent]:
    """Bind *svid_reference* to *card* and anchor the binding in *chain*.

    Deterministically re-derives the SPIFFE ID from the install public key and
    the card's agent id; refuses the binding if the SVID's SPIFFE ID disagrees.
    On success, returns a copy of the card carrying the SPIFFE ID, the
    :class:`SvidBinding` receipt, and the appended audit event.

    Raises:
        BindingError: If the SVID's SPIFFE ID does not equal the ID derived
            from the install identity and the card.
    """
    from bernstein.core.security.audit_chain import record_spiffe_svid_binding

    try:
        expected = derive_spiffe_id_from_key(
            trust_domain=trust_domain,
            install_public_key_pem=install_public_key_pem,
            agent_id=card.agent_id,
        )
    except SpiffeIdError as exc:
        raise BindingError(f"cannot derive SPIFFE ID for card {card.agent_id!r}: {exc}") from exc

    if svid_reference.spiffe_id != expected:
        raise BindingError(
            "SVID SPIFFE ID does not match the ID derived from the install "
            f"identity and card (expected {expected!r}, got {svid_reference.spiffe_id!r})"
        )

    binding = SvidBinding(
        agent_id=card.agent_id,
        spiffe_id=expected,
        install_id=install_segment(install_public_key_pem),
        card_hash=card.card_hash,
        trust_domain=trust_domain,
        svid_reference=svid_reference,
        bound_at=clock(),
    )
    event = record_spiffe_svid_binding(
        chain=chain,
        agent_id=binding.agent_id,
        spiffe_id=binding.spiffe_id,
        install_id=binding.install_id,
        card_hash=binding.card_hash,
        svid_sha256=svid_reference.x509_svid_sha256,
        binding_hash=binding.content_hash(),
        trust_domain=trust_domain,
        actor=actor,
    )
    finalized = dataclasses.replace(binding, prev_chain_digest=str(event.details.get("prev_chain_digest", "")))
    updated_card = dataclasses.replace(card, svid_reference=expected)
    return updated_card, finalized, event


def verify_binding(*, binding: SvidBinding, install_public_key_pem: bytes, trust_domain: str) -> tuple[bool, str]:
    """Re-derive the SPIFFE ID and confirm the binding is internally consistent.

    Returns ``(ok, reason)``. The binding is valid only if the SPIFFE ID
    re-derived from the install public key and the bound agent id equals both
    the binding's ``spiffe_id`` and its SVID reference's ``spiffe_id``, and the
    install fingerprint matches.
    """
    try:
        expected = derive_spiffe_id_from_key(
            trust_domain=trust_domain,
            install_public_key_pem=install_public_key_pem,
            agent_id=binding.agent_id,
        )
    except SpiffeIdError as exc:
        return False, f"derivation failed: {exc}"

    if binding.install_id != install_segment(install_public_key_pem):
        return False, "install fingerprint does not match the supplied install key"
    if binding.spiffe_id != expected:
        return False, f"binding SPIFFE ID {binding.spiffe_id!r} != derived {expected!r}"
    if binding.svid_reference.spiffe_id != expected:
        return False, "SVID reference SPIFFE ID does not match the derived ID"
    return True, "ok"


def verify_binding_against_event(binding: SvidBinding, event: AuditEvent) -> tuple[bool, str]:
    """Confirm *binding* matches the ``spiffe.svid_binding`` *event* that pinned it.

    Returns ``(ok, reason)``. Recomputes the binding's content hash and checks
    it against the hash the chain recorded, so any post-hoc tamper to the
    binding (a swapped SPIFFE ID, install fingerprint, card hash, or SVID
    reference) is detected offline from the chain alone.
    """
    from bernstein.core.security.audit_chain import EVENT_SPIFFE_SVID_BINDING

    if event.event_type != EVENT_SPIFFE_SVID_BINDING:
        return False, f"event is {event.event_type!r}, not {EVENT_SPIFFE_SVID_BINDING!r}"
    recorded_hash = str(event.details.get("binding_hash", ""))
    if binding.content_hash() != recorded_hash:
        return False, "binding content hash does not match the chained receipt"
    if str(event.details.get("spiffe_id", "")) != binding.spiffe_id:
        return False, "event SPIFFE ID does not match the binding"
    if str(event.details.get("install_id", "")) != binding.install_id:
        return False, "event install fingerprint does not match the binding"
    return True, "ok"
