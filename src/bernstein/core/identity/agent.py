"""The one agent identity an authority decision is checked against.

Bernstein authenticated agents through two unrelated types. A JWT-backed
record minted by an identity store answered "who is this agent" for the task
server's auth middleware; an Ed25519-signed capability card answered the same
question for the spawn anchor and the budget countdown. Neither referenced the
other, so no query could join an authentication event to a delegation hop -
"did the same agent do both of these things" had no answer, which is exactly
what an incident review needs.

This module holds the single identity both credential formats resolve to. The
formats themselves are unchanged and stay where they are: a JWT is still minted
and verified by :mod:`bernstein.core.identity.agent_jwt`, a card is still
issued and verified by :mod:`bernstein.core.identity.agent_card`. What changes
is that the *principal* they authenticate is one type with one id space -
:class:`AgentPrincipal` - and each format contributes an
:class:`AgentCredentialRef` to it rather than being an identity of its own.

:class:`bernstein.core.identity.delegation.DelegationReceipt` records its
``issuer`` and ``subject`` in that id space, so a delegation chain and an
authentication event join on :attr:`AgentPrincipal.id`.

Nothing here imports the credential modules at runtime: the resolvers read the
attributes they need, which keeps this module free to be imported from the
delegation ledger without a cycle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Literal, get_args

if TYPE_CHECKING:
    from bernstein.core.identity.agent_card import AgentIdentityCard
    from bernstein.core.identity.agent_jwt import AgentIdentity

__all__ = [
    "CREDENTIAL_FORMATS",
    "AgentCredentialRef",
    "AgentPrincipal",
    "CredentialFormat",
    "PrincipalMismatchError",
    "merge_principals",
    "principal_from_identity_card",
    "principal_from_jwt_identity",
    "principal_ref",
]

#: Credential formats that can authenticate an :class:`AgentPrincipal`.
#:
#: ``jwt``
#:     The task-scoped bearer token held by
#:     :class:`bernstein.core.identity.agent_jwt.AgentIdentityStore`.
#: ``ed25519-card``
#:     The signed capability card issued by
#:     :mod:`bernstein.core.identity.agent_card`.
CredentialFormat = Literal["jwt", "ed25519-card"]

CREDENTIAL_FORMATS: Final[tuple[CredentialFormat, ...]] = get_args(CredentialFormat)


class PrincipalMismatchError(ValueError):
    """Two credentials were presented as one agent but name different ids.

    Raised instead of silently preferring one side: folding two id spaces
    together is the defect this module exists to prevent, so a caller holding
    credentials for two different agents must see it rather than end up with a
    principal that claims both.
    """


@dataclass(frozen=True)
class AgentCredentialRef:
    """What one credential format contributes to a principal.

    Only the non-secret handle is carried - the SHA-256 token hash for a JWT,
    the card hash for a signed card. The credential itself stays in the module
    that mints and verifies it; this is the reference an audit reader follows
    back to it.

    Attributes:
        format: Which credential format this reference came from.
        reference: Non-secret handle for the credential (token hash / card hash).
        issued_at: Unix timestamp the credential was minted (0 when unknown).
        expires_at: Unix timestamp the credential expires (0 means no expiry).
    """

    format: CredentialFormat
    reference: str
    issued_at: float = 0.0
    expires_at: float = 0.0

    def is_expired(self, now: float | None = None) -> bool:
        """Return True when this credential has an expiry that has passed."""
        if self.expires_at <= 0:
            return False
        return (time.time() if now is None else now) > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form."""
        return {
            "format": self.format,
            "reference": self.reference,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentCredentialRef:
        """Rebuild a reference from :meth:`to_dict` output."""
        fmt = str(data["format"])
        if fmt not in CREDENTIAL_FORMATS:
            msg = f"unknown credential format {fmt!r}; expected one of {list(CREDENTIAL_FORMATS)}"
            raise ValueError(msg)
        return cls(
            format=fmt,  # pyright: ignore[reportArgumentType] - checked against CREDENTIAL_FORMATS above
            reference=str(data["reference"]),
            issued_at=float(data.get("issued_at", 0.0)),
            expires_at=float(data.get("expires_at", 0.0)),
        )


@dataclass(frozen=True)
class AgentPrincipal:
    """The one identity an authority decision is checked against.

    Attributes:
        id: The agent id. This is the single id space - a delegation receipt's
            ``issuer`` / ``subject``, a JWT identity's ``id``, and a card's
            ``agent_id`` are all values of it.
        role: Agent role the credentials were issued for.
        session_id: Spawned session this principal acted in ("" when unknown).
        parent_id: Id of the principal that spawned this one, when delegated.
        credentials: One reference per credential format that authenticated
            this principal, at most one per format.
    """

    id: str
    role: str = ""
    session_id: str = ""
    parent_id: str | None = None
    credentials: tuple[AgentCredentialRef, ...] = ()

    def __post_init__(self) -> None:
        """Refuse an empty id and two references of the same format."""
        if not self.id:
            msg = "agent principal id must not be empty"
            raise ValueError(msg)
        formats = [ref.format for ref in self.credentials]
        if len(formats) != len(set(formats)):
            msg = f"principal {self.id!r} carries more than one credential per format: {formats}"
            raise ValueError(msg)

    def credential(self, fmt: CredentialFormat) -> AgentCredentialRef | None:
        """Return this principal's reference for *fmt*, or None when absent."""
        for ref in self.credentials:
            if ref.format == fmt:
                return ref
        return None

    def with_credential(self, ref: AgentCredentialRef) -> AgentPrincipal:
        """Return a copy carrying *ref*, replacing any reference of its format."""
        kept = tuple(existing for existing in self.credentials if existing.format != ref.format)
        return replace(self, credentials=(*kept, ref))

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form."""
        return {
            "id": self.id,
            "role": self.role,
            "session_id": self.session_id,
            "parent_id": self.parent_id,
            "credentials": [ref.to_dict() for ref in self.credentials],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentPrincipal:
        """Rebuild a principal from :meth:`to_dict` output."""
        raw: list[Any] = list(data.get("credentials", []))
        return cls(
            id=str(data["id"]),
            role=str(data.get("role", "")),
            session_id=str(data.get("session_id", "")),
            parent_id=data.get("parent_id"),
            credentials=tuple(AgentCredentialRef.from_dict(entry) for entry in raw),
        )


def principal_from_jwt_identity(identity: AgentIdentity) -> AgentPrincipal:
    """Resolve a JWT-store identity to the principal it authenticates."""
    credential = identity.credential
    refs: tuple[AgentCredentialRef, ...] = ()
    if credential is not None:
        refs = (
            AgentCredentialRef(
                format="jwt",
                reference=credential.token_hash,
                issued_at=credential.created_at,
                expires_at=credential.expires_at,
            ),
        )
    return AgentPrincipal(
        id=identity.id,
        role=identity.role,
        session_id=identity.session_id,
        parent_id=identity.parent_identity_id,
        credentials=refs,
    )


def principal_from_identity_card(card: AgentIdentityCard) -> AgentPrincipal:
    """Resolve a signed identity card to the principal it authenticates."""
    return AgentPrincipal(
        id=card.agent_id,
        role=card.role,
        credentials=(
            AgentCredentialRef(
                format="ed25519-card",
                reference=card.card_hash,
                issued_at=card.created_at,
                expires_at=card.expires_at,
            ),
        ),
    )


def merge_principals(left: AgentPrincipal, right: AgentPrincipal) -> AgentPrincipal:
    """Fold two resolutions of the same agent into one principal.

    This is the query the split types could not answer: given a JWT-authenticated
    caller and a carded one, either they are the same principal - and the result
    carries both credential references - or they are not, and the mismatch is
    raised rather than resolved by preference.

    Raises:
        PrincipalMismatchError: The two principals name different agent ids.
    """
    if left.id != right.id:
        msg = f"cannot merge principals for different agents: {left.id!r} != {right.id!r}"
        raise PrincipalMismatchError(msg)
    merged = AgentPrincipal(
        id=left.id,
        role=left.role or right.role,
        session_id=left.session_id or right.session_id,
        parent_id=left.parent_id if left.parent_id is not None else right.parent_id,
        credentials=left.credentials,
    )
    for ref in right.credentials:
        merged = merged.with_credential(ref)
    return merged


def principal_ref(principal: AgentPrincipal | str) -> str:
    """Return the id an audit record names for *principal*.

    Accepts a bare string so callers that already hold an agent id (the audit
    chain, CLI arguments read off disk) keep working unchanged, while callers
    holding a principal cannot record anything but its id.
    """
    return principal if isinstance(principal, str) else principal.id
