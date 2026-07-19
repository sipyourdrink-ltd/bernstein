"""Lineage receipts for inbound A2A task responses (#2609).

An inbound ``POST /a2a/tasks/send`` answers a peer that has no access to
this node's internals. Transport success proves only that bytes arrived; it
says nothing about whether those bytes are the ones the node actually
recorded. This module closes that gap: the response is projected onto a
receipt

``{entry_hash, content_hash, operator_hmac, head_signature, kid}``

that a caller verifies offline, holding nothing but the response bytes, the
receipt, and a public key.

Scope of the claim
------------------
A receipt attests the exact response bytes the node recorded *at the moment
it was issued*. On the inbound send path that moment is acceptance: the
response is the acceptance record (the task this node took in and chained),
not the eventual completed result. So a receipt proves "this is the answer
this node recorded for this task", not "this task has finished". Re-attesting
the completed result when the task terminates, and serving that receipt from
the read path, is tracked as a follow-up on #2609. The mechanism below is
independent of which response dict it is handed, so it carries over unchanged
when the completion-time response is the one recorded.

Two independent claims, deliberately kept apart
-----------------------------------------------
* **Identity evidence** - the signed capability card served at
  ``/.well-known/agent.json`` plus its ``kid`` in the JWK set. It answers
  "which node is this, and what does it advertise?".
* **Execution evidence** - the receipt in this module. It answers "was this
  response observed from this execution path?".

A caller must be able to check the second without upgrading the first into
trust, and without asking the node to summarise its own behaviour. Verifying
a receipt therefore never contacts the node.

What the signature covers
-------------------------
``head_signature`` is an Ed25519 signature (via the shared
:class:`~bernstein.core.security.lineage_kms.KMSAdapter`, the same signer the
audit chain head uses) over the **binding digest**: a SHA-256 over the
canonical ``{schema_version, task_id, artefact_path, content_hash,
entry_hash, operator_hmac, kid}``. Because the binding names both the content
hash and the chain anchor, one signature check catches

* a tampered answer (``content_hash`` no longer matches the bytes), and
* a tampered receipt (the binding digest no longer matches the signature).

Strip the signature and the response is not "unlogged" - it is
*unverifiable*. There is no other path from the bytes back to this node's
execution, which is the point.

Determinism
-----------
The receipt is a deterministic projection of (response payload, spine state,
timestamp). The response is canonicalised with RFC 8785-style sorted, compact
JSON before hashing, and Ed25519 is deterministic per RFC 8032, so two
identical inbound tasks against identical state yield byte-identical
receipts. The timestamp is an explicit argument rather than an ambient clock
read, so determinism is a property of the call, not of a patched module.

The write lands on :class:`~bernstein.core.lineage.spine.LineageSpine`, whose
entry hashes chain over the previous head. A receipt therefore fixes the
response's *position* in an ordered chain, not merely its existence.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.security.audit_head_signature import (
    build_head_signature,
    verify_head_signature,
)

if TYPE_CHECKING:
    from bernstein.core.lineage.spine import LineageSpine
    from bernstein.core.security.lineage_kms import KMSAdapter

__all__ = [
    "A2A_RECEIPT_SCHEMA_VERSION",
    "A2AReceiptIssuer",
    "A2AReceiptVerification",
    "A2ATaskReceipt",
    "canonical_response_bytes",
    "receipt_artefact_path",
    "receipt_binding_digest",
    "verify_task_receipt",
]

#: Receipt schema version. Bumping requires a parallel reader.
A2A_RECEIPT_SCHEMA_VERSION: int = 1

#: Lineage path prefix under which inbound A2A responses are anchored.
_RECEIPT_PATH_PREFIX = ".sdd/a2a/responses"


def canonical_response_bytes(response: dict[str, Any]) -> bytes:
    """Return the canonical bytes a receipt's ``content_hash`` covers.

    Sorted keys, no insignificant whitespace, UTF-8. This is the same
    canonicalisation the rest of the stack signs over (RFC 8785 shape), so a
    peer that already verifies capability cards needs no new primitive.

    Args:
        response: The JSON-serialisable A2A response payload.

    Returns:
        Deterministic UTF-8 bytes.

    Raises:
        ValueError: If the payload is not JSON-serialisable, or contains
            NaN/Infinity (which have no canonical JSON form).
    """
    try:
        return json.dumps(
            response,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"A2A response is not canonicalisable: {exc}") from exc


def receipt_artefact_path(task_id: str) -> str:
    """Return the repo-relative lineage path anchoring ``task_id``.

    The path is sanitised so a hostile ``task_id`` from the wire cannot
    smuggle traversal segments into the spine; the spine rejects those
    anyway, but failing early keeps the error attributable.
    """
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in task_id)
    return f"{_RECEIPT_PATH_PREFIX}/{safe}.json"


@dataclass(frozen=True, slots=True)
class A2ATaskReceipt:
    """Execution evidence for one inbound A2A task response.

    Attributes:
        schema_version: See :data:`A2A_RECEIPT_SCHEMA_VERSION`.
        task_id: The A2A task the response answers.
        artefact_path: Lineage path the response was anchored at.
        content_hash: ``sha256:`` over :func:`canonical_response_bytes`.
        entry_hash: Hash of the spine entry recording the write. Chains over
            the previous chain head, so it fixes ordering as well as content.
        operator_hmac: The spine entry's HMAC tag, echoed so a holder of the
            operator key can cross-check the entry without the spine.
        kid: Key id of the signing identity behind ``head_signature``.
        head_signature: Ed25519 signature block over the binding digest,
            shaped like the audit chain's ``head_signature``.
    """

    schema_version: int
    task_id: str
    artefact_path: str
    content_hash: str
    entry_hash: str
    operator_hmac: str
    kid: str
    head_signature: dict[str, Any] = field(default_factory=dict)

    def binding(self) -> dict[str, Any]:
        """Return the exact fields the head signature attests to."""
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "artefact_path": self.artefact_path,
            "content_hash": self.content_hash,
            "entry_hash": self.entry_hash,
            "operator_hmac": self.operator_hmac,
            "kid": self.kid,
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the JSON document carried on the wire."""
        return self.binding() | {"head_signature": dict(self.head_signature)}

    def to_json(self, *, indent: int | None = 2) -> str:
        """Render the receipt as a JSON string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> A2ATaskReceipt:
        """Rebuild a receipt from a parsed JSON document.

        Raises:
            ValueError: If the document is not a well-formed receipt.
        """
        if not isinstance(data, dict):
            raise ValueError("A2A receipt must be a JSON object")
        for key in ("task_id", "artefact_path", "content_hash", "entry_hash", "kid"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError(f"A2A receipt '{key}' must be a non-empty string")
        head_signature = data.get("head_signature", {})
        if not isinstance(head_signature, dict):
            raise ValueError("A2A receipt 'head_signature' must be an object")
        version = data.get("schema_version", A2A_RECEIPT_SCHEMA_VERSION)
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValueError("A2A receipt 'schema_version' must be an integer")
        return cls(
            schema_version=version,
            task_id=str(data["task_id"]),
            artefact_path=str(data["artefact_path"]),
            content_hash=str(data["content_hash"]),
            entry_hash=str(data["entry_hash"]),
            operator_hmac=str(data.get("operator_hmac", "")),
            kid=str(data["kid"]),
            head_signature=dict(head_signature),
        )

    @classmethod
    def from_json(cls, text: str) -> A2ATaskReceipt:
        """Parse a receipt from a JSON string."""
        return cls.from_dict(json.loads(text))


def receipt_binding_digest(receipt: A2ATaskReceipt) -> str:
    """Return the hex SHA-256 the head signature is computed over.

    Signing a digest (rather than the receipt JSON) keeps the payload a fixed
    32 bytes, which is what :func:`build_head_signature` already expects from
    the audit chain head.
    """
    canonical = json.dumps(
        receipt.binding(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class A2AReceiptVerification:
    """Outcome of verifying an :class:`A2ATaskReceipt`.

    Attributes:
        ok: ``True`` only when the content hash matches the supplied bytes
            *and* the head signature verifies over the binding digest.
        errors: Human-readable failure messages, empty when ``ok``.
        verified_key_id: ``key_id`` from the signature block on success.
    """

    ok: bool
    errors: list[str] = field(default_factory=list)
    verified_key_id: str | None = None


def verify_task_receipt(
    receipt: A2ATaskReceipt,
    *,
    response: dict[str, Any] | None = None,
    response_bytes: bytes | None = None,
    trusted_public_key_jwk: dict[str, Any] | None = None,
) -> A2AReceiptVerification:
    """Verify a receipt against the response it claims to attest.

    Offline by construction: nothing here contacts the issuing node. Supply
    either the parsed ``response`` or the raw ``response_bytes`` a peer
    received on the wire.

    Args:
        receipt: The receipt to check.
        response: Parsed response payload; canonicalised before hashing.
        response_bytes: Raw response bytes, used verbatim. Takes precedence
            over ``response`` when both are given.
        trusted_public_key_jwk: When supplied, the receipt's embedded JWK must
            match it before the signature is trusted. When omitted the
            embedded key is trusted on first use -- which authenticates the
            bytes against *a* key, not against *the operator's pinned* key.

    Returns:
        :class:`A2AReceiptVerification`.
    """
    errors: list[str] = []

    if response_bytes is None and response is None:
        return A2AReceiptVerification(ok=False, errors=["no response supplied to verify against"])

    if response_bytes is None:
        try:
            response_bytes = canonical_response_bytes(response or {})
        except ValueError as exc:
            return A2AReceiptVerification(ok=False, errors=[str(exc)])

    recomputed = "sha256:" + hashlib.sha256(response_bytes).hexdigest()
    if recomputed != receipt.content_hash:
        errors.append(f"content_hash mismatch: response hashes to {recomputed}, receipt claims {receipt.content_hash}")

    if not receipt.head_signature:
        errors.append("head_signature is missing - the response carries no proof of execution")
        return A2AReceiptVerification(ok=False, errors=errors)

    verification = verify_head_signature(
        receipt_binding_digest(receipt),
        receipt.head_signature,
        trusted_public_key_jwk=trusted_public_key_jwk,
    )
    if not verification.ok:
        errors.extend(f"head_signature: {err}" for err in verification.errors)

    if errors:
        return A2AReceiptVerification(ok=False, errors=errors)
    return A2AReceiptVerification(ok=True, verified_key_id=verification.verified_key_id)


class A2AReceiptIssuer:
    """Records inbound A2A responses on the lineage spine and mints receipts.

    One issuer is held per server instance. It owns the
    :class:`~bernstein.core.lineage.spine.LineageSpine` the responses are
    anchored on and the KMS adapter that signs receipt bindings. Issuing is
    thread-safe to the same degree the spine is (appends serialise on an
    ``flock``).

    The spine is Merkle-chained: each entry hash covers the previous head, so
    a receipt does not merely record that a response happened, it fixes the
    response's position in an ordered chain. Removing or reordering an entry
    breaks every later entry hash.
    """

    __slots__ = ("_actor", "_kid", "_kms", "_model", "_spine")

    def __init__(
        self,
        *,
        spine: LineageSpine,
        kid: str,
        kms_adapter: KMSAdapter,
        actor: str = "a2a-server",
        model: str = "",
    ) -> None:
        self._spine = spine
        self._kid = kid
        self._kms = kms_adapter
        self._actor = actor
        self._model = model

    def issue(
        self,
        *,
        task_id: str,
        response: dict[str, Any],
        step_id: str = "",
        timestamp: int | None = None,
    ) -> A2ATaskReceipt:
        """Record ``response`` on the spine and return its receipt.

        Args:
            task_id: The A2A task the response answers.
            response: JSON-serialisable response payload.
            step_id: Optional cross-link to the originating step / tool call.
            timestamp: Integer timestamp recorded on the entry. Defaults to
                the current time in nanoseconds. Passing an explicit value
                makes the whole projection deterministic, which is what the
                determinism tests rely on.

        Returns:
            The :class:`A2ATaskReceipt` to attach to the response.

        Raises:
            ValueError: If ``task_id`` is empty or the response cannot be
                canonicalised.
        """
        if not task_id:
            raise ValueError("task_id must be a non-empty string")

        content = canonical_response_bytes(response)
        artefact_path = receipt_artefact_path(task_id)
        ts = time.time_ns() if timestamp is None else timestamp

        entry = self._spine.record_entry(
            artifact_path=artefact_path,
            content=content,
            actor=self._actor,
            step_id=step_id or f"a2a:{task_id}",
            model=self._model,
            timestamp=ts,
        )

        unsigned = A2ATaskReceipt(
            schema_version=A2A_RECEIPT_SCHEMA_VERSION,
            task_id=task_id,
            artefact_path=artefact_path,
            content_hash="sha256:" + hashlib.sha256(content).hexdigest(),
            entry_hash=entry.entry_hash,
            # The HMAC tag comes straight off the entry we just appended, so
            # issuing a receipt no longer re-reads the whole spine per inbound
            # request (that walk was O(entries) on a network-facing path).
            operator_hmac=entry.hmac,
            kid=self._kid,
        )
        head_signature = build_head_signature(
            receipt_binding_digest(unsigned),
            kms_adapter=self._kms,
        )
        return A2ATaskReceipt(
            schema_version=unsigned.schema_version,
            task_id=unsigned.task_id,
            artefact_path=unsigned.artefact_path,
            content_hash=unsigned.content_hash,
            entry_hash=unsigned.entry_hash,
            operator_hmac=unsigned.operator_hmac,
            kid=unsigned.kid,
            head_signature=head_signature,
        )
