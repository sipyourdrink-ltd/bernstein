"""Two-factor approval for destructive operations.

Requires two independent approvals - from two different approvers - before
any destructive operation is allowed to proceed.  Channels include CLI
confirmation, Slack, webhook callbacks, and email.

Approval requests carry a TTL (default 300 s) after which they expire
automatically.  A single explicit denial vetoes the entire request
regardless of how many approvals have been collected.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ApprovalChannel(StrEnum):
    """Channel through which an approval can be submitted."""

    CLI = "cli"
    SLACK = "slack"
    WEBHOOK = "webhook"
    EMAIL = "email"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalRequest:
    """A pending request for approval of a destructive operation."""

    request_id: str
    operation: str
    reason: str
    requester: str
    channels: list[ApprovalChannel]
    created_at: str
    expires_at: str
    required_approvals: int = 2


@dataclass(frozen=True)
class ApprovalResponse:
    """A single approval or denial from an approver."""

    request_id: str
    channel: ApprovalChannel
    approver: str
    approved: bool
    timestamp: str


@dataclass(frozen=True)
class ApprovalStatus:
    """Evaluated status of an approval request given its responses."""

    request: ApprovalRequest
    responses: list[ApprovalResponse]
    is_approved: bool
    is_expired: bool
    is_denied: bool


# ---------------------------------------------------------------------------
# Destructive-operation patterns
# ---------------------------------------------------------------------------

DESTRUCTIVE_OPERATIONS: list[str] = [
    "git push --force",
    "database migrate",
    "production deploy",
    "git reset --hard",
    "drop table",
    "delete branch",
]


def is_destructive(operation: str) -> bool:
    """Return ``True`` if *operation* matches any known destructive pattern.

    Matching is case-insensitive substring search against each entry in
    :data:`DESTRUCTIVE_OPERATIONS`.
    """
    op_lower = operation.lower()
    return any(pattern in op_lower for pattern in DESTRUCTIVE_OPERATIONS)


# ---------------------------------------------------------------------------
# Request creation
# ---------------------------------------------------------------------------


def create_approval_request(
    operation: str,
    requester: str,
    channels: list[ApprovalChannel] | None = None,
    ttl_seconds: int = 300,
) -> ApprovalRequest:
    """Create a new :class:`ApprovalRequest`.

    Args:
        operation: Human-readable description of the operation.
        requester: Identifier of the entity requesting approval.
        channels: Channels to solicit approval from.  Defaults to
            ``[CLI, SLACK]``.
        ttl_seconds: Time-to-live in seconds before the request expires.

    Returns:
        A frozen :class:`ApprovalRequest` ready for distribution.
    """
    if channels is None:
        channels = [ApprovalChannel.CLI, ApprovalChannel.SLACK]

    now = datetime.now(tz=UTC)
    expires = now + timedelta(seconds=ttl_seconds)

    return ApprovalRequest(
        request_id=str(uuid.uuid4()),
        operation=operation,
        reason=f"Approval required for: {operation}",
        requester=requester,
        channels=channels,
        created_at=now.isoformat(),
        expires_at=expires.isoformat(),
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_approval(
    request: ApprovalRequest,
    responses: list[ApprovalResponse],
) -> ApprovalStatus:
    """Evaluate whether *request* has been approved, denied, or expired.

    Only responses whose ``request_id`` matches *request* are considered. The
    field exists to bind a response to the operation it approves; a response
    carrying a different id is evidence about a different decision.

    Rules:
    * **Denied** - any single matching response with ``approved=False`` vetoes
      the entire request.
    * **Expired** - ``expires_at`` is in the past (UTC).
    * **Approved** - approvals arrive from at least ``required_approvals``
      *distinct approvers*, and the request is neither denied nor expired.

    Approvals are counted per approver rather than per response or per
    channel. The point of this module is that a destructive operation needs
    more than one independent sign-off, and independence is a property of
    people: repeated responses are one party's say-so however many rows or
    channels they produce, while two genuine approvers who both happen to
    reply in Slack are still two.

    Args:
        request: The original approval request.
        responses: Collected approval/denial responses. Responses for other
            requests are ignored rather than rejected, so a caller may pass a
            whole inbox.

    Returns:
        An :class:`ApprovalStatus` snapshot. ``responses`` on the result is
        the full input list, unfiltered, so a caller can still see what was
        submitted.
    """
    now = datetime.now(tz=UTC)
    expires_at = datetime.fromisoformat(request.expires_at)
    is_expired = now >= expires_at

    # Only responses issued against *this* request count. ``request_id`` is on
    # ApprovalResponse to bind the two together; ignoring it let approvals
    # collected for one operation satisfy the gate on a different one.
    own = [r for r in responses if r.request_id == request.request_id]

    is_denied = any(not r.approved for r in own)

    # Counted per approver. Independence is a property of *people*, and
    # neither of the other two candidates measures it: counting rows lets one
    # approver click twice, and counting channels lets the same approver use
    # two of them - the very scenario this gate exists to stop - while
    # refusing two genuine approvers who happen to share a channel.
    approvers = {r.approver for r in own if r.approved}
    is_approved = len(approvers) >= request.required_approvals and not is_denied and not is_expired

    return ApprovalStatus(
        request=request,
        responses=responses.copy(),
        is_approved=is_approved,
        is_expired=is_expired,
        is_denied=is_denied,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_approval_prompt(request: ApprovalRequest) -> str:
    """Return a human-readable string describing the approval request.

    Suitable for display in a CLI prompt, Slack message, or email body.
    """
    channels_str = ", ".join(ch.value for ch in request.channels)
    lines = [
        "=== APPROVAL REQUIRED ===",
        f"Request ID : {request.request_id}",
        f"Operation  : {request.operation}",
        f"Reason     : {request.reason}",
        f"Requester  : {request.requester}",
        f"Channels   : {channels_str}",
        f"Approvals  : {request.required_approvals} required",
        f"Created    : {request.created_at}",
        f"Expires    : {request.expires_at}",
        "=========================",
    ]
    return "\n".join(lines)
