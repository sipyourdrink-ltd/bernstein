"""request_id / card_hash binding for routed elicitations (issue #2651).

The router previously forwarded whatever ``(request_id, card_hash)`` pair the
caller supplied: the gate checked the hash and the handler checked the request
id, but nothing checked that the two named the *same* prompt. An operator
approving card A could therefore answer elicitation B, and a gate settlement
could commit while the handler leg silently did nothing.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from bernstein.core.approval.card_gate import ApprovalCardBindingMismatch, ApprovalCardGate
from bernstein.core.approval.card_inbound import (
    A2AInputRequiredRouter,
    ApprovalCardRequestMismatch,
    ElicitationApprovalRouter,
)
from bernstein.core.protocols.mcp.mcp_elicitation import (
    ElicitationHandler,
    ElicitationRequest,
    ElicitationStatus,
)
from bernstein.core.security.audit_chain import (
    EVENT_APPROVAL_CARD_RESOLVED,
    AuditChainStore,
)

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"deterministic-test-key-2651"


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=_KEY)


def _router(tmp_path: Path) -> tuple[ElicitationApprovalRouter, ElicitationHandler, AuditChainStore]:
    chain = _chain(tmp_path)
    handler = ElicitationHandler()
    router = ElicitationApprovalRouter(
        handler=handler,
        gate=ApprovalCardGate(chain),
        thread_id="C42",
        worktree_id="wt-a",
    )
    return router, handler, chain


def _request(request_id: str) -> ElicitationRequest:
    return ElicitationRequest(
        id=request_id,
        server_name="github",
        message=f"Provide a value for {request_id}",
        request_type="input",
    )


def test_mismatched_request_id_and_card_hash_is_refused(tmp_path: Path) -> None:
    router, handler, chain = _router(tmp_path)
    first = asyncio.run(router.route(_request("e1"), now=1_000.0))
    second = asyncio.run(router.route(_request("e2"), now=1_000.0))
    assert first is not None
    assert second is not None
    assert first.card_hash != second.card_hash

    # Approving card e2 while naming request e1 must not settle either.
    with pytest.raises(ApprovalCardRequestMismatch):
        router.resolve(request_id="e1", card_hash=second.card_hash, decision="approve", now=1_100.0)

    assert chain.query(event_type=EVENT_APPROVAL_CARD_RESOLVED) == []
    assert {r.id for r in handler.get_pending()} == {"e1", "e2"}


def test_unknown_request_id_is_refused_before_the_gate_commits(tmp_path: Path) -> None:
    router, _handler, chain = _router(tmp_path)
    issued = asyncio.run(router.route(_request("e1"), now=1_000.0))
    assert issued is not None

    with pytest.raises(ApprovalCardRequestMismatch):
        router.resolve(request_id="never-issued", card_hash=issued.card_hash, decision="approve", now=1_100.0)

    # The gate leg must not have committed for a pair it could not bind.
    assert chain.query(event_type=EVENT_APPROVAL_CARD_RESOLVED) == []


def test_handler_leg_is_prechecked_so_both_legs_commit_together(tmp_path: Path) -> None:
    router, handler, chain = _router(tmp_path)
    issued = asyncio.run(router.route(_request("e1"), now=1_000.0))
    assert issued is not None

    # Drain the handler behind the router's back: the handler leg can no longer
    # commit, so the gate leg must not commit either.
    assert handler.resolve("e1", "approve") is not None

    with pytest.raises(ApprovalCardRequestMismatch):
        router.resolve(request_id="e1", card_hash=issued.card_hash, decision="approve", now=1_100.0)

    assert chain.query(event_type=EVENT_APPROVAL_CARD_RESOLVED) == []


def test_matching_pair_settles_both_legs(tmp_path: Path) -> None:
    router, _handler, chain = _router(tmp_path)
    issued = asyncio.run(router.route(_request("e1"), now=1_000.0))
    assert issued is not None

    settled, resolved = router.resolve(request_id="e1", card_hash=issued.card_hash, decision="approve", now=1_100.0)
    assert settled.card_hash == issued.card_hash
    assert resolved is not None
    assert resolved.status is ElicitationStatus.USER_RESOLVED
    assert len(chain.query(event_type=EVENT_APPROVAL_CARD_RESOLVED)) == 1


def test_binding_is_consumed_so_a_replayed_pair_is_refused(tmp_path: Path) -> None:
    router, _handler, chain = _router(tmp_path)
    issued = asyncio.run(router.route(_request("e1"), now=1_000.0))
    assert issued is not None

    router.resolve(request_id="e1", card_hash=issued.card_hash, decision="approve", now=1_100.0)

    with pytest.raises(ApprovalCardRequestMismatch):
        router.resolve(request_id="e1", card_hash=issued.card_hash, decision="approve", now=1_200.0)

    assert len(chain.query(event_type=EVENT_APPROVAL_CARD_RESOLVED)) == 1


# ---------------------------------------------------------------------------
# Origin pinning is not opt-in: a router refuses to route without an origin
# ---------------------------------------------------------------------------
#
# The gate can pin a card to the worktree and conversation it was issued into,
# but that guard only fires when the issued card actually carries an origin. If
# a routing surface were allowed to construct with an empty origin, every card
# it issued would be an unpinned bearer token: whoever captured the card_hash
# could settle it from any worktree or conversation, because the gate has
# nothing to compare against. These routers are the real call paths, so they are
# where the pin has to become mandatory rather than default-off.


@pytest.mark.parametrize(
    ("worktree_id", "thread_id"),
    [("", "C42"), ("wt-a", ""), ("", "")],
)
def test_elicitation_router_refuses_to_construct_without_a_full_origin(
    tmp_path: Path,
    worktree_id: str,
    thread_id: str,
) -> None:
    """Revert-checked: fails if the constructor stops requiring a non-empty origin."""
    with pytest.raises(ValueError, match="worktree_id|thread_id"):
        ElicitationApprovalRouter(
            handler=ElicitationHandler(),
            gate=ApprovalCardGate(_chain(tmp_path)),
            worktree_id=worktree_id,
            thread_id=thread_id,
        )


@pytest.mark.parametrize(
    ("worktree_id", "thread_id"),
    [("", "C42"), ("wt-a", ""), ("", "")],
)
def test_a2a_router_refuses_to_construct_without_a_full_origin(
    tmp_path: Path,
    worktree_id: str,
    thread_id: str,
) -> None:
    """Revert-checked: fails if the constructor stops requiring a non-empty origin."""
    with pytest.raises(ValueError, match="worktree_id|thread_id"):
        A2AInputRequiredRouter(
            gate=ApprovalCardGate(_chain(tmp_path)),
            worktree_id=worktree_id,
            thread_id=thread_id,
        )


def test_a2a_card_is_pinned_and_refuses_a_foreign_worktree(tmp_path: Path) -> None:
    """End-to-end on a real call path: a card issued by one router cannot be
    settled by a router in a different worktree.

    This exercises the origin pin the gate enforces, driven entirely through the
    routing surface rather than through the gate directly, so the headline
    binding is proven present on the path a deployment actually uses.
    """
    chain = _chain(tmp_path)
    gate = ApprovalCardGate(chain)
    issuer = A2AInputRequiredRouter(gate=gate, thread_id="C42", worktree_id="wt-a", peer="planner")
    attacker = A2AInputRequiredRouter(gate=gate, thread_id="C42", worktree_id="wt-EVIL", peer="planner")

    issued = asyncio.run(issuer.route(task_uuid="task-1", message="Need a region", now=1_000.0))

    with pytest.raises(ApprovalCardBindingMismatch):
        attacker.resolve(card_hash=issued.card_hash, decision="approve", now=1_100.0)

    assert chain.query(event_type=EVENT_APPROVAL_CARD_RESOLVED) == []


def test_a2a_card_is_pinned_and_refuses_a_foreign_conversation(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    gate = ApprovalCardGate(chain)
    issuer = A2AInputRequiredRouter(gate=gate, thread_id="C42", worktree_id="wt-a", peer="planner")
    attacker = A2AInputRequiredRouter(gate=gate, thread_id="C-EVIL", worktree_id="wt-a", peer="planner")

    issued = asyncio.run(issuer.route(task_uuid="task-1", message="Need a region", now=1_000.0))

    with pytest.raises(ApprovalCardBindingMismatch):
        attacker.resolve(card_hash=issued.card_hash, decision="approve", now=1_100.0)

    assert chain.query(event_type=EVENT_APPROVAL_CARD_RESOLVED) == []
