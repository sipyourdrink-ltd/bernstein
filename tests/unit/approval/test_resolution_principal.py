"""An approval resolution must name the principal that made it (issue #5035).

Two approval paths recorded the deciding party differently, and neither
recorded a usable one:

* ``ApprovalCardGate.resolve`` defaulted ``approver`` to ``""`` and wrote
  ``actor=approver or "operator"`` into the HMAC-chained settlement event, so a
  caller that supplied nobody produced a signed record attributing the decision
  to a name that identifies nobody.
* ``ApprovalQueue.resolve`` had no approver parameter at all and
  ``ResolvedApproval`` had no field to hold one, so for the HTTP, TUI and CLI
  paths the deciding party was not merely defaulted -- it was never captured
  and could not be reconstructed afterwards.

These tests pin the principal as a required, structured field on every resolve
path in the package, and pin the synthetic principal used by the queue's own
TTL eviction and sweeper as distinguishable from a person.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.approval.card import ApprovalCardV2, build_card
from bernstein.core.approval.card_gate import (
    ApprovalCardGate,
    ApprovalCardMissingApprover,
)
from bernstein.core.approval.models import (
    ApprovalDecision,
    ApprovalPrincipal,
    ApprovalPrincipalRequired,
    PendingApproval,
    PrincipalKind,
)
from bernstein.core.approval.queue import ApprovalQueue
from bernstein.core.security.audit import AuditLog
from bernstein.core.security.audit_chain import (
    EVENT_APPROVAL_CARD_REFUSED,
    EVENT_APPROVAL_CARD_RESOLVED,
    AuditChainStore,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_KEY = b"deterministic-test-key-5035"

_ALICE = ApprovalPrincipal(
    identifier="alice@example.test",
    auth_method="scoped-token",
    grant="tok-7f3a",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _queue(root: Path) -> ApprovalQueue:
    return ApprovalQueue(base_dir=root / ".sdd" / "runtime" / "approvals")


def _enqueue(queue: ApprovalQueue, *, ttl_seconds: int = 600) -> PendingApproval:
    return queue.push(
        PendingApproval(
            session_id="S-1",
            agent_role="backend",
            tool_name="Bash",
            tool_args={"command": "rm -rf /tmp/x"},
            ttl_seconds=ttl_seconds,
        ),
    )


def _human_events(root: Path) -> list[Any]:
    log = AuditLog(audit_dir=root / ".sdd" / "audit")
    return [e for e in log.query() if e.event_type == "human_approval_decision"]


def _chain(root: Path) -> AuditChainStore:
    return AuditChainStore(root / "audit", key=_KEY)


def _card() -> ApprovalCardV2:
    return build_card(
        approval_id="ap-5035",
        tool_name="Bash",
        tool_args={"command": "rm -rf /var/data"},
        reasoning="Clear the stale data directory.",
        created_at=1_000.0,
        ttl_seconds=600.0,
    )


# ---------------------------------------------------------------------------
# 1. The resolution carries a principal
# ---------------------------------------------------------------------------


def test_resolved_approval_carries_a_principal(tmp_path: Path) -> None:
    """The identifier, its authentication method and its grant survive resolve.

    Not just in memory: the on-disk sentinel and the audit-chain event are the
    two artefacts an oversight review reads, so both must name the principal.
    """
    queue = _queue(tmp_path)
    pending = _enqueue(queue)

    resolution = queue.resolve(
        pending.id,
        ApprovalDecision.ALLOW,
        principal=_ALICE,
        nonce=pending.nonce,
        channel="http",
    )

    assert resolution.principal.identifier == "alice@example.test"
    assert resolution.principal.auth_method == "scoped-token"
    assert resolution.principal.grant == "tok-7f3a"
    assert resolution.principal.is_human

    sentinel = tmp_path / ".sdd" / "runtime" / "approvals" / f"{pending.id}.resolved.json"
    stored: dict[str, Any] = json.loads(sentinel.read_text(encoding="utf-8"))
    assert stored["principal"]["identifier"] == "alice@example.test"
    assert stored["principal"]["auth_method"] == "scoped-token"
    assert stored["principal"]["kind"] == "human"

    events = _human_events(tmp_path)
    assert len(events) == 1
    assert events[0].actor == "alice@example.test"
    assert events[0].details["principal_auth_method"] == "scoped-token"
    assert events[0].details["principal_kind"] == "human"


# ---------------------------------------------------------------------------
# 2. A resolution without a principal is refused
# ---------------------------------------------------------------------------


def test_queue_resolve_without_a_principal_is_refused(tmp_path: Path) -> None:
    """Omitting the principal must fail the call, not default it.

    Both shapes are covered: leaving the argument out entirely, and passing an
    explicit ``None`` from dynamic call sites that the signature cannot catch.
    """
    queue = _queue(tmp_path)
    pending = _enqueue(queue)

    with pytest.raises(TypeError):
        queue.resolve(pending.id, ApprovalDecision.ALLOW, nonce=pending.nonce)  # type: ignore[call-arg]

    with pytest.raises(ApprovalPrincipalRequired):
        queue.resolve(pending.id, ApprovalDecision.ALLOW, nonce=pending.nonce, principal=None)

    # The refusal is total: nothing was resolved, nothing was chained, and the
    # single-use nonce was not burned by the failed attempts.
    assert queue.get_resolution(pending.id) is None
    assert queue.get(pending.id) is not None
    assert _human_events(tmp_path) == []


def test_a_principal_with_a_blank_identifier_or_auth_method_is_refused() -> None:
    """An empty string is the same missing value wearing a type."""
    with pytest.raises(ApprovalPrincipalRequired):
        ApprovalPrincipal(identifier="", auth_method="scoped-token")
    with pytest.raises(ApprovalPrincipalRequired):
        ApprovalPrincipal(identifier="   ", auth_method="scoped-token")
    with pytest.raises(ApprovalPrincipalRequired):
        ApprovalPrincipal(identifier="alice@example.test", auth_method="")


# ---------------------------------------------------------------------------
# 3. Internal resolutions cannot look like a person
# ---------------------------------------------------------------------------


def test_internal_resolution_principal_is_distinguishable_from_a_human(tmp_path: Path) -> None:
    """The sweeper must not be able to pass for an operator, in either direction."""
    queue = _queue(tmp_path)
    pending = _enqueue(queue, ttl_seconds=1)

    evicted = queue.evict_expired(now=pending.created_at + 10.0)
    assert evicted == [pending.id]

    resolution = queue.get_resolution(pending.id)
    assert resolution is not None
    assert resolution.decision is ApprovalDecision.REJECT
    assert resolution.principal.kind is PrincipalKind.SYNTHETIC
    assert not resolution.principal.is_human
    assert resolution.principal.identifier.startswith("system:")
    assert resolution.principal.auth_method == "server-internal"

    events = _human_events(tmp_path)
    assert len(events) == 1
    assert events[0].details["principal_kind"] == "synthetic"

    # The two shapes are mutually exclusive by construction, so a synthetic
    # resolution cannot be relabelled as a person's and a person cannot borrow
    # the reserved namespace the sweeper writes under.
    with pytest.raises(ApprovalPrincipalRequired):
        ApprovalPrincipal(
            identifier="system:approval-queue/sweeper",
            auth_method="scoped-token",
            kind=PrincipalKind.HUMAN,
        )
    with pytest.raises(ApprovalPrincipalRequired):
        ApprovalPrincipal(
            identifier="alice@example.test",
            auth_method="server-internal",
            kind=PrincipalKind.SYNTHETIC,
        )


# ---------------------------------------------------------------------------
# 4. The card gate refuses an empty approver
# ---------------------------------------------------------------------------


def test_card_gate_refuses_an_empty_approver_instead_of_writing_operator(tmp_path: Path) -> None:
    """A missing approver must not be substituted by a plausible-looking name.

    ``actor="operator"`` is a signed attribution to nobody. The gate refuses,
    records the refusal on the chain under an actor that reads as absent rather
    than as a person, and leaves the card unsettled.
    """
    chain = _chain(tmp_path)
    gate = ApprovalCardGate(chain)
    issued = gate.issue(_card(), worktree_id="wt-a", thread_id="C42")

    with pytest.raises(ApprovalCardMissingApprover):
        gate.resolve(
            card_hash=issued.card_hash,
            decision="approve",
            approver="",
            worktree_id="wt-a",
            thread_id="C42",
            now=1_100.0,
        )

    assert chain.query(event_type=EVENT_APPROVAL_CARD_RESOLVED) == []
    refused = chain.query(event_type=EVENT_APPROVAL_CARD_REFUSED)
    assert len(refused) == 1
    assert refused[0].details["reason"] == "missing_approver"
    assert refused[0].actor != "operator"

    # No event anywhere in the chain names the placeholder.
    assert [e for e in chain.query() if e.actor == "operator"] == []

    # The card is not burned: the legitimate operator can still decide it.
    gate.resolve(
        card_hash=issued.card_hash,
        decision="approve",
        approver="alice@example.test",
        worktree_id="wt-a",
        thread_id="C42",
        now=1_100.0,
    )
    resolved = chain.query(event_type=EVENT_APPROVAL_CARD_RESOLVED)
    assert len(resolved) == 1
    assert resolved[0].actor == "alice@example.test"
    ok, errors = chain.verify()
    assert ok, errors


# ---------------------------------------------------------------------------
# 7. Every resolve path in the package requires a principal
# ---------------------------------------------------------------------------


def _resolve_paths() -> list[tuple[str, Callable[..., Any]]]:
    """Return every ``resolve`` method defined in the approval package.

    Discovery is by walking the modules rather than by listing the four known
    call sites, so a fifth resolve path added later is covered the moment it
    exists instead of the moment somebody remembers to add it here.
    """
    import bernstein.core.approval.card_gate as card_gate_mod
    import bernstein.core.approval.card_inbound as card_inbound_mod
    import bernstein.core.approval.queue as queue_mod

    found: list[tuple[str, Callable[..., Any]]] = []
    for module in (queue_mod, card_gate_mod, card_inbound_mod):
        for class_name, obj in vars(module).items():
            if not inspect.isclass(obj) or obj.__module__ != module.__name__:
                continue
            method = obj.__dict__.get("resolve")
            if callable(method):
                found.append((f"{module.__name__}.{class_name}.resolve", method))
    return found


def test_the_known_resolve_paths_are_all_discovered() -> None:
    """Guard the guard: an empty walk would make the check below vacuous."""
    names = {name for name, _ in _resolve_paths()}
    assert names == {
        "bernstein.core.approval.queue.ApprovalQueue.resolve",
        "bernstein.core.approval.card_gate.ApprovalCardGate.resolve",
        "bernstein.core.approval.card_inbound.ElicitationApprovalRouter.resolve",
        "bernstein.core.approval.card_inbound.A2AInputRequiredRouter.resolve",
    }


@pytest.mark.parametrize(("name", "method"), _resolve_paths(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_resolve_path_requires_a_principal(name: str, method: Callable[..., Any]) -> None:
    """No resolve path may record a decision without naming who made it."""
    params = inspect.signature(method).parameters
    named = [p for p in params.values() if p.name in {"principal", "approver"}]
    assert named, f"{name} records a decision without a principal parameter"
    for param in named:
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, f"{name}: {param.name} must be keyword-only"
        assert param.default is inspect.Parameter.empty, (
            f"{name}: {param.name} has default {param.default!r}; a resolve path must not "
            f"be able to substitute an unnamed decider"
        )
