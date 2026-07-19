"""MCP elicitation + A2A input-required routing into approval cards (#2511)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from bernstein.core.approval.card_gate import ApprovalCardGate
from bernstein.core.approval.card_inbound import (
    A2AInputRequiredRouter,
    ElicitationApprovalRouter,
)
from bernstein.core.protocols.mcp.mcp_elicitation import (
    ElicitationHandler,
    ElicitationRequest,
    ElicitationStatus,
)
from bernstein.core.security.audit_chain import (
    EVENT_APPROVAL_CARD_ISSUED,
    EVENT_APPROVAL_CARD_RESOLVED,
    AuditChainStore,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.chat.bridge import PendingApproval

_KEY = b"deterministic-test-key-2511"


class _RecordingBridge:
    """Minimal bridge capturing push_approval payloads."""

    platform = "fake"

    def __init__(self) -> None:
        self.pushed: list[PendingApproval] = []

    async def push_approval(self, approval: PendingApproval) -> str:
        self.pushed.append(approval)
        return f"msg-{len(self.pushed)}"


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=_KEY)


# ---------------------------------------------------------------------------
# MCP elicitation -> card (AC: elicitation with no auto-policy -> v2 card)
# ---------------------------------------------------------------------------


def test_unmatched_elicitation_produces_card_on_thread(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    gate = ApprovalCardGate(chain)
    handler = ElicitationHandler()
    bridge = _RecordingBridge()
    router = ElicitationApprovalRouter(handler=handler, gate=gate, bridge=bridge, thread_id="C42", worktree_id="wt-a")

    request = ElicitationRequest(
        id="e1",
        server_name="github",
        message="Confirm delete branch main?",
        request_type="confirmation",
    )

    issued = asyncio.run(router.route(request, now=1_000.0))
    assert issued is not None
    # A card was delivered to the bound thread carrying the hashed envelope.
    assert len(bridge.pushed) == 1
    payload = bridge.pushed[0]
    assert payload.thread_id == "C42"
    assert payload.card is not None
    assert payload.card_hash == issued.card_hash
    # The issue event is chain-recorded.
    events = chain.query(event_type=EVENT_APPROVAL_CARD_ISSUED)
    assert len(events) == 1
    assert events[0].details["card_hash"] == issued.card_hash


def test_auto_resolved_elicitation_produces_no_card(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    gate = ApprovalCardGate(chain)
    handler = ElicitationHandler()
    handler.add_auto_policy("auto-confirm", pattern="confirm", response="yes")
    bridge = _RecordingBridge()
    router = ElicitationApprovalRouter(handler=handler, gate=gate, bridge=bridge, thread_id="C42", worktree_id="wt-a")

    request = ElicitationRequest(
        id="e2", server_name="github", message="Confirm safe read?", request_type="confirmation"
    )
    issued = asyncio.run(router.route(request, now=1_000.0))
    assert issued is None
    assert bridge.pushed == []
    assert chain.query(event_type=EVENT_APPROVAL_CARD_ISSUED) == []


def test_elicitation_response_equals_decision_and_is_chain_linked(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    gate = ApprovalCardGate(chain)
    handler = ElicitationHandler()
    router = ElicitationApprovalRouter(handler=handler, gate=gate, thread_id="C42", worktree_id="wt-a")

    request = ElicitationRequest(id="e3", server_name="github", message="Provide a branch name", request_type="input")
    issued = asyncio.run(router.route(request, now=1_000.0))
    assert issued is not None

    _, resolved = router.resolve(
        request_id="e3", card_hash=issued.card_hash, decision="approve", approver="U7", now=1_100.0
    )
    assert resolved is not None
    # The elicitation response equals the operator decision.
    assert resolved.response == "approve"
    assert resolved.status is ElicitationStatus.USER_RESOLVED
    # The issue and resolved events share the card_hash: chain-linked.
    resolved_events = chain.query(event_type=EVENT_APPROVAL_CARD_RESOLVED)
    assert len(resolved_events) == 1
    assert resolved_events[0].details["card_hash"] == issued.card_hash
    ok, errors = chain.verify()
    assert ok, errors


# ---------------------------------------------------------------------------
# A2A input-required -> card
# ---------------------------------------------------------------------------


def test_a2a_input_required_produces_card_and_resolves(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    gate = ApprovalCardGate(chain)
    bridge = _RecordingBridge()
    router = A2AInputRequiredRouter(gate=gate, bridge=bridge, thread_id="C42", worktree_id="wt-a", peer="planner")

    issued = asyncio.run(
        router.route(task_uuid="task-123", message="Need the deploy target region", now=1_000.0),
    )
    assert len(bridge.pushed) == 1
    assert bridge.pushed[0].card_hash == issued.card_hash

    resolved = router.resolve(card_hash=issued.card_hash, decision="reject", approver="U7", now=1_050.0)
    assert resolved.card_hash == issued.card_hash
    assert chain.query(event_type=EVENT_APPROVAL_CARD_RESOLVED)
