"""Provider-neutral pre-dispatch interlock for attested tool calls.

The observe-only instrumenter must remain non-blocking.  Completeness therefore
needs a separate boundary owned by the orchestrator: in enforced mode this
module requires a provider to verify and durably record the attestation and its
dispatch marker before the connector callback becomes reachable.  Observed
mode attempts the same preparation but deliberately keeps failures non-fatal.

The provider protocol is intentionally about evidence, not key management or
policy.  Bernstein's native signer can implement it, as can an operator adapter,
without making either implementation a dependency of the dispatch boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast

from bernstein.core.security.sanitize import sanitize_log

logger = logging.getLogger(__name__)


class AttestationMode(StrEnum):
    """Runtime behavior when attestation preparation fails."""

    ENFORCED = "enforced"
    OBSERVED = "observed"


class AttestationVerdict(StrEnum):
    """Completeness verdict derived from chain evidence."""

    COMPLETE = "complete"
    OBSERVED = "observed"


@dataclass(frozen=True, slots=True)
class ToolCallIntent:
    """Content-derived description of one about-to-dispatch tool call.

    ``scope_id`` is an opaque run/agent authority binding supplied by the
    provider integration.  It lets the host bind evidence to the correct
    execution scope without owning the provider's identity or policy schema.
    """

    scope_id: str
    server_name: str
    method: str
    tool_name: str
    request_id: str
    span_id: str
    args_digest: str

    @classmethod
    def from_request(
        cls,
        *,
        scope_id: str,
        server_name: str,
        method: str,
        tool_name: str,
        request_id: Any,
        span_id: str,
        arguments: Any,
    ) -> ToolCallIntent:
        """Build an intent without retaining raw connector arguments."""
        canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return cls(
            scope_id=scope_id,
            server_name=server_name,
            method=method,
            tool_name=tool_name,
            request_id=str(request_id),
            span_id=span_id,
            args_digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        )

    def digest(self) -> str:
        """Return the canonical digest a provider's evidence must bind."""
        payload = {
            "args_digest": self.args_digest,
            "method": self.method,
            "request_id": self.request_id,
            "scope_id": self.scope_id,
            "server_name": self.server_name,
            "span_id": self.span_id,
            "tool_name": self.tool_name,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class VerifiedDispatchEvidence:
    """Opaque handles proving preparation completed before dispatch.

    ``attestation_ref`` identifies the verified, durably chained attestation.
    ``dispatch_ref`` identifies the chained dispatch marker that references it.
    ``intent_digest`` binds both handles back to the exact host-derived call
    intent.  The interlock treats the references as opaque provider-owned
    handles, but refuses empty or mismatched evidence so an accidental stale or
    no-op implementation cannot authorize a call.
    """

    attestation_ref: str
    dispatch_ref: str
    intent_digest: str


class ToolCallEvidenceProvider(Protocol):
    """Verify and durably record evidence for one pending dispatch."""

    async def prepare_dispatch(self, intent: ToolCallIntent) -> VerifiedDispatchEvidence:
        """Return only after the attestation and dispatch marker are durable."""
        ...


class ToolCallInterlockError(RuntimeError):
    """Raised when enforced dispatch cannot establish durable evidence."""


@dataclass(frozen=True, slots=True)
class AttestationModeProjection:
    """Receipt-facing mode projection derived from chain markers."""

    claimed_mode: str
    verdict: AttestationVerdict

    @property
    def complete(self) -> bool:
        """Whether chain evidence supports a completeness claim."""
        return self.verdict is AttestationVerdict.COMPLETE


@dataclass(slots=True)
class ToolCallAttestationInterlock:
    """Apply enforced or observed semantics at a connector boundary."""

    provider: ToolCallEvidenceProvider
    scope_id: str
    mode: AttestationMode = AttestationMode.ENFORCED

    async def before_dispatch(self, intent: ToolCallIntent) -> VerifiedDispatchEvidence | None:
        """Prepare evidence before a connector is invoked.

        Enforced mode fails closed.  Observed mode logs preparation failures and
        returns ``None`` so the connector remains reachable, which is precisely
        why a verifier must report that run as observed rather than complete.
        """
        try:
            if not self.scope_id.strip() or intent.scope_id != self.scope_id:
                raise ToolCallInterlockError("tool-call intent is not bound to the configured evidence scope")
            evidence = await self.provider.prepare_dispatch(intent)
            if not evidence.attestation_ref.strip() or not evidence.dispatch_ref.strip():
                raise ToolCallInterlockError("attestation provider returned an incomplete evidence handle")
            if evidence.intent_digest != intent.digest():
                raise ToolCallInterlockError("attestation provider returned evidence for a different tool-call intent")
            return evidence
        except Exception as exc:
            if self.mode is AttestationMode.OBSERVED:
                logger.warning(
                    "Observed tool-call attestation preparation failed for %s/%s: %s",
                    sanitize_log(intent.server_name),
                    sanitize_log(intent.tool_name),
                    sanitize_log(str(exc)),
                )
                return None
            if isinstance(exc, ToolCallInterlockError):
                raise
            raise ToolCallInterlockError(
                f"enforced tool-call attestation preparation failed for {intent.server_name}/{intent.tool_name}"
            ) from exc


def derive_attestation_verdict(events: Sequence[Mapping[str, Any]]) -> AttestationVerdict:
    """Derive completeness from ordered chain projections, never a claim field.

    A complete verdict requires at least one ``toolcall.enforced_dispatch``
    event and, for every such event, a preceding ``toolcall.attestation`` whose
    ``attestation_ref`` it references.  Missing, reordered, or unpaired markers
    always downgrade to :attr:`AttestationVerdict.OBSERVED`.

    Receipt fields such as ``claimed_mode`` are intentionally ignored.
    """
    attestations: dict[str, str] = {}
    dispatch_count = 0
    for event in events:
        event_type = str(event.get("event_type", event.get("event", "")))
        details = event.get("details")
        payload: Mapping[str, Any] = cast("Mapping[str, Any]", details) if isinstance(details, Mapping) else event
        if event_type == "toolcall.attestation":
            reference = str(payload.get("attestation_ref", "")).strip()
            intent_digest = str(payload.get("intent_digest", "")).strip()
            if reference and intent_digest:
                attestations[reference] = intent_digest
            continue
        if event_type != "toolcall.enforced_dispatch":
            continue
        dispatch_count += 1
        reference = str(payload.get("attestation_ref", "")).strip()
        intent_digest = str(payload.get("intent_digest", "")).strip()
        if not reference or not intent_digest or attestations.get(reference) != intent_digest:
            return AttestationVerdict.OBSERVED
    if dispatch_count == 0:
        return AttestationVerdict.OBSERVED
    return AttestationVerdict.COMPLETE


def project_attestation_mode(
    events: Sequence[Mapping[str, Any]], *, claimed_mode: str = ""
) -> AttestationModeProjection:
    """Construct the receipt-facing projection without trusting its claim.

    ``claimed_mode`` is retained so a verifier can explain a downgrade, but it
    has no influence on the verdict.  Observed-mode receipt construction thus
    remains available after an instrumentation failure without manufacturing a
    completeness guarantee.
    """
    return AttestationModeProjection(
        claimed_mode=claimed_mode,
        verdict=derive_attestation_verdict(events),
    )


__all__ = [
    "AttestationMode",
    "AttestationModeProjection",
    "AttestationVerdict",
    "ToolCallAttestationInterlock",
    "ToolCallEvidenceProvider",
    "ToolCallIntent",
    "ToolCallInterlockError",
    "VerifiedDispatchEvidence",
    "derive_attestation_verdict",
    "project_attestation_mode",
]
