"""Verifiable claim-receipt route for the task server (#2555).

``POST /tasks/claim-receipt`` drives the dependency-gated claim path
(:func:`bernstein.core.tasks.claim.claim_next_entry` plus
:class:`~bernstein.core.tasks.claim.ClaimFilter`) and returns a signed,
content-addressed :class:`~bernstein.core.protocols.mcp.claim_receipt.ClaimReceipt`
instead of the mutable task projection the older claim routes return.

The receipt is the returned projection of the same dependency snapshot the
existing ``task.claim_receipt`` audit event already records
(:func:`record_task_claim_receipt`), so this route reuses that event rather
than adding a new one. It embeds the audit-chain head the event recorded, so
a worker holds an object it can re-verify offline against the audited run. A
filter that matches no eligible row returns a signed *refusal* receipt: a
claim attempt is never a silent skip.

The atomic backlog-lock semantics are unchanged - ``claim_next_entry`` holds
the cross-thread and cross-process backlog lock for the flip - and the
operator force-claim recovery route is left untouched.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

from bernstein.core.protocols.mcp.claim_receipt import (
    ClaimReceipt,
    backlog_head,
    filter_digest,
    sign_claim_receipt,
)
from bernstein.core.routes.task_crud import _get_sse_bus
from bernstein.core.security.audit_chain import AuditChainStore, record_task_claim_receipt
from bernstein.core.security.sanitize import sanitize_log
from bernstein.core.server import ClaimReceiptRequest  # noqa: TC001  (FastAPI resolves the body model at runtime)
from bernstein.core.tasks.claim import Backlog, ClaimFilter, claim_next_entry

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

router = APIRouter()

#: Claim receipts are signed with a dedicated install identity persisted
#: alongside the mailbox identity. The receipt embeds its own public half, so
#: an offline verifier checks the signature without any key lookup.
_CLAIM_IDENTITY_PRIVATE = "claim_signing.pem"
_CLAIM_IDENTITY_PUBLIC = "claim_signing.pub"

#: The claim path used for the reused ``task.claim_receipt`` audit event.
_MCP_CLAIM_PATH = "mcp_claim"


def _get_claim_backlog_path(request: Request) -> Path:
    """Return the shared JSON backlog the claim path atomically claims from."""
    configured = getattr(request.app.state, "claim_backlog_path", None)
    if configured is not None:
        return configured  # type: ignore[no-any-return]
    runtime_dir: Path = request.app.state.runtime_dir  # type: ignore[attr-defined]
    return runtime_dir / "task-backlog.json"


def _get_claim_identity_dir(request: Request) -> Path:
    """Return the directory holding the install's claim-signing identity."""
    configured = getattr(request.app.state, "claim_identity_dir", None)
    if configured is not None:
        return configured  # type: ignore[no-any-return]
    sdd_dir: Path = request.app.state.sdd_dir  # type: ignore[attr-defined]
    return sdd_dir / "identity"


def _get_audit_chain(request: Request) -> AuditChainStore | None:
    return getattr(request.app.state, "audit_chain", None)


@router.post(
    "/tasks/claim-receipt",
    responses={
        503: {"description": "Server is draining -- no new claims accepted"},
    },
)
async def claim_receipt(body: ClaimReceiptRequest, request: Request) -> dict[str, object]:
    """Claim the next eligible backlog row and return a signed claim receipt.

    The dependency gate is enforced by :class:`ClaimFilter`: a row is offered
    only when its ``depends_on`` are all in ``completed_ids``. The granted
    claim is mirrored into the audit chain via the existing
    ``record_task_claim_receipt`` (no new event type), and the returned
    receipt embeds that event's chain head so the claim verifies offline. A
    filter matching no eligible row returns a signed refusal receipt.
    """
    if request.app.state.draining:  # type: ignore[attr-defined]
        raise HTTPException(status_code=503, detail="Server is draining -- no new claims accepted")

    backlog_path = _get_claim_backlog_path(request)
    claim_filter = ClaimFilter(
        project=body.project,
        role=body.role,
        capability=body.capability,
        completed_ids=frozenset(body.completed_ids),
        max_attempts=body.max_attempts,
    )
    fingerprint = body.claimer_card_fingerprint or "unregistered"

    entry = claim_next_entry(backlog_path, claimer_id=body.claimer_id, filter=claim_filter)

    # Reproject the post-claim backlog head from disk: wall-clock is stripped
    # inside backlog_head, so the head matches on a later offline reprojection.
    rows = [e.to_dict() for e in Backlog.load(backlog_path).entries]
    fd = filter_digest(claim_filter)
    bh = backlog_head(rows)

    chain = _get_audit_chain(request)
    if entry is None:
        chain_head = chain.prev_chain_digest if chain is not None else ""
        receipt = ClaimReceipt.refusal(
            claimer_card_fingerprint=fingerprint,
            backlog_head=bh,
            filter_digest=fd,
            chain_head=chain_head,
        )
        logger.info(
            "task.claim_receipt refused: claimer=%s role=%s (no eligible row)",
            sanitize_log(body.claimer_id),
            sanitize_log(str(body.role)),
        )
    else:
        chain_head = ""
        if chain is not None:
            try:
                event = record_task_claim_receipt(
                    chain=chain,
                    task_id=entry.id,
                    role=entry.role or "",
                    claimed_by=body.claimer_id,
                    depends_on=list(entry.depends_on),
                    task_version=entry.attempts,
                    claim_path=_MCP_CLAIM_PATH,
                )
                chain_head = str(event.details.get("prev_chain_digest", ""))
            except Exception as exc:  # intentional-broad-except: audit mirror is best-effort, never blocks the claim
                logger.warning("task.claim_receipt audit mirror failed: %s", type(exc).__name__)
        receipt = ClaimReceipt.granted_receipt(
            task_id=entry.id,
            claimer_card_fingerprint=fingerprint,
            backlog_head=bh,
            filter_digest=fd,
            chain_head=chain_head,
        )
        _get_sse_bus(request).publish(
            "task_claimed",
            json.dumps({"task_id": entry.id, "claimer": body.claimer_id, "receipt_hash": receipt.receipt_hash}),
        )
        logger.info(
            "task.claim_receipt granted: task_id=%s claimer=%s",
            sanitize_log(entry.id),
            sanitize_log(body.claimer_id),
        )

    from bernstein.core.lineage.identity import load_or_create_signing_identity

    private_pem, public_pem = load_or_create_signing_identity(
        _get_claim_identity_dir(request),
        private_name=_CLAIM_IDENTITY_PRIVATE,
        public_name=_CLAIM_IDENTITY_PUBLIC,
    )
    signed = sign_claim_receipt(receipt, private_key_pem=private_pem, public_key_pem=public_pem)
    return signed.to_wire()
