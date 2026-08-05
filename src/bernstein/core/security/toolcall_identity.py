"""Signed, run-bound identity evidence for one pre-dispatch tool intent."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol, cast

from bernstein.core.lineage.identity import AgentCard, jws_header_kid, sign_detached, verify_detached
from bernstein.core.security.agent_card_signer import canonicalize_jcs, ed25519_pem_from_jwk

TOOLCALL_IDENTITY_DOMAIN = b"bernstein.toolcall.identity-attestation/v1\x00"


class ToolCallIdentityError(RuntimeError):
    """Raised when signed tool-call identity evidence is invalid."""


@dataclass(frozen=True, slots=True)
class ToolCallIdentityAttestation:
    """Canonical pre-dispatch identity statement for one exact tool intent."""

    v: int
    kind: str
    run_id: str
    agent_id: str
    scope_id: str
    server_name: str
    method: str
    tool_name: str
    request_id: str
    span_id: str
    args_digest: str
    intent_digest: str
    call_index: int
    run_journal_head: str
    prev_chain_digest: str
    identity_anchor_ref: str
    tool_signing_kid: str
    attested_at_ns: int

    def canonical_bytes(self) -> bytes:
        """Return the JCS bytes retained and independently reconstructed by verifiers."""
        return canonicalize_jcs(asdict(self))

    def signing_bytes(self) -> bytes:
        """Return domain-separated bytes so lineage signatures cannot cross contexts."""
        return TOOLCALL_IDENTITY_DOMAIN + self.canonical_bytes()


@dataclass(frozen=True, slots=True)
class ToolCallIdentitySignature:
    """Detached JWS plus the key identifier returned by a signer."""

    detached_jws: str
    kid: str


class ToolCallIdentitySigner(Protocol):
    """Narrow signer boundary; providers never receive or serialize private keys."""

    def sign(self, payload: bytes) -> ToolCallIdentitySignature:
        """Sign already canonicalized and domain-separated bytes."""
        ...


@dataclass(frozen=True, slots=True)
class LineageToolCallIdentitySigner:
    """Adapter over Bernstein's existing per-invocation lineage signer."""

    private_key_pem: str
    kid: str

    def sign(self, payload: bytes) -> ToolCallIdentitySignature:
        """Produce the existing EdDSA/``b64=false`` detached JWS shape."""
        return ToolCallIdentitySignature(
            detached_jws=sign_detached(payload, self.private_key_pem, kid=self.kid),
            kid=self.kid,
        )


def identity_attestation_ref(record: ToolCallIdentityAttestation, signature: ToolCallIdentitySignature) -> str:
    """Identify the exact versioned signed envelope, not merely an unsigned intent."""
    envelope = {
        "detached_jws": signature.detached_jws,
        "record": asdict(record),
        "version": 1,
    }
    return "sha256:" + hashlib.sha256(canonicalize_jcs(envelope)).hexdigest()


def identity_envelope(
    record: ToolCallIdentityAttestation,
    signature: ToolCallIdentitySignature,
) -> dict[str, Any]:
    """Return the public, retained evidence envelope."""
    return {
        "domain": TOOLCALL_IDENTITY_DOMAIN[:-1].decode("ascii"),
        "record": asdict(record),
        "detached_jws": signature.detached_jws,
        "attestation_ref": identity_attestation_ref(record, signature),
    }


