"""Single-use nonce binding for human-in-the-loop approvals.

Covers the gate's guarantee that an approval reply must echo the
exact 16-byte nonce minted when the prompt was queued. Mismatches and
replays are rejected with ``ApprovalNonceMismatch`` / ``ApprovalNonceExpired``
and never resolve the gate, so superseded prompts cannot be re-tapped
and the agent cannot forge its own approval.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from bernstein.core.approval.models import (
    NONCE_BYTES,
    ApprovalDecision,
    ApprovalNonceExpired,
    ApprovalNonceMismatch,
    ApprovalPrincipal,
    PendingApproval,
)
from bernstein.core.approval.queue import ApprovalQueue

#: Every resolve names the operator it is attributed to; the queue has no
#: default principal to fall back on (#5035).
_PRINCIPAL = ApprovalPrincipal(identifier="alice@example.test", auth_method="scoped-token")


def _push(queue: ApprovalQueue, *, tool: str = "shell", ttl: int = 30) -> PendingApproval:
    """Push a fresh pending approval onto *queue* for the test."""
    return queue.push(
        PendingApproval(
            session_id="S-test",
            agent_role="backend",
            tool_name=tool,
            tool_args={"command": f"echo {tool}"},
            ttl_seconds=ttl,
        )
    )


def test_minted_nonce_is_sixteen_random_bytes(tmp_path: Path) -> None:
    """Every queued approval gets a fresh 16-byte nonce."""
    queue = ApprovalQueue(base_dir=tmp_path)
    a = _push(queue, tool="a")
    b = _push(queue, tool="b")

    assert isinstance(a.nonce, bytes)
    assert len(a.nonce) == NONCE_BYTES == 16
    assert a.nonce != b.nonce
    # Hex round-trip is stable for HTTP/SSE wire use.
    assert bytes.fromhex(a.nonce_hex) == a.nonce


def test_valid_nonce_resolves_the_gate(tmp_path: Path) -> None:
    """Reply that echoes the exact nonce resolves the approval."""
    queue = ApprovalQueue(base_dir=tmp_path)
    approval = _push(queue)

    resolution = queue.resolve(
        approval.id,
        ApprovalDecision.ALLOW,
        nonce=approval.nonce,
        reason="ok",
        principal=_PRINCIPAL,
    )

    assert resolution.decision is ApprovalDecision.ALLOW
    assert queue.get(approval.id) is None  # pending entry consumed


def test_hex_nonce_string_is_accepted(tmp_path: Path) -> None:
    """The wire form (hex string) is interchangeable with raw bytes."""
    queue = ApprovalQueue(base_dir=tmp_path)
    approval = _push(queue)

    resolution = queue.resolve(
        approval.id,
        ApprovalDecision.ALLOW,
        nonce=approval.nonce_hex,
        principal=_PRINCIPAL,
    )

    assert resolution.decision is ApprovalDecision.ALLOW


def test_missing_nonce_in_reply_is_rejected(tmp_path: Path) -> None:
    """An empty / unparseable nonce string is rejected as a mismatch.

    A reply path that forgets to thread the nonce is indistinguishable
    from a forged reply; the gate must refuse to resolve either way.
    """
    queue = ApprovalQueue(base_dir=tmp_path)
    approval = _push(queue)

    with pytest.raises(ApprovalNonceMismatch):
        queue.resolve(approval.id, ApprovalDecision.ALLOW, nonce="", principal=_PRINCIPAL)

    # The approval is still pending : no resolution leaked through.
    assert queue.get(approval.id) is not None
    assert queue.get_resolution(approval.id) is None


def test_forged_nonce_is_rejected(tmp_path: Path) -> None:
    """A 16-byte string the agent could plausibly generate is rejected."""
    queue = ApprovalQueue(base_dir=tmp_path)
    approval = _push(queue)

    forged = b"\x00" * NONCE_BYTES
    assert forged != approval.nonce

    with pytest.raises(ApprovalNonceMismatch):
        queue.resolve(approval.id, ApprovalDecision.ALLOW, nonce=forged, principal=_PRINCIPAL)

    assert queue.get(approval.id) is not None
    assert queue.get_resolution(approval.id) is None


def test_unparseable_hex_string_is_rejected(tmp_path: Path) -> None:
    """A nonce that is not a valid hex string is treated as a mismatch."""
    queue = ApprovalQueue(base_dir=tmp_path)
    approval = _push(queue)

    with pytest.raises(ApprovalNonceMismatch):
        queue.resolve(approval.id, ApprovalDecision.ALLOW, nonce="not-hex-zz", principal=_PRINCIPAL)

    assert queue.get(approval.id) is not None


def test_stale_nonce_after_ttl_is_rejected(tmp_path: Path) -> None:
    """Replaying a nonce after the approval expired is rejected."""
    queue = ApprovalQueue(base_dir=tmp_path)
    approval = queue.push(
        PendingApproval(
            session_id="S-test",
            agent_role="backend",
            tool_name="shell",
            tool_args={"command": "ls"},
            ttl_seconds=1,
        )
    )

    # Evict using a faked clock : the queue rejects expired entries
    # with the server-side internal path (no nonce), then the operator
    # replay must be refused.
    queue.evict_expired(now=time.time() + 3600)
    assert queue.get_resolution(approval.id) is not None

    with pytest.raises(ApprovalNonceExpired):
        queue.resolve(
            approval.id,
            ApprovalDecision.ALLOW,
            nonce=approval.nonce,
            principal=_PRINCIPAL,
        )


def test_replay_of_same_nonce_is_rejected(tmp_path: Path) -> None:
    """Same nonce played twice : the second attempt is rejected."""
    queue = ApprovalQueue(base_dir=tmp_path)
    approval = _push(queue)

    first = queue.resolve(
        approval.id,
        ApprovalDecision.ALLOW,
        nonce=approval.nonce,
        principal=_PRINCIPAL,
    )
    assert first.decision is ApprovalDecision.ALLOW

    with pytest.raises(ApprovalNonceExpired):
        queue.resolve(
            approval.id,
            ApprovalDecision.REJECT,
            nonce=approval.nonce,
            principal=_PRINCIPAL,
        )

    # The original decision stands : replay cannot flip the verdict.
    final = queue.get_resolution(approval.id)
    assert final is not None
    assert final.decision is ApprovalDecision.ALLOW


def test_superseded_approval_invalidates_nonce(tmp_path: Path) -> None:
    """A new request for the same tool replaces the old approval id.

    The old nonce belongs to the superseded request; replaying it must
    not resolve the newly-queued approval (different id, different
    nonce).
    """
    queue = ApprovalQueue(base_dir=tmp_path)
    first = _push(queue)
    # Operator cancels / agent supersedes the first request and a new
    # one is queued for the same logical target.
    queue.evict_expired(now=time.time() + 3600)

    second = _push(queue)
    assert second.id != first.id
    assert second.nonce != first.nonce

    # Replaying the first nonce against the second approval is rejected
    # by id-bound mismatch.
    with pytest.raises(ApprovalNonceMismatch):
        queue.resolve(second.id, ApprovalDecision.ALLOW, nonce=first.nonce, principal=_PRINCIPAL)

    # The second approval is still pending.
    assert queue.get(second.id) is not None


def test_nonce_persists_to_disk_for_human_channel_resolvers(tmp_path: Path) -> None:
    """On-disk JSON carries the hex nonce so the CLI/web can read it."""
    queue = ApprovalQueue(base_dir=tmp_path)
    approval = _push(queue)

    record_path = tmp_path / f"{approval.id}.json"
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    assert payload["nonce"] == approval.nonce_hex
    assert len(bytes.fromhex(payload["nonce"])) == NONCE_BYTES


def test_nonce_not_exposed_via_to_dict_when_excluded() -> None:
    """``to_dict(include_nonce=False)`` strips the nonce.

    Adapter-facing serialisations (agent stdin, prompt templates) must
    use this form so the nonce never reaches the agent process.
    """
    approval = PendingApproval(
        session_id="S",
        agent_role="backend",
        tool_name="shell",
        tool_args={"command": "ls"},
    )
    public = approval.to_dict(include_nonce=False)
    assert "nonce" not in public
    # Sanity: every other field is still there so callers do not lose
    # context they depend on.
    for key in ("id", "session_id", "agent_role", "tool_name", "tool_args"):
        assert key in public


def test_wait_for_resolves_normally_when_correct_nonce_is_supplied(tmp_path: Path) -> None:
    """End-to-end: gate awaits resolution; correct nonce wins."""
    queue = ApprovalQueue(base_dir=tmp_path)
    approval = _push(queue)

    async def scenario() -> None:
        async def resolver() -> None:
            await asyncio.sleep(0.01)
            queue.resolve(
                approval.id,
                ApprovalDecision.ALLOW,
                nonce=approval.nonce,
                principal=_PRINCIPAL,
            )

        task = asyncio.create_task(resolver())
        result = await queue.wait_for(approval.id, timeout_seconds=2.0)
        await task
        assert result.decision is ApprovalDecision.ALLOW

    asyncio.run(scenario())


def test_resolve_without_nonce_remains_back_compat_for_server_internals(tmp_path: Path) -> None:
    """Server-internal callers (timeout, evict) may resolve without a nonce.

    Only human-channel reply paths supply a nonce. The TTL-driven
    eviction path and the wait-for timeout fallback must keep working
    so the gate does not deadlock when no operator answers.
    """
    queue = ApprovalQueue(base_dir=tmp_path)
    approval = _push(queue)

    # Nonce omitted entirely: this is the back-compat / server-internal
    # path used by evict_expired and the wait_for timeout fallback.
    resolution = queue.resolve(approval.id, ApprovalDecision.REJECT, reason="server-internal", principal=_PRINCIPAL)
    assert resolution.decision is ApprovalDecision.REJECT
