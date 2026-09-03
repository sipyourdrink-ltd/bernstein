"""Data models for interactive tool-call approval.

Defines the payload that the security-layer gate pushes onto the queue
when a tool invocation misses the always-allow rules and interactive
approvals are enabled, plus the decision primitives used by resolvers
(TUI, web, CLI).
"""

from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ApprovalTimeoutError(RuntimeError):
    """Raised when a pending approval expires before a decision arrives.

    Callers MUST treat this as an implicit reject: the tool call did not
    receive explicit operator consent, so the agent is not permitted to
    proceed. The error message is user-facing and should include the
    approval id and the tool name that timed out.
    """


class ApprovalNonceMismatch(RuntimeError):
    """Raised when an approval reply carries a nonce that does not match.

    The gate mints a 16-byte single-use nonce when a request is queued.
    The reply must echo the exact nonce; a missing, stale, replayed, or
    forged nonce raises this error so the gate refuses to resolve. The
    failure is surfaced as ``409 NONCE_MISMATCH`` over HTTP and
    ``EAPPRVL_NONCE`` over IPC.
    """


class ApprovalNonceExpired(RuntimeError):
    """Raised when the nonce belongs to a superseded or expired approval.

    The original approval may have timed out, been replaced by a newer
    request for the same target, or already been resolved. Replaying the
    old nonce surfaces this error and the gate refuses to resolve. HTTP
    callers see ``410 NONCE_EXPIRED``.
    """


class ApprovalDecision(StrEnum):
    """Operator verdict on a pending tool-call approval.

    Attributes:
        ALLOW: One-shot allow for this specific invocation.
        REJECT: Deny the tool call; the agent surfaces a permission error.
        ALWAYS: Allow and promote the tool+args pattern into the user's
            always-allow rules so future matches short-circuit the queue.
    """

    ALLOW = "allow"
    REJECT = "reject"
    ALWAYS = "always"


def _new_id() -> str:
    """Return a short, URL-safe unique approval id."""
    return f"ap-{secrets.token_hex(6)}"


#: Length of the single-use nonce minted per approval.
NONCE_BYTES: int = 16


def _new_nonce() -> bytes:
    """Return a fresh single-use 16-byte nonce."""
    return secrets.token_bytes(NONCE_BYTES)


@dataclass(frozen=True)
class PendingApproval:
    """A tool call awaiting an operator decision.

    Attributes:
        id: Unique approval identifier used in URLs and filenames.
        session_id: Bernstein session / run identifier.
        agent_role: Role of the agent that issued the tool call
            (``backend``, ``architect``, etc.).
        tool_name: Name of the tool being invoked.
        tool_args: Arguments passed to the tool. ``path``/``command``/
            ``file_path``/``query`` fields are used for pattern matching
            when the operator chooses "always allow".
        created_at: Unix epoch seconds at which the approval was queued.
        ttl_seconds: Time-to-live in seconds. After ``created_at +
            ttl_seconds`` the approval is considered expired and the
            default resolver rejects it.
        nonce: Server-generated single-use 16-byte token. The reply must
            echo this exact value or the gate refuses to resolve. Never
            written into adapter-visible state, agent stdin, or any
            rendered prompt template.
    """

    id: str = field(default_factory=_new_id)
    session_id: str = ""
    agent_role: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    ttl_seconds: int = 600
    nonce: bytes = field(default_factory=_new_nonce)

    @property
    def nonce_hex(self) -> str:
        """Return the nonce as a lowercase hex string for HTTP/SSE wire use."""
        return self.nonce.hex()

    @property
    def expires_at(self) -> float:
        """Unix epoch seconds at which this approval times out."""
        return self.created_at + float(self.ttl_seconds)

    def is_expired(self, *, now: float | None = None) -> bool:
        """Return ``True`` when the approval has passed its TTL.

        Args:
            now: Optional injected timestamp for deterministic tests.
        """
        current = time.time() if now is None else now
        return current >= self.expires_at

    def to_dict(self, *, include_nonce: bool = True) -> dict[str, Any]:
        """Return a JSON-serialisable representation.

        Args:
            include_nonce: When ``False`` the ``nonce`` key is omitted,
                so it can be used for any surface visible to agent
                processes, rendered prompts, or third-party adapters.
                The default ``True`` includes the hex-encoded nonce for
                on-disk persistence read by the human-channel resolvers.
        """
        payload = asdict(self)
        nonce_bytes = payload.pop("nonce", b"")
        if include_nonce:
            payload["nonce"] = nonce_bytes.hex() if isinstance(nonce_bytes, (bytes, bytearray)) else str(nonce_bytes)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingApproval:
        """Build a :class:`PendingApproval` from a JSON-decoded mapping.

        Unknown keys are ignored so the on-disk format can grow without
        breaking older readers. A missing ``nonce`` field rehydrates with
        a fresh nonce so legacy on-disk records load (they will still be
        rejected on resolve because the in-memory minted nonce will not
        match anything the operator can supply).
        """
        raw_nonce = data.get("nonce")
        if isinstance(raw_nonce, (bytes, bytearray)):
            nonce = bytes(raw_nonce)
        elif isinstance(raw_nonce, str) and raw_nonce:
            try:
                nonce = bytes.fromhex(raw_nonce)
            except ValueError:
                nonce = _new_nonce()
        else:
            nonce = _new_nonce()
        known = {
            "id": str(data.get("id", _new_id())),
            "session_id": str(data.get("session_id", "")),
            "agent_role": str(data.get("agent_role", "")),
            "tool_name": str(data.get("tool_name", "")),
            "tool_args": dict(data.get("tool_args", {}) or {}),
            "created_at": float(data.get("created_at", time.time())),
            "ttl_seconds": int(data.get("ttl_seconds", 600)),
            "nonce": nonce,
        }
        return cls(**known)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ResolvedApproval:
    """The outcome of resolving a :class:`PendingApproval`.

    Attributes:
        approval_id: Id of the pending approval this resolution refers to.
        decision: The operator verdict.
        reason: Optional free-form note supplied by the operator.
        resolved_at: Unix epoch seconds at which the decision was made.
    """

    approval_id: str
    decision: ApprovalDecision
    reason: str = ""
    resolved_at: float = field(default_factory=time.time)
