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


class ApprovalPrincipalRequired(ValueError):
    """Raised when a resolve path is given no usable principal.

    Covers an absent principal, a blank identifier or authentication method,
    and a principal whose identifier contradicts its :class:`PrincipalKind`.
    Every case is the same defect: a decision about to be signed with nobody's
    name on it, or with a name that misrepresents what made it.
    """


class PrincipalKind(StrEnum):
    """Whether a resolution was made by a person or by the server itself.

    Attributes:
        HUMAN: A person, authenticated by some method, decided.
        SYNTHETIC: The process decided on its own -- TTL eviction or the
            sweeper. Recorded explicitly rather than omitted, because an
            absent principal reads as an unrecorded human one.
    """

    HUMAN = "human"
    SYNTHETIC = "synthetic"


#: Namespace reserved for synthetic principals. A human principal may not use
#: it and a synthetic one must: the prefix is what lets a reader who sees only
#: the identifier string tell a sweeper's rejection from a person's.
SYNTHETIC_IDENTIFIER_PREFIX = "system:"

#: Authentication method recorded for a synthetic principal. It is not an
#: authentication at all, and says so, rather than borrowing a method name a
#: person could also have used.
AUTH_METHOD_SERVER_INTERNAL = "server-internal"


@dataclass(frozen=True)
class ApprovalPrincipal:
    """The party a resolution is attributed to.

    Human oversight is the one fact in an approval record that cannot be
    inferred from anything else the chain holds, so it is carried as a
    structured value rather than a free-form string: who, how they proved they
    were themselves, and under which session or grant they acted.

    Attributes:
        identifier: The person or component the decision is attributed to.
            Must be non-blank; synthetic principals live under
            :data:`SYNTHETIC_IDENTIFIER_PREFIX` and human ones may not.
        auth_method: How the identifier was established (``scoped-token``,
            ``oidc``, ``local-shell`` ...). Must be non-blank.
        kind: Whether a person or the server itself decided.
        grant: The session, token id, or grant the principal acted under.
            Empty when the surface has none to name.
    """

    identifier: str
    auth_method: str
    kind: PrincipalKind = PrincipalKind.HUMAN
    grant: str = ""

    def __post_init__(self) -> None:
        """Refuse a principal that names nobody, or misnames what it is."""
        if not self.identifier.strip():
            raise ApprovalPrincipalRequired("approval principal identifier must be non-blank")
        if not self.auth_method.strip():
            raise ApprovalPrincipalRequired(
                f"approval principal {self.identifier!r} must state how it was authenticated",
            )
        reserved = self.identifier.startswith(SYNTHETIC_IDENTIFIER_PREFIX)
        if self.kind is PrincipalKind.SYNTHETIC and not reserved:
            raise ApprovalPrincipalRequired(
                f"synthetic principal {self.identifier!r} must live under "
                f"{SYNTHETIC_IDENTIFIER_PREFIX!r} so it cannot be read as a person",
            )
        if self.kind is PrincipalKind.HUMAN and reserved:
            raise ApprovalPrincipalRequired(
                f"human principal {self.identifier!r} may not claim the reserved "
                f"{SYNTHETIC_IDENTIFIER_PREFIX!r} namespace",
            )

    @property
    def is_human(self) -> bool:
        """Return ``True`` when a person made this decision."""
        return self.kind is PrincipalKind.HUMAN

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-serialisable representation."""
        return {
            "identifier": self.identifier,
            "auth_method": self.auth_method,
            "kind": self.kind.value,
            "grant": self.grant,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalPrincipal:
        """Build a principal from a JSON-decoded mapping.

        A record that cannot produce a valid principal raises rather than
        rehydrating a blank one: a stored resolution whose principal did not
        survive the round trip is not attributable, and pretending otherwise
        is the defect this type exists to close.
        """
        return cls(
            identifier=str(data.get("identifier", "")),
            auth_method=str(data.get("auth_method", "")),
            kind=PrincipalKind(str(data.get("kind", PrincipalKind.HUMAN.value))),
            grant=str(data.get("grant", "")),
        )


def internal_principal(component: str) -> ApprovalPrincipal:
    """Return the synthetic principal for a server-internal resolution.

    Args:
        component: The part of the server that decided, e.g.
            ``approval-queue/sweeper``. Recorded under the reserved namespace
            so an auditor can separate it from every human decision without
            consulting anything outside the record.
    """
    return ApprovalPrincipal(
        identifier=f"{SYNTHETIC_IDENTIFIER_PREFIX}{component}",
        auth_method=AUTH_METHOD_SERVER_INTERNAL,
        kind=PrincipalKind.SYNTHETIC,
    )


def local_shell_principal() -> ApprovalPrincipal:
    """Return the principal for a decision made at a local shell or the TUI.

    The operator's authority here is possession of the shell, so that is what
    is recorded as the authentication method rather than a stronger-sounding
    one. An environment that cannot name its own user raises instead of
    falling back to a placeholder: an unattributable decision is the state
    this type exists to refuse, and the CLI surfaces the error.

    Raises:
        ApprovalPrincipalRequired: When the OS provides no user name.
    """
    import getpass

    try:
        user = getpass.getuser()
    except (OSError, KeyError):  # pragma: no cover - no passwd entry
        user = ""
    if not user.strip():  # pragma: no cover - no passwd entry
        raise ApprovalPrincipalRequired(
            "cannot attribute this approval: the operating system provides no user name",
        )
    return ApprovalPrincipal(identifier=f"os-user:{user}", auth_method="local-shell")


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
        principal: The party the decision is attributed to. Required, and
            keyword-only so it cannot be filled in positionally by accident.
            A resolution that cannot name its decider is not a record of human
            oversight, so there is no default to fall back to.
        reason: Optional free-form note supplied by the operator.
        resolved_at: Unix epoch seconds at which the decision was made.
    """

    approval_id: str
    decision: ApprovalDecision
    principal: ApprovalPrincipal = field(kw_only=True)
    reason: str = ""
    resolved_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """Refuse a resolution whose principal is absent or of the wrong type."""
        # The annotation says this cannot happen; the check is for the
        # dynamic call sites the annotation does not reach.
        if not isinstance(self.principal, ApprovalPrincipal):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ApprovalPrincipalRequired(
                f"resolution of {self.approval_id!r} needs an ApprovalPrincipal, got {type(self.principal).__name__}",
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation including the principal."""
        return {
            "approval_id": self.approval_id,
            "decision": self.decision.value,
            "reason": self.reason,
            "resolved_at": self.resolved_at,
            "principal": self.principal.to_dict(),
        }