def verify_identity_envelope(
    envelope: Mapping[str, Any],
    anchor: Mapping[str, Any],
    *,
    expected_intent_digest: str | None = None,
    expected_attestation_ref: str | None = None,
) -> ToolCallIdentityAttestation:
    """Verify an envelope solely against public material frozen in its run anchor."""
    if envelope.get("domain") != TOOLCALL_IDENTITY_DOMAIN[:-1].decode("ascii"):
        raise ToolCallIdentityError("tool-call identity domain mismatch")
    raw_record = envelope.get("record")
    detached_jws = envelope.get("detached_jws")
    if not isinstance(raw_record, Mapping) or not isinstance(detached_jws, str):
        raise ToolCallIdentityError("tool-call identity envelope is malformed")
    typed_record = cast("Mapping[str, Any]", raw_record)
    expected_fields = set(ToolCallIdentityAttestation.__dataclass_fields__)
    if set(typed_record) != expected_fields:
        raise ToolCallIdentityError("tool-call identity record fields are not exact")
    try:
        record = ToolCallIdentityAttestation(**dict(typed_record))
    except (TypeError, ValueError) as exc:
        raise ToolCallIdentityError("tool-call identity record is malformed") from exc
    if record.v != 1 or record.kind != "bernstein.toolcall.identity-attestation":
        raise ToolCallIdentityError("tool-call identity record version or kind is unsupported")
    non_empty_string_fields = (
        "run_id",
        "agent_id",
        "scope_id",
        "server_name",
        "method",
        "tool_name",
        "request_id",
        "span_id",
        "args_digest",
        "intent_digest",
        "prev_chain_digest",
        "identity_anchor_ref",
        "tool_signing_kid",
    )
    if not isinstance(cast("object", record.run_journal_head), str) or any(
        not isinstance(getattr(record, field), str) or not getattr(record, field) for field in non_empty_string_fields
    ):
        raise ToolCallIdentityError("tool-call identity string binding is malformed")
    if (
        type(record.call_index) is not int
        or type(record.attested_at_ns) is not int
        or record.call_index < 1
        or record.attested_at_ns < 0
    ):
        raise ToolCallIdentityError("tool-call identity index or timestamp is invalid")

    anchor_kid = anchor.get("tool_signing_kid")
    anchor_jwk = anchor.get("tool_verification_key_jwk")
    anchor_digest = anchor.get("tool_verification_key_digest")
    if not isinstance(anchor_kid, str) or not isinstance(anchor_jwk, Mapping) or not isinstance(anchor_digest, str):
        raise ToolCallIdentityError("run anchor has no tool signing identity")
    typed_jwk = dict(cast("Mapping[str, Any]", anchor_jwk))
    digest = "sha256:" + hashlib.sha256(canonicalize_jcs(typed_jwk)).hexdigest()
    if digest != anchor_digest:
        raise ToolCallIdentityError("frozen tool verification key digest mismatch")
    if record.tool_signing_kid != anchor_kid or jws_header_kid(detached_jws) != anchor_kid:
        raise ToolCallIdentityError("tool signing kid substitution detected")
    try:
        public_key_pem = ed25519_pem_from_jwk(typed_jwk).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ToolCallIdentityError("frozen tool verification key is malformed") from exc
    card = AgentCard(agent_id=record.agent_id, kid=anchor_kid, public_key_pem=public_key_pem)
    if not verify_detached(record.signing_bytes(), detached_jws, card):
        raise ToolCallIdentityError("tool-call identity signature verification failed")

    signature = ToolCallIdentitySignature(detached_jws=detached_jws, kid=anchor_kid)
    reference = identity_attestation_ref(record, signature)
    if envelope.get("attestation_ref") != reference:
        raise ToolCallIdentityError("tool-call identity attestation reference mismatch")
    if expected_attestation_ref is not None and reference != expected_attestation_ref:
        raise ToolCallIdentityError("tool-call identity envelope references different evidence")
    if expected_intent_digest is not None and record.intent_digest != expected_intent_digest:
        raise ToolCallIdentityError("tool-call identity envelope binds a different intent")
    return record


__all__ = [
    "TOOLCALL_IDENTITY_DOMAIN",
    "LineageToolCallIdentitySigner",
    "ToolCallIdentityAttestation",
    "ToolCallIdentityError",
    "ToolCallIdentitySignature",
    "ToolCallIdentitySigner",
    "identity_attestation_ref",
    "identity_envelope",
    "verify_identity_envelope",
]
