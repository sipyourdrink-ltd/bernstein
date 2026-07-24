"""Receipt-anchored OData v4 write-back helper.

A write-back to a system of record is not a fire-and-forget HTTP call; it is a
provable act. :func:`update_entity` implements the GET-before-PATCH pattern with
``If-Match`` optimistic concurrency, surfaces ``412`` / ``428`` as typed
conflicts (never a blind retry that could clobber a concurrent human edit), and
emits a receipt bound into the HMAC audit chain: ``{connection, entity set, key
predicate, ETag observed, payload content-hash, HTTP status}``. Because the
receipt is anchored as an ``odata.writeback_receipt`` event, ``bernstein audit
verify`` covers it with no new verb -- a tampered row breaks the chain, and an
auditor holding the sent body re-hashes it and matches the recorded payload
hash.

Draft-enabled objects (create-draft -> patch -> bound activate action) are
supported through the connection's ``draft_flow`` flag and activate-action name,
because several suites gate writes behind draft promotion.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.security.audit_chain import record_odata_writeback
from bernstein.core.trigger_sources.odata_poll import (
    OdataConflict,
    OdataError,
    OdataHttpClient,
    build_key_predicate,
    discover_keys,
    key_signature,
)

if TYPE_CHECKING:
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.trigger_sources.odata_poll import Clock, OdataConnection

logger = logging.getLogger(__name__)

__all__ = [
    "WriteBackReceipt",
    "canonical_payload_hash",
    "update_entity",
]


@dataclass(frozen=True)
class WriteBackReceipt:
    """Content-addressable proof of one OData write-back.

    Attributes:
        connection: Connection label.
        entity_set: Entity set written to.
        entity_key: Canonical key predicate inner text (e.g. ``id=1``).
        etag_observed: The ``If-Match`` ETag the PATCH was gated on.
        payload_content_hash: ``sha256:`` digest of the canonical sent payload.
        http_status: HTTP status returned by the write.
        audit_event_hmac: HMAC of the anchored ``odata.writeback_receipt`` event
            -- the chain position the receipt is bound to.
        draft_flow: Whether the write went through a draft-activate flow.
        activate_action: The bound activate action name (draft flow only).
    """

    connection: str
    entity_set: str
    entity_key: str
    etag_observed: str
    payload_content_hash: str
    http_status: int
    audit_event_hmac: str
    draft_flow: bool = False
    activate_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping (stable field order)."""
        return {
            "connection": self.connection,
            "entity_set": self.entity_set,
            "entity_key": self.entity_key,
            "etag_observed": self.etag_observed,
            "payload_content_hash": self.payload_content_hash,
            "http_status": self.http_status,
            "audit_event_hmac": self.audit_event_hmac,
            "draft_flow": self.draft_flow,
            "activate_action": self.activate_action,
        }

    def receipt_hash(self) -> str:
        """Return the content-addressed identifier for this receipt."""
        body = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(body).hexdigest()


