"""ENT-002: Cluster node registration hardening with JWT authentication.

Adds JWT-based authentication for node registration and heartbeats.
Unauthenticated nodes are rejected. Tokens carry a ``node`` scope and
are verified on every registration and heartbeat request.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.jwt_tokens import JWTManager, JWTPayload

if TYPE_CHECKING:
    from bernstein.core.models import NodeInfo

logger = logging.getLogger(__name__)

# Scope constants for cluster JWT tokens.
SCOPE_NODE_REGISTER = "node:register"
SCOPE_NODE_HEARTBEAT = "node:heartbeat"
SCOPE_NODE_ADMIN = "node:admin"


class ClusterAuthError(Exception):
    """Raised when cluster authentication fails."""


@dataclass(frozen=True)
class ClusterAuthConfig:
    """Configuration for cluster JWT authentication.

    Attributes:
        secret: Shared secret for JWT signing.
        token_expiry_hours: How long node tokens remain valid.
        require_auth: Whether authentication is mandatory.
        allowed_scopes: Set of scopes that grant registration access.
    """

    secret: str
    token_expiry_hours: int = 24
    require_auth: bool = True
    allowed_scopes: tuple[str, ...] = (SCOPE_NODE_REGISTER, SCOPE_NODE_HEARTBEAT, SCOPE_NODE_ADMIN)


class ClusterAuthenticator:
    """JWT-based authenticator for cluster node operations.

    Validates tokens on registration and heartbeat requests.
    Issues tokens to nodes during initial enrollment.
    """

    def __init__(self, config: ClusterAuthConfig) -> None:
        self._config = config
        self._jwt = JWTManager(
            secret=config.secret,
            expiry_hours=config.token_expiry_hours,
        )
        self._revoked_tokens: set[str] = set()
        self._node_tokens: dict[str, str] = {}  # node_id -> session_id

    @property
    def require_auth(self) -> bool:
        """Whether authentication is mandatory."""
        return self._config.require_auth

    def issue_node_token(
        self,
        node_id: str,
        scopes: list[str] | None = None,
    ) -> str:
        """Issue a JWT token for a cluster node.

        Args:
            node_id: Unique identifier for the node.
            scopes: Token scopes. Defaults to register + heartbeat.

        Returns:
            Signed JWT token string.
        """
        if scopes is None:
            scopes = [SCOPE_NODE_REGISTER, SCOPE_NODE_HEARTBEAT]
        token = self._jwt.create_token(
            session_id=f"node-{node_id}",
            user_id=node_id,
            scopes=scopes,
        )
        self._node_tokens[node_id] = f"node-{node_id}"
        logger.info("Issued cluster token for node %s with scopes %s", node_id, scopes)
        return token

    def verify_request(
        self,
        authorization: str | None,
        required_scope: str = SCOPE_NODE_REGISTER,
    ) -> JWTPayload:
        """Verify an incoming request's authorization header.

        Args:
            authorization: The ``Authorization`` header value (``Bearer <token>``).
            required_scope: The scope that must be present in the token.

        Returns:
            Verified JWTPayload.

        Raises:
            ClusterAuthError: If the token is missing, invalid, expired,
                revoked, or lacks the required scope.
        """
        if not self._config.require_auth:
            # Auth disabled: return a synthetic payload
            return JWTPayload(
                session_id="anonymous",
                user_id=None,
                issued_at=time.time(),
                expires_at=time.time() + 3600,
                scopes=list(self._config.allowed_scopes),
            )

        if not authorization:
            raise ClusterAuthError("Missing Authorization header")

        parts = authorization.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise ClusterAuthError("Invalid Authorization header format (expected 'Bearer <token>')")

        token = parts[1]

        # Check revocation
        if token in self._revoked_tokens:
            raise ClusterAuthError("Token has been revoked")

        payload = self._jwt.verify_token(token)
        if payload is None:
            raise ClusterAuthError("Invalid or expired token")

        # Check required scope
        if required_scope not in payload.scopes:
            raise ClusterAuthError(f"Token lacks required scope '{required_scope}' (has: {payload.scopes})")

        return payload

    def verify_registration(self, authorization: str | None) -> JWTPayload:
        """Verify a node registration request.

        Args:
            authorization: The ``Authorization`` header value.

        Returns:
            Verified JWTPayload.

        Raises:
            ClusterAuthError: If verification fails.
        """
        return self.verify_request(authorization, SCOPE_NODE_REGISTER)

    def verify_heartbeat(self, authorization: str | None) -> JWTPayload:
        """Verify a node heartbeat request.

        Args:
            authorization: The ``Authorization`` header value.

        Returns:
            Verified JWTPayload.

        Raises:
            ClusterAuthError: If verification fails.
        """
        return self.verify_request(authorization, SCOPE_NODE_HEARTBEAT)

    def revoke_token(self, token: str) -> None:
        """Revoke a token so it cannot be used again.

        Args:
            token: The JWT token string to revoke.
        """
        self._revoked_tokens.add(token)
        logger.info("Revoked cluster token (hash: %s...)", token[:16])

    def revoke_node(self, node_id: str) -> None:
        """Revoke all tokens associated with a node.

        Args:
            node_id: Node identifier whose tokens should be revoked.
        """
        session_id = self._node_tokens.pop(node_id, None)
        if session_id:
            logger.info("Revoked tokens for node %s", node_id)

    def is_node_authenticated(self, node_id: str) -> bool:
        """Check whether a node has an active token.

        Args:
            node_id: Node identifier.

        Returns:
            True if the node has a registered session.
        """
        return node_id in self._node_tokens


class AuthenticatedNodeRegistry:
    """Wrapper around NodeRegistry that enforces JWT authentication.

    Delegates to the underlying NodeRegistry only after verifying
    the caller's JWT token.
    """

    def __init__(
        self,
        registry: Any,  # NodeRegistry from cluster.py
        authenticator: ClusterAuthenticator,
    ) -> None:
        self._registry = registry
        self._auth = authenticator

    def register(self, node: NodeInfo, authorization: str | None) -> tuple[NodeInfo, str]:
        """Register a node with JWT verification.

        Args:
            node: Node information.
            authorization: Authorization header value.

        Returns:
            Tuple of (registered NodeInfo, issued JWT token).

        Raises:
            ClusterAuthError: If auth is required and verification fails.
        """
        if self._auth.require_auth:
            self._auth.verify_registration(authorization)

        registered = self._registry.register(node)
        token = self._auth.issue_node_token(registered.id)
        return registered, token

    def heartbeat(
        self,
        node_id: str,
        authorization: str | None,
        capacity: Any | None = None,
    ) -> Any | None:
        """Process a heartbeat with JWT verification.

        Args:
            node_id: Node identifier.
            authorization: Authorization header value.
            capacity: Optional updated capacity.

        Returns:
            Updated NodeInfo, or None if node is unknown.

        Raises:
            ClusterAuthError: If auth is required and verification fails.
        """
        if self._auth.require_auth:
            payload = self._auth.verify_heartbeat(authorization)
            # Verify the token belongs to the right node
            if payload.user_id and payload.user_id != node_id:
                raise ClusterAuthError(
                    f"Token node_id mismatch: token for '{payload.user_id}', heartbeat for '{node_id}'"
                )

        return self._registry.heartbeat(node_id, capacity)

    def unregister(self, node_id: str, authorization: str | None) -> bool:
        """Unregister a node with JWT verification.

        Args:
            node_id: Node identifier.
            authorization: Authorization header value.

        Returns:
            True if the node was removed.

        Raises:
            ClusterAuthError: If auth is required and verification fails.
        """
        if self._auth.require_auth:
            self._auth.verify_request(authorization, SCOPE_NODE_ADMIN)
        self._auth.revoke_node(node_id)
        return self._registry.unregister(node_id)
