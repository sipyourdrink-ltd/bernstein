"""Cluster node registration hardening with JWT authentication.

Adds JWT-based authentication for node registration and heartbeats.
Unauthenticated nodes are rejected. Tokens carry a ``node`` scope and
are verified on every registration and heartbeat request.
"""

from __future__ import annotations

import hmac
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


def _record_admission_failure(reason: str) -> None:
    """Increment the cluster admission-failure counter; never raise.

    Imported lazily so this module stays importable when the Prometheus
    package isn't on the path (e.g. minimal test envs).
    """
    try:
        from bernstein.core.observability import prometheus as _metrics

        _metrics.record_admission_failure(reason)
    except Exception:  # pragma: no cover - defensive
        logger.debug("Failed to record admission failure metric", exc_info=True)


class ClusterAuthError(Exception):
    """Raised when cluster authentication fails."""


@dataclass(frozen=True)
class ClusterAuthConfig:
    """Configuration for cluster JWT authentication.

    Attributes:
        secret: Shared secret for JWT signing. Also accepted verbatim as a
            worker bearer credential (see ``verify_request``).
        token_expiry_hours: How long node tokens remain valid.
        require_auth: Whether authentication is mandatory.
        allowed_scopes: Set of scopes that grant registration access.
        shared_secrets: Additional raw bearer values accepted as a full-scope
            worker credential (e.g. the operator's API bearer token when it
            differs from ``secret``). Lets one credential satisfy both the
            outer API middleware and the inner cluster layer (issue #2805).
    """

    secret: str
    token_expiry_hours: int = 24
    require_auth: bool = True
    allowed_scopes: tuple[str, ...] = (SCOPE_NODE_REGISTER, SCOPE_NODE_HEARTBEAT, SCOPE_NODE_ADMIN)
    shared_secrets: tuple[str, ...] = ()


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
        # Only the node_id and scope list are logged; the JWT itself stays in
        # the return value.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure  # noqa: E501
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
            _record_admission_failure("invalid_token")
            raise ClusterAuthError("Missing Authorization header")

        parts = authorization.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            _record_admission_failure("invalid_token")
            raise ClusterAuthError("Invalid Authorization header format (expected 'Bearer <token>')")

        token = parts[1]

        # Check revocation
        if token in self._revoked_tokens:
            _record_admission_failure("invalid_token")
            raise ClusterAuthError("Token has been revoked")

        # Shared-secret path: a worker may present the raw cluster secret (or
        # the operator's API bearer token) instead of a minted node JWT. This
        # is the single worker-join credential story (#2805): the same token
        # the outer API middleware accepts also authenticates node
        # registration and heartbeat, so no separate JWT issuance surface is
        # needed. Constant-time compared against every configured secret; a
        # match grants the full node scope set.
        for candidate in (self._config.secret, *self._config.shared_secrets):
            if candidate and hmac.compare_digest(token, candidate):
                now = time.time()
                return JWTPayload(
                    session_id="cluster-shared-secret",
                    user_id=None,
                    issued_at=now,
                    expires_at=now + self._config.token_expiry_hours * 3600,
                    scopes=list(self._config.allowed_scopes),
                )

        payload = self._jwt.verify_token(token)
        if payload is None:
            _record_admission_failure("invalid_token")
            raise ClusterAuthError("Invalid or expired token")

        # Check required scope
        if required_scope not in payload.scopes:
            _record_admission_failure("scope_denied")
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
        # JWT prefix would be the public header (alg/typ) only, but we
        # mask anyway so the log line is unambiguous in audit review.
        from bernstein.core.security.redactor import mask

        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure  # noqa: E501
        logger.info("Revoked cluster token: %s", mask(token, keep=4))

    def revoke_node(self, node_id: str) -> None:
        """Revoke all tokens associated with a node.

        Args:
            node_id: Node identifier whose tokens should be revoked.
        """
        session_id = self._node_tokens.pop(node_id, None)
        if session_id:
            # Only the node_id (public identifier) is logged.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
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