def canonical_payload_hash(patch: dict[str, Any]) -> str:
    """Return the ``sha256:`` content hash of ``patch`` (canonical JSON).

    The canonical form is sorted-key, compact-separator JSON, so the same
    payload always produces the same hash and an auditor can recompute it from
    the sent body independently.
    """
    body = json.dumps(patch, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def update_entity(
    connection: OdataConnection,
    key: dict[str, Any],
    patch: dict[str, Any],
    *,
    chain: AuditChainStore,
    http_client: OdataHttpClient | None = None,
    clock: Clock | None = None,
    actor: str = "odata_writeback",
) -> WriteBackReceipt:
    """Write ``patch`` back to an OData entity and anchor a signed receipt.

    Non-draft path: GET the current entity + ETag, then ``If-Match`` PATCH.
    A missing ETag raises :class:`OdataConflict` (428) before any blind write;
    a stale ETag surfaces the service's ``412`` as :class:`OdataConflict`.

    Draft path (``connection.draft_flow``): create a draft, PATCH it under its
    ETag, then POST the bound activate action.

    Args:
        connection: The OData connection.
        key: Key property -> value mapping identifying the entity (ignored for
            the create step of a draft flow).
        patch: The field changes to write.
        chain: Audit chain the write-back receipt is anchored into.
        http_client: Optional pre-built client (tests inject one bound to the
            in-process fake).
        clock: Optional injected clock for rate-limit waits.
        actor: Recorded audit actor.

    Returns:
        A :class:`WriteBackReceipt` bound to the recorded audit event.

    Raises:
        OdataConflict: On a 412 (stale ETag) or 428 (missing precondition).
        OdataError: On any other HTTP failure.
    """
    client = http_client or OdataHttpClient(connection, clock=clock)
    payload_hash = canonical_payload_hash(patch)

    if connection.draft_flow:
        return _draft_update(connection, patch, client=client, chain=chain, payload_hash=payload_hash, actor=actor)

    predicate = build_key_predicate(connection.entity_set, key)
    entity_key = key_signature(key)

    _entity, etag = client.get_entity(predicate)
    if not etag:
        # No fresh concurrency token: refuse before writing so a concurrent
        # human edit is never clobbered. This is the 428 path.
        raise OdataConflict(f"no fresh ETag for {predicate}; refusing blind write", status=428)

    status, _body, _new_etag = client.patch_entity(predicate, patch, if_match=etag)

    event = record_odata_writeback(
        chain=chain,
        connection_name=connection.name,
        entity_set=connection.entity_set,
        entity_key=entity_key,
        etag_observed=etag,
        payload_content_hash=payload_hash,
        http_status=status,
        actor=actor,
    )
    return WriteBackReceipt(
        connection=connection.name,
        entity_set=connection.entity_set,
        entity_key=entity_key,
        etag_observed=etag,
        payload_content_hash=payload_hash,
        http_status=status,
        audit_event_hmac=event.hmac,
    )


def _draft_update(
    connection: OdataConnection,
    patch: dict[str, Any],
    *,
    client: OdataHttpClient,
    chain: AuditChainStore,
    payload_hash: str,
    actor: str,
) -> WriteBackReceipt:
    """Create-draft -> patch -> activate, then anchor a signed receipt."""
    activate_action = connection.draft_activate_action
    if not activate_action:
        raise OdataError("draft_flow requires draft_activate_action to be configured")

    key_names = tuple(connection.key_properties) or discover_keys(connection, client)

    # 1. Create the draft.
    _status_c, draft_body, draft_etag = client.post(connection.entity_set, {})
    if draft_body is None:
        raise OdataError("draft create returned no body")
    draft_key = {name: draft_body.get(name) for name in key_names}
    draft_predicate = build_key_predicate(connection.entity_set, draft_key)
    if not draft_etag:
        raise OdataConflict(f"draft {draft_predicate} exposed no ETag", status=428)

    # 2. Patch the draft under its ETag.
    client.patch_entity(draft_predicate, patch, if_match=draft_etag)

    # 3. Activate the draft via the bound action.
    status_a, active_body, _active_etag = client.post(f"{draft_predicate}/{activate_action}")
    active_key = draft_key
    if active_body is not None:
        active_key = {name: active_body.get(name, draft_key.get(name)) for name in key_names}
    entity_key = key_signature(active_key)

    event = record_odata_writeback(
        chain=chain,
        connection_name=connection.name,
        entity_set=connection.entity_set,
        entity_key=entity_key,
        etag_observed=draft_etag,
        payload_content_hash=payload_hash,
        http_status=status_a,
        draft_flow=True,
        activate_action=activate_action,
        actor=actor,
    )
    return WriteBackReceipt(
        connection=connection.name,
        entity_set=connection.entity_set,
        entity_key=entity_key,
        etag_observed=draft_etag,
        payload_content_hash=payload_hash,
        http_status=status_a,
        audit_event_hmac=event.hmac,
        draft_flow=True,
        activate_action=activate_action,
    )
