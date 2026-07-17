"""Route server-initiated prompts into the approval card v2 pipeline.

Issue #2511. Two server-initiated prompt surfaces previously bypassed the
attested approval path:

* **MCP elicitations.** ``elicitation/create`` requests were auto-resolved by
  policy or queued in a separate pending list, and never rendered as chat
  cards.
* **A2A ``input-required``.** A cross-agent task entering ``input-required``
  mapped to a blocked task state with no operator-facing prompt.

This module maps both onto :class:`ApprovalCardV2`: a server-initiated prompt
that no auto-resolve policy matches becomes a hash-committed card that
inherits the whole discipline -- committed decision context, chain-side
expiry, and the audit trail -- without any extra integration work on the MCP
server or peer agent side.

The elicitation response equals the operator's decision, and the issue and
resolve events share the ``card_hash``, so the elicitation answer and the
approval record are chain-linked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bernstein.core.approval.card import ApprovalCardV2, build_card

if TYPE_CHECKING:
    from bernstein.core.approval.card_gate import ApprovalCardGate, IssuedCard
    from bernstein.core.chat.bridge import BridgeProtocol, PendingApproval
    from bernstein.core.protocols.mcp.mcp_elicitation import (
        ElicitationHandler,
        ElicitationRequest,
    )

__all__ = [
    "DEFAULT_CARD_TTL_SECONDS",
    "A2AInputRequiredRouter",
    "ElicitationApprovalRouter",
    "card_for_a2a_input_required",
    "card_for_elicitation",
]

#: Default lifetime for a card issued from a server-initiated prompt.
DEFAULT_CARD_TTL_SECONDS = 600.0


def card_for_elicitation(
    request: ElicitationRequest,
    *,
    created_at: float,
    ttl_seconds: float = DEFAULT_CARD_TTL_SECONDS,
) -> ApprovalCardV2:
    """Build a v2 card from an MCP elicitation request.

    The tool name is namespaced by the originating server, the arguments
    digest binds the exact prompt (message, request type, schema), and the
    stated intent is the server's message.
    """
    tool_args: dict[str, Any] = {
        "message": request.message,
        "request_type": request.request_type,
        "schema": request.schema,
    }
    return build_card(
        approval_id=f"elicit-{request.id}",
        tool_name=f"mcp.elicitation/{request.server_name}",
        tool_args=tool_args,
        reasoning=request.message,
        created_at=created_at,
        ttl_seconds=ttl_seconds,
    )


def card_for_a2a_input_required(
    *,
    task_uuid: str,
    message: str,
    created_at: float,
    ttl_seconds: float = DEFAULT_CARD_TTL_SECONDS,
    peer: str = "",
) -> ApprovalCardV2:
    """Build a v2 card from an A2A task that entered ``input-required``."""
    tool_args: dict[str, Any] = {
        "task_uuid": task_uuid,
        "message": message,
        "state": "input-required",
        "peer": peer,
    }
    return build_card(
        approval_id=f"a2a-{task_uuid}",
        tool_name=f"a2a.input_required/{peer}" if peer else "a2a.input_required",
        tool_args=tool_args,
        reasoning=message,
        created_at=created_at,
        ttl_seconds=ttl_seconds,
    )


class ElicitationApprovalRouter:
    """Route unresolved MCP elicitations into the approval card pipeline.

    Args:
        handler: The elicitation handler; its auto-resolve policies run first.
        gate: The chain-anchored approval card gate.
        bridge: Optional chat bridge to deliver the card on. When set the card
            is pushed to ``thread_id`` as a :class:`PendingApproval` carrying
            the hashed envelope.
        thread_id: Chat thread the card is delivered on.
        worktree_id: Worktree the card is pinned to.
    """

    def __init__(
        self,
        *,
        handler: ElicitationHandler,
        gate: ApprovalCardGate,
        bridge: BridgeProtocol | None = None,
        thread_id: str = "",
        worktree_id: str = "",
    ) -> None:
        self._handler = handler
        self._gate = gate
        self._bridge = bridge
        self._thread_id = thread_id
        self._worktree_id = worktree_id

    async def route(
        self,
        request: ElicitationRequest,
        *,
        now: float,
        ttl_seconds: float = DEFAULT_CARD_TTL_SECONDS,
    ) -> IssuedCard | None:
        """Handle an elicitation; issue and deliver a card if unresolved.

        Returns:
            The :class:`IssuedCard` when the elicitation matched no auto-policy
            (a card was issued and, if a bridge is configured, delivered), or
            ``None`` when an auto-policy resolved it (no card needed).
        """
        from bernstein.core.protocols.mcp.mcp_elicitation import ElicitationStatus

        processed = self._handler.handle(request)
        if processed.status is not ElicitationStatus.PENDING:
            return None

        card = card_for_elicitation(request, created_at=now, ttl_seconds=ttl_seconds)
        issued = self._gate.issue(card, worktree_id=self._worktree_id, thread_id=self._thread_id)
        if self._bridge is not None:
            await self._bridge.push_approval(self._pending_payload(request, issued))
        return issued

    def _pending_payload(self, request: ElicitationRequest, issued: IssuedCard) -> PendingApproval:
        from bernstein.core.chat.bridge import PendingApproval

        return PendingApproval(
            approval_id=card_approval_id(issued),
            title=f"MCP elicitation from {request.server_name}",
            body=request.message,
            thread_id=self._thread_id,
            card=issued.card,
            card_hash=issued.card_hash,
        )

    def resolve(
        self,
        *,
        request_id: str,
        card_hash: str,
        decision: str,
        approver: str = "",
        now: float | None = None,
    ) -> tuple[IssuedCard, ElicitationRequest | None]:
        """Resolve a routed elicitation via the gate, then the handler.

        The gate enforces the hash echo and chain-side expiry and records the
        ``chat.approval_card.resolved`` event; the handler records the
        elicitation response, which equals the operator decision. Both are
        chain-linked through the shared ``card_hash``.
        """
        issued = self._gate.resolve(
            card_hash=card_hash,
            decision=decision,
            approver=approver,
            worktree_id=self._worktree_id,
            now=now,
        )
        resolved = self._handler.resolve(request_id, decision)
        return issued, resolved


class A2AInputRequiredRouter:
    """Issue an approval card when an A2A task enters ``input-required``.

    Mirrors :class:`ElicitationApprovalRouter` for the A2A surface: a bound
    chat thread receives the hash-committed card, and the operator's decision
    resolves it through the same gate.
    """

    def __init__(
        self,
        *,
        gate: ApprovalCardGate,
        bridge: BridgeProtocol | None = None,
        thread_id: str = "",
        worktree_id: str = "",
        peer: str = "",
    ) -> None:
        self._gate = gate
        self._bridge = bridge
        self._thread_id = thread_id
        self._worktree_id = worktree_id
        self._peer = peer

    async def route(
        self,
        *,
        task_uuid: str,
        message: str,
        now: float,
        ttl_seconds: float = DEFAULT_CARD_TTL_SECONDS,
    ) -> IssuedCard:
        """Issue and deliver a card for an A2A ``input-required`` task."""
        card = card_for_a2a_input_required(
            task_uuid=task_uuid,
            message=message,
            created_at=now,
            ttl_seconds=ttl_seconds,
            peer=self._peer,
        )
        issued = self._gate.issue(card, worktree_id=self._worktree_id, thread_id=self._thread_id)
        if self._bridge is not None:
            from bernstein.core.chat.bridge import PendingApproval

            await self._bridge.push_approval(
                PendingApproval(
                    approval_id=card.approval_id,
                    title=f"A2A input required ({self._peer or 'peer'})",
                    body=message,
                    thread_id=self._thread_id,
                    card=issued.card,
                    card_hash=issued.card_hash,
                ),
            )
        return issued

    def resolve(
        self,
        *,
        card_hash: str,
        decision: str,
        approver: str = "",
        now: float | None = None,
    ) -> IssuedCard:
        """Resolve the A2A card via the gate (hash echo + chain-side expiry)."""
        return self._gate.resolve(
            card_hash=card_hash,
            decision=decision,
            approver=approver,
            worktree_id=self._worktree_id,
            now=now,
        )


def card_approval_id(issued: IssuedCard) -> str:
    """Return the approval id carried by an issued card's envelope."""
    return issued.card.approval_id
