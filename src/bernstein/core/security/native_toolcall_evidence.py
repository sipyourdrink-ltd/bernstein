"""Bernstein-native durable evidence for enforced tool-call dispatch."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.identity import jws_header_kid
from bernstein.core.security.audit_chain import (
    EVENT_TOOLCALL_ATTESTATION,
    EVENT_TOOLCALL_ENFORCED_DISPATCH,
    AuditChainStore,
)
from bernstein.core.security.toolcall_identity import (
    TOOLCALL_IDENTITY_DOMAIN,
    FrozenToolCallIdentityVerifier,
    ToolCallIdentityAttestation,
    ToolCallIdentityError,
    ToolCallIdentitySigner,
    identity_envelope,
)
from bernstein.core.security.toolcall_interlock import (
    ToolCallIntent,
    VerifiedDispatchEvidence,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.core.security.audit import ChainScanCursor
    from bernstein.core.security.identity_spawn_anchor import AnchoredRunIdentity


def _content_ref(kind: str, payload: dict[str, str]) -> str:
    canonical = json.dumps(
        {"kind": kind, "payload": payload, "version": 1},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


@dataclass(slots=True)
class NativeToolCallEvidenceProvider:
    """Write host-derived tool-call evidence to Bernstein's HMAC audit chain.

    This provider proves durable, ordered capture at the connector boundary. It
    Legacy construction retains the HMAC-only evidence path. Supplying all
    identity arguments additionally signs each exact intent with the run's
    spawn-frozen lineage key; no vendor or remote identity service is required.
    """

    chain: AuditChainStore
    actor: str = "bernstein.toolcall-interlock"
    run_identity: AnchoredRunIdentity | None = None
    signer: ToolCallIdentitySigner | None = None
    run_journal_head: Callable[[], str] | None = None
    clock_ns: Callable[[], int] = time.time_ns
    _cursor: ChainScanCursor | None = field(init=False, default=None)
    _anchor_events: list[Any] = field(init=False, default_factory=list)  # pyright: ignore[reportUnknownVariableType]
    _call_indices: list[int] = field(init=False, default_factory=list)  # pyright: ignore[reportUnknownVariableType]
    _frozen_verifier: FrozenToolCallIdentityVerifier | None = field(init=False, default=None)
    _identity_anchor_ref: str = field(init=False, default="")
    _known_chain_head: str = field(init=False, default="")
    _seen_event_hmacs: set[str] = field(init=False, default_factory=set)  # pyright: ignore[reportUnknownVariableType]

    def __post_init__(self) -> None:
        """Authenticate cold history once for identity-bound construction."""
        if self._identity_enabled():
            self._consume_verified_history()
            identity = self.run_identity
            assert identity is not None
            anchor = self._anchor_details(identity)
            if not all(
                anchor.get(field)
                for field in (
                    "tool_signing_kid",
                    "tool_verification_key_jwk",
                    "tool_verification_key_digest",
                )
            ):
                raise ToolCallIdentityError("run anchor has no frozen tool signing identity")
            self._frozen_verifier = FrozenToolCallIdentityVerifier.from_anchor(anchor, agent_id=identity.agent_id)
            self._identity_anchor_ref = "hmac:" + self._anchor_events[0].hmac

    def _identity_enabled(self) -> bool:
        configured = (self.run_identity, self.signer, self.run_journal_head)
        if any(value is not None for value in configured) and not all(value is not None for value in configured):
            raise ValueError("run identity, signer, and journal-head reader must be configured together")
        return all(value is not None for value in configured)

    def _consume_verified_history(self) -> None:
        result = self.chain.scan_verified(self._cursor)
        if not result.ok:
            raise ToolCallIdentityError(f"audit history verification failed: {'; '.join(result.errors)}")
        identity = self.run_identity
        for event in result.events:
            if event.event_type == "identity.spawn_attestation" and (
                identity is None or event.resource_id == identity.run_id
            ):
                if event.hmac in self._seen_event_hmacs:
                    continue
                self._seen_event_hmacs.add(event.hmac)
                self._anchor_events.append(event)
            elif (
                identity is not None
                and event.event_type == EVENT_TOOLCALL_ATTESTATION
                and event.details.get("run_id") == identity.run_id
            ):
                if event.hmac in self._seen_event_hmacs:
                    continue
                self._seen_event_hmacs.add(event.hmac)
                index = event.details.get("call_index")
                if not isinstance(index, int) or isinstance(index, bool):
                    raise ToolCallIdentityError("existing run has malformed tool-call identity history")
                self._call_indices.append(index)
        self._cursor = result.cursor
        self._known_chain_head = result.cursor.prev_hmac

    def _anchor_details(self, identity: AnchoredRunIdentity) -> dict[str, Any]:
        if len(self._anchor_events) != 1:
            raise ToolCallIdentityError("run must have exactly one verified identity spawn anchor")
        details = self._anchor_events[0].details
        for key, value in asdict(identity).items():
            if details.get(key) != value:
                raise ToolCallIdentityError("configured run identity conflicts with its verified spawn anchor")
        return details

    def _next_call_index(self) -> int:
        ordered = sorted(self._call_indices)
        if ordered != list(range(1, len(ordered) + 1)):
            raise ToolCallIdentityError("existing run has non-contiguous tool-call identity history")
        return len(ordered) + 1

    async def prepare_dispatch(self, intent: ToolCallIntent) -> VerifiedDispatchEvidence:
        """Append attestation and admission markers before dispatch is allowed."""
        fields = asdict(intent)
        if any(not value.strip() for value in fields.values()):
            raise ValueError("tool-call intent fields must be non-empty")

        intent_digest = intent.digest()
        if self._identity_enabled():
            return self._prepare_identity_dispatch(intent, intent_digest)
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

    def _prepare_identity_dispatch(self, intent: ToolCallIntent, intent_digest: str) -> VerifiedDispatchEvidence:
        identity = self.run_identity
        signer = self.signer
        journal_head_reader = self.run_journal_head
        assert identity is not None and signer is not None and journal_head_reader is not None
        current_journal_head = journal_head_reader()
        if current_journal_head != identity.run_journal_head:
            raise ToolCallIdentityError("run journal head moved; a new identity-anchored run is required")

        with self.chain.chain_transaction():
            prev_chain_digest = self.chain.resync_head()
            if prev_chain_digest != self._known_chain_head:
                self._consume_verified_history()
                if self._known_chain_head != prev_chain_digest:
                    raise ToolCallIdentityError("authenticated scan did not reach the locked audit-chain head")
            self._anchor_details(identity)
            call_index = self._next_call_index()
            record = ToolCallIdentityAttestation(
                v=1,
                kind="bernstein.toolcall.identity-attestation",
                run_id=identity.run_id,
                agent_id=identity.agent_id,
                scope_id=intent.scope_id,
                server_name=intent.server_name,
                method=intent.method,
                tool_name=intent.tool_name,
                request_id=intent.request_id,
                span_id=intent.span_id,
                args_digest=intent.args_digest,
                intent_digest=intent_digest,
                call_index=call_index,
                run_journal_head=current_journal_head,
                prev_chain_digest=prev_chain_digest,
                identity_anchor_ref=self._identity_anchor_ref,
                tool_signing_kid=identity.tool_signing_kid or "",
                attested_at_ns=self.clock_ns(),
            )
            signing_bytes = record.signing_bytes()
            signature = signer.sign(signing_bytes)
            envelope = identity_envelope(
                record,
                signature,
                record_canonical=signing_bytes[len(TOOLCALL_IDENTITY_DOMAIN) :],
            )
            attestation_ref = str(envelope["attestation_ref"])
            verifier = self._frozen_verifier
            if verifier is None:
                raise ToolCallIdentityError("run has no frozen tool-call identity verifier")
            if signature.kid != verifier.kid or jws_header_kid(signature.detached_jws) != verifier.kid:
                raise ToolCallIdentityError("tool signing kid substitution detected")
            if not verifier.verify(signing_bytes, signature.detached_jws):
                raise ToolCallIdentityError("locally generated tool-call identity signature did not verify")
            common: dict[str, Any] = {
                "attestation_ref": attestation_ref,
                "intent_digest": intent_digest,
                **asdict(intent),
                "run_id": identity.run_id,
                "agent_id": identity.agent_id,
                "call_index": call_index,
                "identity_anchor_ref": record.identity_anchor_ref,
                "identity_envelope": envelope,
            }
            attestation = self.chain.log_with_prev_digest(
                event_type=EVENT_TOOLCALL_ATTESTATION,
                actor=self.actor,
                resource_type="toolcall_scope",
                resource_id=intent.scope_id,
                details=common,
            )
            self._call_indices.append(call_index)
            self._seen_event_hmacs.add(attestation.hmac)
            self._known_chain_head = attestation.hmac
            dispatch = self.chain.log_with_prev_digest(
                event_type=EVENT_TOOLCALL_ENFORCED_DISPATCH,
                actor=self.actor,
                resource_type="toolcall_scope",
                resource_id=intent.scope_id,
                details=common,
            )
            self._known_chain_head = dispatch.hmac

        return VerifiedDispatchEvidence(
            attestation_ref=attestation_ref,
            dispatch_ref="hmac:" + dispatch.hmac,
            intent_digest=intent_digest,
        )


__all__ = ["NativeToolCallEvidenceProvider"]
