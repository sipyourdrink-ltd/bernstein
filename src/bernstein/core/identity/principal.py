"""One identity for an agent, independent of the credential that proved it.

Three unrelated types answer "who is this agent" today:
:class:`bernstein.core.identity.agent_jwt.AgentIdentity` (JWT / opaque
bearer store), :class:`bernstein.core.identity.agent_card.AgentIdentityCard`
(Ed25519-signed capability card), and the delegation hop chain in
:mod:`bernstein.core.identity.delegation`.  They share no id space, so an
authority decision taken against one of them cannot be checked against
another: nothing answers "did the same agent do both of these things", which
is the question a delegation or incident review has to answer.

:class:`AgentPrincipal` is that one identity.  A credential is no longer an
identity - it is a :class:`CredentialRef` *of* a principal, tagged with the
format that proved it (:class:`CredentialFormat`).  Two views of the same
agent, one authenticated by JWT and one carrying a signed card, resolve to
principals with the same :attr:`AgentPrincipal.id` and can be joined with
:meth:`AgentPrincipal.merge`.

This module introduces the type and the two read-only adapters onto it.  It
does not change how either credential format is verified: the JWT signature
check stays in the store, the Ed25519 card check stays in the card signer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from bernstein.core.security.tenanting import DEFAULT_TENANT_ID, normalize_tenant_id

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from bernstein.core.identity.agent_card import AgentIdentityCard
    from bernstein.core.identity.agent_jwt import AgentIdentity

__all__ = [
    "AgentPrincipal",
    "CredentialFormat",
    "CredentialRef",
    "PrincipalStatus",
    "principal_from_agent_identity",
    "principal_from_identity_card",
    "principal_ref",
]


class CredentialFormat(StrEnum):
    """How a principal was proved, not who the principal is.

    ``OPAQUE`` and ``JWT`` are the two token kinds the agent identity store
    persists; ``ED25519_CARD`` is the signed capability card.  A principal may
    carry at most one reference per format (see
    :meth:`AgentPrincipal.with_credential`).
    """

    JWT = "jwt"
    OPAQUE = "opaque"
    ED25519_CARD = "ed25519-card"


class PrincipalStatus(StrEnum):
    """Lifecycle status of a principal.

    Spelled with the same three values as
    :class:`bernstein.core.identity.agent_jwt.AgentIdentityStatus` so a
    stored identity record maps across without a translation table.
    """

    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


#: Status precedence used by :meth:`AgentPrincipal.merge`.  Merging two views
#: of one principal keeps the most restrictive of the two: a revocation
#: recorded against either view revokes the join, so a stale active view
#: cannot launder a revoked one back into service.
_STATUS_RESTRICTIVENESS: dict[PrincipalStatus, int] = {
    PrincipalStatus.ACTIVE: 0,
    PrincipalStatus.SUSPENDED: 1,
    PrincipalStatus.REVOKED: 2,
}


@dataclass(frozen=True)
class CredentialRef:
    """A credential that proved a principal, named by format and locator.

    Attributes:
        format: Which credential format this reference describes.
        ref: The locator inside that format - the ``jti`` for a JWT, the
            SHA-256 token hash for an opaque bearer token, the ``card_hash``
            for a signed card.  Never the secret itself.
        issued_at: Unix timestamp the credential was minted.
        expires_at: Unix expiry, ``0.0`` for no expiry.
        revoked: Whether the issuing store has revoked the credential.
        algorithm: Signature algorithm, when the format carries one
            (``HS256`` for a JWT, ``EdDSA`` for a signed card).  Empty for a
            credential that carries no signature.
    """

    format: CredentialFormat
    ref: str
    issued_at: float = 0.0
    expires_at: float = 0.0
    revoked: bool = False
    algorithm: str = ""

    def is_valid_at(self, instant: float | None = None) -> bool:
        """Return True when this credential is neither revoked nor expired.

        Args:
            instant: Unix timestamp to evaluate against; the current time
                when omitted.  Explicit instants let a replay evaluate a
                credential as of the moment the decision was taken rather
                than as of now.
        """
        if self.revoked:
            return False
        now = time.time() if instant is None else instant
        return not (self.expires_at > 0 and now > self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format.value,
            "ref": self.ref,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "revoked": self.revoked,
            "algorithm": self.algorithm,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CredentialRef:
        return cls(
            format=CredentialFormat(str(data["format"])),
            ref=str(data["ref"]),
            issued_at=float(data.get("issued_at", 0.0)),
            expires_at=float(data.get("expires_at", 0.0)),
            revoked=bool(data.get("revoked", False)),
            algorithm=str(data.get("algorithm", "")),
        )


@dataclass(frozen=True)
class AgentPrincipal:
    """The single identity an authority decision is checked against.

    Attributes:
        id: Stable agent id.  Both credential adapters derive it from the
            agent's own id, so a JWT-authenticated view and a carded view of
            the same agent compare equal here - that equality is the join
            this type exists to provide.
        role: Agent role (backend, qa, security, ...).
        session_id: Spawned session this principal was minted for, when the
            source record names one.
        tenant_id: Owning tenant; never widened by a merge.
        parent_id: Principal that spawned this one, for delegation chains.
        status: Lifecycle status.
        permissions: Granted permission strings.
        credentials: At most one :class:`CredentialRef` per format.
        metadata: Source-specific detail (adapter, model, cell id, ...).
    """

    id: str
    role: str = ""
    session_id: str = ""
    tenant_id: str = DEFAULT_TENANT_ID
    parent_id: str | None = None
    status: PrincipalStatus = PrincipalStatus.ACTIVE
    permissions: frozenset[str] = frozenset()
    credentials: tuple[CredentialRef, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("AgentPrincipal id must not be blank")
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))

    @property
    def is_active(self) -> bool:
        return self.status is PrincipalStatus.ACTIVE

    def has_permission(self, permission: str) -> bool:
        """Return True when this principal is active and holds *permission*."""
        return self.is_active and permission in self.permissions

    def credential_for(self, credential_format: CredentialFormat) -> CredentialRef | None:
        """Return the reference for *credential_format*, or None when absent."""
        for credential in self.credentials:
            if credential.format is credential_format:
                return credential
        return None

    def with_credential(self, credential: CredentialRef) -> AgentPrincipal:
        """Return a copy carrying *credential*, replacing any of the same format.

        A principal holds one reference per format, so a rotated token
        replaces its predecessor rather than accumulating beside it - two
        live references of one format would leave "which one authorised
        this" unanswerable.
        """
        kept = tuple(existing for existing in self.credentials if existing.format is not credential.format)
        return AgentPrincipal(
            id=self.id,
            role=self.role,
            session_id=self.session_id,
            tenant_id=self.tenant_id,
            parent_id=self.parent_id,
            status=self.status,
            permissions=self.permissions,
            credentials=(*kept, credential),
            metadata=dict(self.metadata),
        )

    def merge(self, other: AgentPrincipal) -> AgentPrincipal:
        """Join two views of the same principal.

        The receiver is the base: its non-empty fields win, ``other`` fills
        what the receiver leaves blank, and credential formats the receiver
        lacks are added.  Status is the more restrictive of the two.

        Permissions are unioned rather than intersected because each view
        reports only what its own source records - a card carries none at
        all, so intersecting would strip an authenticated identity of every
        permission it holds.  Revocation is the direction that must not be
        lost, and the status rule above is what carries it.

        Args:
            other: Another view of the same principal.

        Raises:
            ValueError: The two views name different principals, or belong to
                different tenants - a principal that spanned tenants would
                make every tenant-scoped authority check meaningless.
        """
        if self.id != other.id:
            raise ValueError(f"cannot merge principals with different ids: {self.id!r} and {other.id!r}")
        if (
            self.tenant_id != other.tenant_id
            and self.tenant_id != DEFAULT_TENANT_ID
            and other.tenant_id != DEFAULT_TENANT_ID
        ):
            raise ValueError(
                f"cannot merge principal {self.id!r} across tenants: {self.tenant_id!r} and {other.tenant_id!r}"
            )
        credentials = list(self.credentials)
        held = {credential.format for credential in self.credentials}
        credentials.extend(credential for credential in other.credentials if credential.format not in held)
        metadata = dict(other.metadata)
        metadata.update(self.metadata)
        return AgentPrincipal(
            id=self.id,
            role=self.role or other.role,
            session_id=self.session_id or other.session_id,
            tenant_id=self.tenant_id if self.tenant_id != DEFAULT_TENANT_ID else other.tenant_id,
            parent_id=self.parent_id or other.parent_id,
            status=max(self.status, other.status, key=lambda value: _STATUS_RESTRICTIVENESS[value]),
            permissions=self.permissions | other.permissions,
            credentials=tuple(credentials),
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "parent_id": self.parent_id,
            "status": self.status.value,
            "permissions": sorted(self.permissions),
            "credentials": [credential.to_dict() for credential in self.credentials],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AgentPrincipal:
        raw_credentials: Iterable[Mapping[str, Any]] = data.get("credentials", ())
        return cls(
            id=str(data["id"]),
            role=str(data.get("role", "")),
            session_id=str(data.get("session_id", "")),
            tenant_id=str(data.get("tenant_id", DEFAULT_TENANT_ID)),
            parent_id=data.get("parent_id") or None,
            status=PrincipalStatus(str(data.get("status", PrincipalStatus.ACTIVE.value))),
            permissions=frozenset(str(item) for item in data.get("permissions", ())),
            credentials=tuple(CredentialRef.from_dict(item) for item in raw_credentials),
            metadata=dict(data.get("metadata", {})),
        )


def principal_from_agent_identity(identity: AgentIdentity) -> AgentPrincipal:
    """Project a stored agent identity record onto the one principal type.

    The record's bearer credential becomes a :class:`CredentialRef` in the
    format the record declares.  A JWT is referenced by its ``jti`` (the
    claim a verifier can revoke against) and an opaque token by its stored
    hash; neither reference carries the secret.

    Args:
        identity: A record from the agent identity store.
    """
    credential = identity.credential
    credentials: tuple[CredentialRef, ...] = ()
    tenant_id = DEFAULT_TENANT_ID
    if credential is not None:
        is_jwt = credential.token_type == "jwt"
        credential_format = CredentialFormat.JWT if is_jwt else CredentialFormat.OPAQUE
        credentials = (
            CredentialRef(
                format=credential_format,
                ref=credential.jti if is_jwt and credential.jti else credential.token_hash,
                issued_at=credential.created_at,
                expires_at=credential.expires_at,
                revoked=credential.revoked,
                algorithm=credential.algorithm if is_jwt else "",
            ),
        )
        tenant_id = credential.tenant_id
    return AgentPrincipal(
        id=identity.id,
        role=identity.role,
        session_id=identity.session_id,
        tenant_id=tenant_id,
        parent_id=identity.parent_identity_id,
        status=PrincipalStatus(identity.status.value),
        permissions=frozenset(identity.permissions),
        credentials=credentials,
        metadata=dict(identity.metadata),
    )


def principal_from_identity_card(card: AgentIdentityCard) -> AgentPrincipal:
    """Project a signed agent identity card onto the one principal type.

    The card is referenced by its ``card_hash`` - the same content address
    the spawn anchor binds into the audit chain - so a principal reached
    through a card can be joined back to the anchored attestation.  A card
    with no detached signature declares no algorithm: it identifies the agent
    but proves nothing on its own.

    Args:
        card: A capability card, signed or not.
    """
    algorithm = card.signatures[0].alg if card.signatures else ""
    credential = CredentialRef(
        format=CredentialFormat.ED25519_CARD,
        ref=card.card_hash,
        issued_at=card.created_at,
        expires_at=card.expires_at,
        algorithm=algorithm,
    )
    return AgentPrincipal(
        id=card.agent_id,
        role=card.role,
        credentials=(credential,),
        metadata={"adapter": card.adapter, "model": card.model},
    )


def principal_ref(principal: AgentPrincipal | str) -> str:
    """Return the id an audit record names for *principal*.

    Audit writers accept either an :class:`AgentPrincipal` or a bare id
    string: the delegation ledger records hops for agents *and* for parties
    that hold no credential at all ("operator", "cli"), and those have an id
    but no principal to resolve.  Routing both through this function keeps a
    caller that holds a principal from recording anything except its
    :attr:`AgentPrincipal.id`, so a hop written from an authenticated agent
    lands in the same id space it is read back in.

    Args:
        principal: A principal, or an id already in that id space.
    """
    return principal if isinstance(principal, str) else principal.id
