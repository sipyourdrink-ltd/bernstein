"""Bernstein-native durable evidence for enforced tool-call dispatch."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from bernstein.core.security.audit_chain import (
    EVENT_TOOLCALL_ATTESTATION,
    EVENT_TOOLCALL_ENFORCED_DISPATCH,
    AuditChainStore,
)
from bernstein.core.security.toolcall_interlock import (
    ToolCallIntent,
    VerifiedDispatchEvidence,
)


def _content_ref(kind: str, payload: dict[str, str]) -> str:
    canonical = json.dumps(
        {"kind": kind, "payload": payload, "version": 1},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class NativeToolCallEvidenceProvider:
    """Write host-derived tool-call evidence to Bernstein's HMAC audit chain.

    This provider proves durable, ordered capture at the connector boundary. It
    deliberately does not claim per-agent signed identity: a later identity
    provider can implement the same protocol without changing the interlock.
    """

    chain: AuditChainStore
    actor: str = "bernstein.toolcall-interlock"

    async def prepare_dispatch(self, intent: ToolCallIntent) -> VerifiedDispatchEvidence:
        """Append attestation and admission markers before dispatch is allowed."""
        fields = asdict(intent)
        if any(not value.strip() for value in fields.values()):
            raise ValueError("tool-call intent fields must be non-empty")

        intent_digest = intent.digest()
        attestation_ref = _content_ref(
            "bernstein.toolcall.attestation",
            {"intent_digest": intent_digest, **fields},
        )
        common = {
            "attestation_ref": attestation_ref,
            "intent_digest": intent_digest,
            **fields,
        }

        # Keep the two appends adjacent across threads and processes. If the
        # second append fails, no evidence handle is returned and enforced mode
        # leaves the connector unreachable; the first record remains honest
        # partial evidence rather than being rolled back or misreported.
        with self.chain.chain_transaction():
            self.chain.log_with_prev_digest(
                event_type=EVENT_TOOLCALL_ATTESTATION,
                actor=self.actor,
                resource_type="toolcall_scope",
                resource_id=intent.scope_id,
                details=common,
            )
            dispatch = self.chain.log_with_prev_digest(
                event_type=EVENT_TOOLCALL_ENFORCED_DISPATCH,
                actor=self.actor,
                resource_type="toolcall_scope",
                resource_id=intent.scope_id,
                details=common,
            )

        return VerifiedDispatchEvidence(
            attestation_ref=attestation_ref,
            dispatch_ref="hmac:" + dispatch.hmac,
            intent_digest=intent_digest,
        )


__all__ = ["NativeToolCallEvidenceProvider"]
