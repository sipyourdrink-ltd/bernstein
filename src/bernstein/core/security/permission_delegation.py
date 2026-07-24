"""Permission delegation from coordinator to workers.

Two surfaces live here:

* The legacy in-memory :class:`DelegationToken` / :class:`PermissionDelegator`
  registry - an unsigned, coarse-scope grant checked against the coordinator's
  own process state. Existing callers keep using it unchanged.
* An additive bridge onto the signed, scope-attenuating capability tokens in
  :mod:`bernstein.core.security.capability_tokens`. :func:`enum_to_caveats`
  maps the ``read``/``write``/``execute``/``full`` enum onto the ``PERM_*``
  caveat vocabulary, :meth:`PermissionDelegator.mint_capability` mints a signed
  token from that scope, and :meth:`PermissionDelegator.verify_capability`
  verifies **offline chain first** (no network, no registry) and only then
  consults the in-process registry for *liveness* (expiry) and *revocation*.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from bernstein.core.security import capability_tokens as _cap

if TYPE_CHECKING:
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.security.capability_tokens import CapabilityChain, CapabilityToken, Caveats

logger = logging.getLogger(__name__)


DelegationScope = Literal["read", "write", "execute", "full"]


def enum_to_caveats(
    scope: DelegationScope,
    *,
    remaining_depth: int,
    not_after: float,
    task_ids: set[str] | frozenset[str] | None = None,
    path_prefixes: set[str] | frozenset[str] | None = None,
    max_uses: int | None = None,
    extra_permissions: set[str] | frozenset[str] | None = None,
) -> Caveats:
    """Map a legacy delegation scope enum onto capability-token caveats.

    The enum hierarchy ``read`` < ``write`` < ``execute`` < ``full`` maps onto
    cumulative ``PERM_*`` sets, so a token minted for a narrower scope is a
    strict subset of a wider one - the enum ordering becomes capability-token
    subset attenuation with no behavioural change for existing callers.
    """
    return _cap.caveats_for_scope(
        scope,
        remaining_depth=remaining_depth,
        not_after=not_after,
        task_ids=task_ids,
        path_prefixes=path_prefixes,
        max_uses=max_uses,
        extra_permissions=extra_permissions,
    )


@dataclass
class DelegationToken:
    """Token for delegated permissions."""

    token_id: str
    parent_approval_id: str
    coordinator_id: str
    worker_id: str
    scope: DelegationScope
    granted_at: float
    expires_at: float
    permissions: list[str] = field(default_factory=list[str])
    used_count: int = 0
    max_uses: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DelegationToken:
        """Create from dictionary."""
        return cls(**data)

    def is_expired(self) -> bool:
        """Check if token has expired."""
        return time.time() > self.expires_at

    def is_exhausted(self) -> bool:
        """Check if token has been used max times."""
        if self.max_uses is None:
            return False
        return self.used_count >= self.max_uses

    def can_use(self) -> bool:
        """Check if token can be used."""
        return not self.is_expired() and not self.is_exhausted()

    def use(self) -> bool:
        """Mark token as used.

        Returns:
            True if used successfully, False if exhausted.
        """
        if not self.can_use():
            return False

        self.used_count += 1
        return True


class PermissionDelegator:
    """Manage permission delegation from coordinators to workers.

    When a coordinator (leader) spawns workers, allows delegation of
    approval context so workers inherit or reference the same approval
    flow instead of prompting humans separately.

    Features:
    - Token-based delegation with expiry
    - Scope-based permissions
    - Usage limits
    - Security boundaries

    Args:
        default_ttl_seconds: Default token TTL.
        max_uses_per_token: Default max uses per token.
    """

    def __init__(
        self,
        default_ttl_seconds: float = 3600,
        max_uses_per_token: int | None = None,
    ) -> None:
        self._default_ttl = default_ttl_seconds
        self._max_uses = max_uses_per_token
        self._tokens: dict[str, DelegationToken] = {}
        self._approvals: dict[str, dict[str, Any]] = {}
        # Signed-capability liveness registry: token hashes seen at mint, and
        # the subset that has been revoked. The registry is a *cache* consulted
        # only after the offline chain check - never the sole source of truth.
        self._capabilities: dict[str, CapabilityToken] = {}
        self._revoked_capabilities: set[str] = set()

    def register_approval(
        self,
        approval_id: str,
        scope: DelegationScope,
        permissions: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register a parent approval for delegation.

        Args:
            approval_id: Unique approval identifier.
            scope: Approval scope.
            permissions: List of granted permissions.
            metadata: Optional metadata.
        """
        self._approvals[approval_id] = {
            "scope": scope,
            "permissions": permissions,
            "metadata": metadata or {},
            "registered_at": time.time(),
        }

        logger.info(
            "Registered approval %s with scope %s",
            approval_id,
            scope,
        )

    def create_delegation(
        self,
        parent_approval_id: str,
        coordinator_id: str,
        worker_id: str,
        scope: DelegationScope | None = None,
        ttl_seconds: float | None = None,
        max_uses: int | None = None,
    ) -> DelegationToken | None:
        """Create a delegation token for a worker.

        Args:
            parent_approval_id: Parent approval identifier.
            coordinator_id: Coordinator identifier.
            worker_id: Worker identifier.
            scope: Optional scope override (must be <= parent scope).
            ttl_seconds: Optional TTL override.
            max_uses: Optional max uses override.

        Returns:
            DelegationToken or None if parent not found.
        """
        if parent_approval_id not in self._approvals:
            logger.warning("Parent approval %s not found", parent_approval_id)
            return None

        parent = self._approvals[parent_approval_id]

        # Validate scope hierarchy
        if scope and not self._is_scope_valid(scope, parent["scope"]):
            logger.warning(
                "Invalid scope %s (parent has %s)",
                scope,
                parent["scope"],
            )
            return None

        import uuid

        now = time.time()
        token = DelegationToken(
            token_id=str(uuid.uuid4())[:8],
            parent_approval_id=parent_approval_id,
            coordinator_id=coordinator_id,
            worker_id=worker_id,
            scope=scope or parent["scope"],
            granted_at=now,
            expires_at=now + (ttl_seconds or self._default_ttl),
            permissions=parent["permissions"],
            max_uses=max_uses or self._max_uses,
        )

        self._tokens[token.token_id] = token

        # ``token_id`` is an opaque internal handle (not the JWT itself).
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
        logger.info(
            "Created delegation token %s for worker %s (scope: %s)",
            token.token_id,
            worker_id,
            token.scope,
        )

        return token

    def _is_scope_valid(
        self,
        requested: DelegationScope,
        parent: DelegationScope,
    ) -> bool:
        """Check if requested scope is valid given parent scope.

        Args:
            requested: Requested scope.
            parent: Parent scope.

        Returns:
            True if valid.
        """
        scope_hierarchy = {
            "read": 0,
            "write": 1,
            "execute": 2,
            "full": 3,
        }

        return scope_hierarchy.get(requested, 0) <= scope_hierarchy.get(parent, 0)

    def verify_token(
        self,
        token_id: str,
        required_permission: str,
    ) -> bool:
        """Verify a delegation token has required permission.

        Args:
            token_id: Token identifier.
            required_permission: Required permission string.

        Returns:
            True if token is valid and has permission.
        """
        if token_id not in self._tokens:
            return False

        token = self._tokens[token_id]

        if not token.can_use():
            # Clean up expired/exhausted tokens
            del self._tokens[token_id]
            return False

        # Check permission
        if required_permission not in token.permissions:
            # ``token_id`` is an opaque internal handle (not the JWT itself).
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.warning(
                "Token %s lacks permission %s",
                token_id,
                required_permission,
            )
            return False

        return True

    def use_token(self, token_id: str) -> bool:
        """Mark a token as used.

        Args:
            token_id: Token identifier.

        Returns:
            True if used successfully.
        """
        if token_id not in self._tokens:
            return False

        token = self._tokens[token_id]
        return token.use()

    def get_token(self, token_id: str) -> DelegationToken | None:
        """Get a delegation token.

        Args:
            token_id: Token identifier.

        Returns:
            DelegationToken or None.
        """
        return self._tokens.get(token_id)

    def revoke_token(self, token_id: str) -> bool:
        """Revoke a delegation token.

        Args:
            token_id: Token identifier.

        Returns:
            True if revoked.
        """
        if token_id in self._tokens:
            del self._tokens[token_id]
            # ``token_id`` is an opaque internal handle (not the JWT itself).
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.info("Revoked delegation token %s", token_id)
            return True
        return False

    def revoke_worker_tokens(self, worker_id: str) -> int:
        """Revoke all tokens for a worker.

        Args:
            worker_id: Worker identifier.

        Returns:
            Number of tokens revoked.
        """
        to_revoke = [tid for tid, t in self._tokens.items() if t.worker_id == worker_id]

        for token_id in to_revoke:
            del self._tokens[token_id]

        # Counts and worker_id only - not credentials.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
        logger.info(
            "Revoked %d tokens for worker %s",
            len(to_revoke),
            worker_id,
        )

        return len(to_revoke)

    def cleanup_expired(self) -> int:
        """Clean up expired tokens.

        Returns:
            Number of tokens cleaned up.
        """
        expired = [tid for tid, t in self._tokens.items() if t.is_expired()]

        for token_id in expired:
            del self._tokens[token_id]

        if expired:
            logger.debug("Cleaned up %d expired tokens", len(expired))

        return len(expired)

    def get_token_hash(self, token_id: str) -> str:
        """Get a short hash for a token (for logging).

        Args:
            token_id: Token identifier.

        Returns:
            Short hash string.
        """
        return hashlib.sha256(token_id.encode()).hexdigest()[:8]

    # ------------------------------------------------------------------
    # Signed capability-token bridge (issue #2611)
    # ------------------------------------------------------------------

    def mint_capability(
        self,
        *,
        issuer_identity_id: str,
        issuer_private_key: bytes,
        subject_identity_id: str,
        subject_pubkey: bytes | str,
        scope: DelegationScope,
        remaining_depth: int,
        ttl_seconds: float | None = None,
        task_ids: set[str] | frozenset[str] | None = None,
        path_prefixes: set[str] | frozenset[str] | None = None,
        max_uses: int | None = None,
        parent: CapabilityToken | None = None,
        audit_chain: AuditChainStore | None = None,
    ) -> CapabilityToken:
        """Mint a signed capability token from a legacy scope enum.

        When ``parent`` is ``None`` a root token is minted (the principal's
        grant); otherwise the token attenuates ``parent`` and its enum-derived
        caveats must be a subset of the parent's or the mint raises
        :class:`~bernstein.core.security.capability_tokens.AttenuationError`.
        The minted token is registered for liveness so :meth:`verify_capability`
        can later report revocation.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        not_after = time.time() + ttl
        if parent is not None:
            # A child never outlives its parent. Clamping (rather than raising)
            # keeps the common "same TTL" attenuation ergonomic: an absolute
            # expiry computed a few microseconds after the parent's would
            # otherwise read as a widening and be rejected.
            not_after = min(not_after, parent.caveats.not_after)
        caveats = enum_to_caveats(
            scope,
            remaining_depth=remaining_depth,
            not_after=not_after,
            task_ids=task_ids,
            path_prefixes=path_prefixes,
            max_uses=max_uses if max_uses is not None else self._max_uses,
        )
        if parent is None:
            token = _cap.mint_root(
                issuer_identity_id=issuer_identity_id,
                issuer_private_key=issuer_private_key,
                subject_identity_id=subject_identity_id,
                subject_pubkey=subject_pubkey,
                caveats=caveats,
                audit_chain=audit_chain,
            )
        else:
            token = _cap.attenuate(
                parent,
                issuer_private_key=issuer_private_key,
                subject_identity_id=subject_identity_id,
                subject_pubkey=subject_pubkey,
                caveats=caveats,
                audit_chain=audit_chain,
            )
        self._capabilities[token.token_hash()] = token
        return token

    def verify_capability(
        self,
        chain: CapabilityChain,
        required_permission: str,
        *,
        trust_anchors: set[str],
        audit_chain: AuditChainStore | None = None,
    ) -> bool:
        """Verify a capability chain grants ``required_permission`` (dual path).

        Offline first: the chain must pass
        :func:`~bernstein.core.security.capability_tokens.verify_chain` (per-hop
        signature, structural linkage, identity/pubkey continuity, monotonic
        attenuation, trusted root) with no network and no registry, and the leaf
        must actually carry the permission. Only then is the in-process registry
        consulted for *liveness* (no hop expired) and *revocation* (no hop
        revoked). A tampered or widened chain is rejected before the registry is
        ever touched, so authority never depends on a live coordinator.
        """
        result = _cap.verify_chain(chain, trust_anchors=trust_anchors, audit_chain=audit_chain)
        if not result.valid:
            return False
        if not chain.tokens:
            return False
        leaf = chain.tokens[-1]
        if required_permission not in leaf.caveats.permissions:
            return False
        # Registry liveness/revocation (cache-only, never the source of truth).
        now = time.time()
        for token in chain.tokens:
            if token.token_hash() in self._revoked_capabilities:
                return False
            if token.caveats.not_after <= now:
                return False
        return True

    def revoke_capability(self, token_hash: str) -> None:
        """Revoke a capability token by its JCS hash (liveness only).

        Revocation is registry state, not a property of the signed bytes: the
        chain still verifies offline, but :meth:`verify_capability` refuses a
        chain containing a revoked hop.
        """
        self._revoked_capabilities.add(token_hash)
        # ``token_hash`` is a content hash, not a credential.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
        logger.info("Revoked capability token %s", token_hash[:8])


def should_delegate(
    coordinator_mode: bool,
    has_parent_approval: bool,
    worker_scope: DelegationScope,
) -> bool:
    """Determine if delegation should be used.

    Args:
        coordinator_mode: Whether coordinator mode is enabled.
        has_parent_approval: Whether parent approval exists.
        worker_scope: Worker's required scope.

    Returns:
        True if delegation should be used.
    """
    return coordinator_mode and has_parent_approval and worker_scope != "full"
