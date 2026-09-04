"""Bind one verified signed agent identity to one Bernstein run."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.identity.agent_card import AgentIdentityCard
from bernstein.core.security.agent_card_signer import (
    AgentCardSignature,
    canonicalize_jcs,
    ed25519_pem_from_jwk,
    ed25519_public_jwk,
    verify_agent_card,
)
from bernstein.core.security.audit_chain import (
    EVENT_IDENTITY_SPAWN_ATTESTATION,
    AuditChainStore,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from bernstein.core.lineage.identity import AgentCard


class IdentitySpawnAnchorError(RuntimeError):
    """Raised when a run identity cannot be anchored or reconstructed."""


def _sha256_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonicalize_jcs(value)).hexdigest()


def _card_is_valid_at(card: AgentIdentityCard, instant: float) -> bool:
    return card.created_at <= instant and (not card.expires_at or card.expires_at > instant)


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
    tool_signing_kid: str | None = None
    tool_verification_key_jwk: dict[str, Any] | None = None
    tool_verification_key_digest: str | None = None


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
        tool_signing_card: AgentCard | None = None,
    ) -> AnchoredRunIdentity:
        snapshot = deepcopy(card)
        kid = _jws_kid(signature.detached_jws)
        if signature.kid != kid:
            raise IdentitySpawnAnchorError("agent-card kid substitution detected")
        public_key = self.trusted_public_keys.get(kid)
        if public_key is None or not verify_agent_card(snapshot, signature, public_key):
            raise IdentitySpawnAnchorError("agent-card signature is not trusted")
        validated_at = float(self.clock())
        if not _card_is_valid_at(snapshot, validated_at):
            raise IdentitySpawnAnchorError("agent card is not valid at spawn time")

        envelope = {"card": asdict(snapshot), "signature": asdict(signature)}
        digest = _sha256_digest(envelope)
        public_jwk = ed25519_public_jwk(public_key, kid=kid)
        tool_jwk: dict[str, Any] | None = None
        tool_jwk_digest: str | None = None
        tool_kid: str | None = None
        if tool_signing_card is not None:
            if tool_signing_card.agent_id != snapshot.agent_id or not tool_signing_card.kid.strip():
                raise IdentitySpawnAnchorError("lineage tool signing identity does not match the spawned agent")
            try:
                tool_jwk = ed25519_public_jwk(
                    tool_signing_card.public_key_pem.encode("ascii"),
                    kid=tool_signing_card.kid,
                )
            except (ValueError, TypeError, UnicodeEncodeError) as exc:
                raise IdentitySpawnAnchorError("lineage tool verification key is not valid Ed25519 material") from exc
            tool_kid = tool_signing_card.kid
            tool_jwk_digest = _sha256_digest(tool_jwk)
        identity = AnchoredRunIdentity(
            run_id=run_id,
            agent_id=snapshot.agent_id,
            agent_card_kid=kid,
            card_hash=snapshot.card_hash,
            signed_card_digest=digest,
            svid_reference=snapshot.svid_reference,
            run_journal_head=run_journal_head,
            tool_signing_kid=tool_kid,
            tool_verification_key_jwk=tool_jwk,
            tool_verification_key_digest=tool_jwk_digest,
        )
        details = {
            **asdict(identity),
            "validated_at": validated_at,
            "signed_card": envelope,
            "verification_key_jwk": public_jwk,
            "verification_key_digest": _sha256_digest(public_jwk),
        }

        with self.chain.chain_transaction():
            existing = self.chain.query(
                event_type=EVENT_IDENTITY_SPAWN_ATTESTATION,
                resource_id=run_id,
            )
            if existing:
                prior = {key: existing[0].details.get(key) for key in asdict(identity)}
                if prior == asdict(identity):
                    return identity
                if prior.get("run_journal_head") != run_journal_head:
                    raise IdentitySpawnAnchorError("run journal head moved since the identity was anchored")
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
        envelope = cast(object, details.get("signed_card"))
        if not isinstance(envelope, dict):
            raise IdentitySpawnAnchorError("signed-card evidence is unavailable")
        typed_envelope = cast(dict[str, Any], envelope)
        digest = _sha256_digest(typed_envelope)
        if digest != details.get("signed_card_digest"):
            raise IdentitySpawnAnchorError("signed-card evidence digest mismatch")
        card_data = cast(object, typed_envelope.get("card"))
        signature_data = cast(object, typed_envelope.get("signature"))
        if not isinstance(card_data, dict) or not isinstance(signature_data, dict):
            raise IdentitySpawnAnchorError("signed-card evidence is malformed")
        try:
            card = AgentIdentityCard(**cast(dict[str, Any], card_data))
            signature = AgentCardSignature(**cast(dict[str, Any], signature_data))
            kid = _jws_kid(signature.detached_jws)
        except (TypeError, IdentitySpawnAnchorError) as exc:
            raise IdentitySpawnAnchorError("signed-card evidence is malformed") from exc

        validated_at = details.get("validated_at")
        if not isinstance(validated_at, (int, float)) or isinstance(validated_at, bool):
            raise IdentitySpawnAnchorError("historical validation timestamp is unavailable")
        if not _card_is_valid_at(card, float(validated_at)):
            raise IdentitySpawnAnchorError("agent card was not valid at its recorded validation time")

        public_jwk = cast(object, details.get("verification_key_jwk"))
        if not isinstance(public_jwk, dict):
            raise IdentitySpawnAnchorError("frozen historical verification key is unavailable")
        typed_jwk = cast(dict[str, Any], public_jwk)
        if _sha256_digest(typed_jwk) != details.get("verification_key_digest"):
            raise IdentitySpawnAnchorError("frozen historical verification key digest mismatch")
        if typed_jwk.get("kid") != kid or typed_jwk.get("alg") != "EdDSA" or typed_jwk.get("use") != "sig":
            raise IdentitySpawnAnchorError("frozen historical verification key metadata mismatch")
        try:
            public_key = ed25519_pem_from_jwk(typed_jwk)
        except ValueError as exc:
            raise IdentitySpawnAnchorError("frozen historical verification key is malformed") from exc
        identity_mismatch = signature.kid != kid or kid != details.get("agent_card_kid")
        if identity_mismatch or not verify_agent_card(card, signature, public_key):
            raise IdentitySpawnAnchorError("historical signed-card verification failed")
        tool_kid = details.get("tool_signing_kid")
        tool_jwk = details.get("tool_verification_key_jwk")
        tool_digest = details.get("tool_verification_key_digest")
        if any(value is not None for value in (tool_kid, tool_jwk, tool_digest)):
            if not isinstance(tool_kid, str) or not isinstance(tool_jwk, dict) or not isinstance(tool_digest, str):
                raise IdentitySpawnAnchorError("frozen tool verification identity is incomplete")
            typed_tool_jwk = cast("dict[str, Any]", tool_jwk)
            if typed_tool_jwk.get("kid") != tool_kid or _sha256_digest(typed_tool_jwk) != tool_digest:
                raise IdentitySpawnAnchorError("frozen tool verification identity mismatch")
            try:
                ed25519_pem_from_jwk(typed_tool_jwk)
            except ValueError as exc:
                raise IdentitySpawnAnchorError("frozen tool verification key is malformed") from exc
        required = {
            field: details[field]
            for field in (
                "run_id",
                "agent_id",
                "agent_card_kid",
                "card_hash",
                "signed_card_digest",
                "svid_reference",
                "run_journal_head",
            )
        }
        return AnchoredRunIdentity(
            **required,
            tool_signing_kid=cast("str | None", tool_kid),
            tool_verification_key_jwk=cast("dict[str, Any] | None", tool_jwk),
            tool_verification_key_digest=cast("str | None", tool_digest),
        )


__all__ = ["AnchoredRunIdentity", "IdentitySpawnAnchor", "IdentitySpawnAnchorError"]
