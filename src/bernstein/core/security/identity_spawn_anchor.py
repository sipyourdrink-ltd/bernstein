"""Bind one verified signed agent identity to one Bernstein run."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, cast

from bernstein.core.security.agent_card_signer import (
    AgentCardSignature,
    canonicalize_jcs,
    verify_agent_card,
)
from bernstein.core.security.audit_chain import (
    EVENT_IDENTITY_SPAWN_ATTESTATION,
    AuditChainStore,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from bernstein.core.security.agent_identity import AgentIdentityCard


class IdentitySpawnAnchorError(RuntimeError):
    """Raised when a run identity cannot be anchored or reconstructed."""


def _jws_kid(detached_jws: str) -> str:
    try:
        protected, payload, _signature = detached_jws.split(".")
        if payload:
            raise ValueError
        padded = protected + "=" * (-len(protected) % 4)
        header = cast(object, json.loads(base64.urlsafe_b64decode(padded)))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdentitySpawnAnchorError("invalid detached agent-card JWS") from exc
    if not isinstance(header, dict):
        raise IdentitySpawnAnchorError("agent-card JWS has no valid kid")
    kid = cast(dict[str, object], header).get("kid")
    if not isinstance(kid, str):
        raise IdentitySpawnAnchorError("agent-card JWS has no valid kid")
    return kid


@dataclass(frozen=True, slots=True)
class AnchoredRunIdentity:
    run_id: str
    agent_id: str
    agent_card_kid: str
    card_hash: str
    signed_card_digest: str
    svid_reference: str
    run_journal_head: str


@dataclass(slots=True)
class IdentitySpawnAnchor:
    chain: AuditChainStore
    trusted_public_keys: Mapping[str, bytes]
    clock: Callable[[], float] = time.time

    def anchor(
        self,
        *,
        run_id: str,
        card: AgentIdentityCard,
        signature: AgentCardSignature,
        run_journal_head: str,
    ) -> AnchoredRunIdentity:
        snapshot = deepcopy(card)
        kid = _jws_kid(signature.detached_jws)
        if signature.kid != kid:
            raise IdentitySpawnAnchorError("agent-card kid substitution detected")
        public_key = self.trusted_public_keys.get(kid)
        if public_key is None or not verify_agent_card(snapshot, signature, public_key):
            raise IdentitySpawnAnchorError("agent-card signature is not trusted")
        now = float(self.clock())
        if snapshot.created_at > now or (snapshot.expires_at and snapshot.expires_at <= now):
            raise IdentitySpawnAnchorError("agent card is not valid at spawn time")

        envelope = {"card": asdict(snapshot), "signature": asdict(signature)}
        digest = "sha256:" + hashlib.sha256(canonicalize_jcs(envelope)).hexdigest()
        identity = AnchoredRunIdentity(
            run_id=run_id,
            agent_id=snapshot.agent_id,
            agent_card_kid=kid,
            card_hash=snapshot.card_hash,
            signed_card_digest=digest,
            svid_reference=snapshot.svid_reference,
            run_journal_head=run_journal_head,
        )
        details = {**asdict(identity), "anchored_at": now, "signed_card": envelope}

        with self.chain.chain_transaction():
            existing = self.chain.query(
                event_type=EVENT_IDENTITY_SPAWN_ATTESTATION,
                resource_id=run_id,
            )
            if existing:
                prior = {key: existing[0].details.get(key) for key in asdict(identity)}
                if prior == asdict(identity):
                    return identity
                raise IdentitySpawnAnchorError("a conflicting identity is already anchored to this run")
            self.chain.log_with_prev_digest(
                event_type=EVENT_IDENTITY_SPAWN_ATTESTATION,
                actor="bernstein.identity-anchor",
                resource_type="run",
                resource_id=run_id,
                details=details,
            )
        return identity

    def reconstruct(self, run_id: str) -> AnchoredRunIdentity:
        valid, errors = self.chain.verify()
        if not valid:
            raise IdentitySpawnAnchorError(f"audit chain verification failed: {'; '.join(errors)}")
        events = self.chain.query(event_type=EVENT_IDENTITY_SPAWN_ATTESTATION, resource_id=run_id)
        if len(events) != 1:
            raise IdentitySpawnAnchorError("run must contain exactly one identity spawn attestation")
        details = events[0].details
        envelope = details.get("signed_card")
        if not isinstance(envelope, dict):
            raise IdentitySpawnAnchorError("signed-card evidence is unavailable")
        digest = "sha256:" + hashlib.sha256(canonicalize_jcs(envelope)).hexdigest()
        if digest != details.get("signed_card_digest"):
            raise IdentitySpawnAnchorError("signed-card evidence digest mismatch")
        return AnchoredRunIdentity(**{field: details[field] for field in AnchoredRunIdentity.__dataclass_fields__})


__all__ = ["AnchoredRunIdentity", "IdentitySpawnAnchor", "IdentitySpawnAnchorError"]
