"""Permission delegation from coordinator to workers."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


DelegationScope = Literal["read", "write", "execute", "full"]


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
